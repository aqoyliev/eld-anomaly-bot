"""Send a test ELD-disconnection alert through the real pipeline.

This mocks the ELD side only: it picks a vehicle that is *actually moving right
now* on the company's movement provider (live GoMotive/Samsara data), then
pretends EVO reports that same unit as a stale/disconnected ELD.
The detector, store (dedup), and Telegram alert all run for real, so the 🚨
message that lands in your alert channel is exactly what production will send.

Run from the project root:
    .venv/Scripts/python.exe scripts/mock_alert.py
    .venv/Scripts/python.exe scripts/mock_alert.py --unit 1137 --stale-min 35
    .venv/Scripts/python.exe scripts/mock_alert.py --company "U-HOME LOGISTICS LLC"
    .venv/Scripts/python.exe scripts/mock_alert.py --cleanup   # delete mock events
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

# Allow running as a standalone script from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot, types  # noqa: E402

from data import config  # noqa: E402
from utils.eld import evoeld, gomotive, poller, samsara, store  # noqa: E402
from utils.eld.evoeld import EvoVehicle  # noqa: E402
from utils.eld.units import unit_key  # noqa: E402


async def _pick_company(name: str | None):
    """The named company, or the only one there is."""
    if name:
        return await store.get_company_by_name(name)
    companies = await store.list_companies(active_only=False)
    if len(companies) == 1:
        return companies[0]
    print("Several companies exist — name one with --company: "
          + ", ".join(c.name for c in companies))
    return None


async def run(company_name: str | None, unit: str | None, stale_min: int,
              twice: bool) -> int:
    await store.init_db()

    company = await _pick_company(company_name)
    if company is None:
        print("No such company. Add one with the /addcompany bot command.")
        return 1
    if not company.evo_configured:
        print(f"{company.name} has no EVO credentials, so the poll cycle would "
              "skip it. Set them with /seteld <company> evo first.")
        return 1

    # Whichever movement provider this company actually runs.
    if company.gomotive_token:
        moving = await gomotive.fetch_moving_vehicles(
            company.gomotive_token, config.GOMOTIVE_BASE_URL,
            threshold_mph=config.MOVING_SPEED_THRESHOLD,
        )
        where = "GoMotive"
    else:
        moving = await samsara.fetch_moving_vehicles(
            company.samsara_token, config.SAMSARA_BASE_URL,
            threshold_mph=config.MOVING_SPEED_THRESHOLD,
            freshness_seconds=config.SAMSARA_GPS_FRESHNESS,
        )
        where = "Samsara"
    if not moving:
        print(f"No vehicles are moving on {where} right now — try again shortly.")
        return 1

    if unit is None:
        unit = next(iter(moving))
    if unit not in moving:
        print(f"Unit {unit!r} is not currently moving on {where}. "
              f"Moving units include: {list(moving)[:10]}")
        return 1

    mv = moving[unit]
    print(f"Picked moving vehicle {unit!r} (VIN {mv.vin}) on {where}: "
          f"{mv.speed:.0f} mph @ {mv.coordinates_label}")

    # Fake EVO: last report `stale_min` min old (UTC, like real EVO times).
    mock_vehicle = EvoVehicle(
        unit_number=unit,
        vin=mv.vin,
        last_report_time=datetime.utcnow() - timedelta(minutes=stale_min),
        driver="TEST DRIVER",
    )

    async def fake_fetch(api_key=None, provider_token=None, usdot=None,
                         base_url=None):
        # The snapshot is keyed by normalized unit number, like the real one.
        return {unit_key(unit): mock_vehicle}

    # Patch only the ELD lookup; everything else is the real pipeline.
    evoeld.fetch_units = fake_fetch

    bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML)
    try:
        print("\n-- poll cycle 1 (expect a new alert) --")
        await poller.poll_once(bot, company)
        if twice:
            print("\n-- poll cycle 2 (expect NO duplicate alert) --")
            await poller.poll_once(bot, company)
    finally:
        session = await bot.get_session()
        await session.close()

    print("\nActive (flagged) events — what /status shows:")
    for e in await store.get_active_events(company.id):
        print(f"  {e.unit_number}: {e.last_speed} mph @ {e.last_location} "
              f"(disconnected {e.eld_disconnect_time})")

    print("\nCheck your alert channel for the 🚨 message.")
    return 0


async def cleanup() -> int:
    """Delete mock events outright so they don't linger in /history."""
    await store.init_db()
    n = await store.delete_events_by_driver("TEST DRIVER")
    print(f"Deleted {n} mock event(s).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test ELD-disconnect alert.")
    parser.add_argument("--company", help="company name (default: the only one)")
    parser.add_argument("--unit", help="specific unit number (default: first moving)")
    parser.add_argument("--stale-min", type=int, default=35,
                        help="minutes since the mocked EVO report (default 35)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle (default runs twice to show dedup)")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete any mock events instead of sending an alert")
    args = parser.parse_args()

    if args.cleanup:
        raise SystemExit(asyncio.run(cleanup()))
    raise SystemExit(asyncio.run(
        run(args.company, args.unit, args.stale_min, twice=not args.once)
    ))


if __name__ == "__main__":
    main()
