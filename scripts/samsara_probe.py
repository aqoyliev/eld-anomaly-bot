"""Quick probe for the Samsara fleet API.

Calls GET /fleet/vehicles/stats?types=gps and prints a per-vehicle summary
(name, speed, position, reading time). Handy for confirming a company's
Samsara token authenticates and that vehicle names line up with the unit
numbers Quantum stores, before adding the company to the bot.

Run from the project root:
    .venv/Scripts/python.exe scripts/samsara_probe.py --token <api-token>
    .venv/Scripts/python.exe scripts/samsara_probe.py            # uses SAMSARA_TOKEN from .env
    .venv/Scripts/python.exe scripts/samsara_probe.py --raw      # dump raw JSON of page 1
"""

import argparse
import asyncio
import json
import os
import sys

import aiohttp

# Allow running as a standalone script from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402
from utils.eld import samsara  # noqa: E402


async def probe(token: str, raw: bool) -> int:
    if not token:
        print("No token: pass --token or set SAMSARA_TOKEN in .env")
        return 1

    url = f"{config.SAMSARA_BASE_URL.rstrip('/')}/fleet/vehicles/stats"
    if raw:
        headers = {"accept": "application/json",
                   "Authorization": f"Bearer {token}"}
        print(f"GET {url}?types=gps")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as s:
            async with s.get(url, headers=headers,
                             params={"types": "gps"}) as r:
                print(f"HTTP status : {r.status}")
                text = await r.text()
        try:
            print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print(text)
        return 0

    print(f"GET {url}?types=gps (paged via the samsara client)")
    try:
        # threshold -1 so even parked (0 mph) vehicles are listed.
        vehicles = await samsara.fetch_moving_vehicles(
            token, config.SAMSARA_BASE_URL, threshold_mph=-1.0
        )
    except RuntimeError as err:
        print(f"FAILED: {err}")
        print("\nToken did not authenticate (or lacks the 'Read Vehicle "
              "Statistics' scope).")
        return 3

    print(f"\nvehicles with a GPS speed reading: {len(vehicles)}")
    for unit, v in sorted(vehicles.items()):
        print(f"  {unit:<12} id={v.vehicle_id}  {v.speed or 0:.0f} mph  "
              f"@ {v.coordinates_label}  ({v.time})")
    moving = [u for u, v in vehicles.items() if (v.speed or 0) > 0]
    print(f"\nmoving now: {len(moving)}" + (f" — {moving}" if moving else ""))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the Samsara fleet API.")
    parser.add_argument("--token", default=config.SAMSARA_TOKEN,
                        help="Samsara API token (default: SAMSARA_TOKEN from .env)")
    parser.add_argument("--raw", action="store_true",
                        help="dump the raw JSON of the first page instead")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(probe(args.token, args.raw)))


if __name__ == "__main__":
    main()
