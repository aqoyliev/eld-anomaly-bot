"""Send a test ELD-disconnection alert through the real pipeline.

Because GreenLight isn't authorized yet, this mocks the GreenLight side only:
it picks a vehicle that is *actually moving right now* on GoMotive (live data),
then pretends GreenLight reports that same unit as a stale/disconnected ELD.
The detector, store (dedup), and Telegram alert all run for real, so the 🚨
message that lands in your alert channel is exactly what production will send.

Run from the project root:
    .venv/Scripts/python.exe scripts/mock_alert.py
    .venv/Scripts/python.exe scripts/mock_alert.py --unit 1137 --stale-min 35
    .venv/Scripts/python.exe scripts/mock_alert.py --cleanup   # resolve mock events
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
from utils.eld import gomotive, greenlight, poller, store  # noqa: E402
from utils.eld.greenlight import GreenLightVehicle  # noqa: E402


async def run(unit: str | None, stale_min: int, twice: bool) -> int:
    await store.init_db()

    moving = await gomotive.fetch_moving_vehicles(config.MOVING_SPEED_THRESHOLD)
    if not moving:
        print("No vehicles are moving on GoMotive right now — try again shortly.")
        return 1

    if unit is None:
        unit = next(iter(moving))
    if unit not in moving:
        print(f"Unit {unit!r} is not currently moving on GoMotive. "
              f"Moving units include: {list(moving)[:10]}")
        return 1

    gm = moving[unit]
    print(f"Picked moving vehicle {unit!r} (VIN {gm.vin}): "
          f"{gm.speed:.0f} mph @ {gm.location}")

    # Fake GreenLight: last report `stale_min` min old (UTC, like real GL times).
    stale_time = datetime.utcnow() - timedelta(minutes=stale_min)
    mock_vehicle = GreenLightVehicle(
        unit_number=unit,
        vin=gm.vin,
        driver="TEST DRIVER",
        state=None,
        location="(GreenLight last-known)",
        last_report_time=stale_time,
    )

    async def fake_fetch(unit_numbers, concurrency=10):
        # Pretend GreenLight reports the chosen unit as stale; others not found.
        return {u: (mock_vehicle if u == unit else None) for u in unit_numbers}

    # Patch only the GreenLight lookup; everything else is the real pipeline.
    greenlight.fetch_vehicles = fake_fetch

    bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML)
    try:
        print("\n-- poll cycle 1 (expect a new alert) --")
        await poller.poll_once(bot)
        if twice:
            print("\n-- poll cycle 2 (expect NO duplicate alert) --")
            await poller.poll_once(bot)
    finally:
        session = await bot.get_session()
        await session.close()

    print("\nActive (flagged) events — what /status shows:")
    for e in await store.get_active_events():
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
    parser.add_argument("--unit", help="specific unit number (default: first moving)")
    parser.add_argument("--stale-min", type=int, default=35,
                        help="minutes since the mocked GreenLight report (default 35)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle (default runs twice to show dedup)")
    parser.add_argument("--cleanup", action="store_true",
                        help="resolve any mock events instead of sending an alert")
    args = parser.parse_args()

    if args.cleanup:
        raise SystemExit(asyncio.run(cleanup()))
    raise SystemExit(asyncio.run(run(args.unit, args.stale_min, twice=not args.once)))


if __name__ == "__main__":
    main()
