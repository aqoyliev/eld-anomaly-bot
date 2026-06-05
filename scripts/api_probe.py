"""Manual end-to-end probe: call Motive (GoMotive) first, print the raw JSON,
then call GreenLight ELD for one unit and print that JSON too.

By default it pulls one small page from Motive's v1 vehicle_locations feed,
picks the first unit number it sees, and looks that unit up on GreenLight — so a
single run exercises both APIs with real, related data. Pass --unit to force a
specific GreenLight lookup, or --version v3 to probe Motive's v3 feed.

Run from the project root:
    .venv/Scripts/python.exe scripts/api_probe.py
    .venv/Scripts/python.exe scripts/api_probe.py --version v3 --size 20
    .venv/Scripts/python.exe scripts/api_probe.py --unit 2462
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
from utils.eld.greenlight import greenlight_key  # noqa: E402


def _dump(title: str, status: int, content_type: str, text: str) -> dict:
    """Pretty-print an HTTP response; return the parsed dict (or {} if not JSON)."""
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(f"HTTP status : {status}")
    print(f"Content-Type: {content_type}")
    print("Raw body:")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        return {}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return data


async def call_motive(
    session: aiohttp.ClientSession, version: str, size: int
) -> dict:
    if not config.GOMOTIVE_TOKEN:
        print("GOMOTIVE_TOKEN is not set in .env")
        return {}

    url = f"{config.GOMOTIVE_BASE_URL.rstrip('/')}/{version}/vehicle_locations"
    headers = {
        "accept": "application/json",
        "X-Api-Key": config.GOMOTIVE_TOKEN,
        "X-Metric-Units": "false",
    }
    params = {"per_page": size, "page_no": 1, "vehicle_status": "active"}

    print(f"GET {url}?per_page={size}&page_no=1&vehicle_status=active")
    async with session.get(url, headers=headers, params=params) as r:
        return _dump(
            f"MOTIVE ({version})", r.status, r.headers.get("Content-Type", ""),
            await r.text(),
        )


async def call_greenlight(session: aiohttp.ClientSession, unit: str) -> dict:
    if not config.GREENLIGHT_TOKEN:
        print("GREENLIGHT_TOKEN is not set in .env")
        return {}

    key = greenlight_key(unit)
    url = f"{config.GREENLIGHT_BASE_URL.rstrip('/')}/vehicles/{key}"
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {config.GREENLIGHT_TOKEN}",
    }

    print(f"\nGET {url}   (unit {unit!r} -> key {key!r})")
    async with session.get(url, headers=headers) as r:
        return _dump(
            f"GREENLIGHT (unit {key})", r.status,
            r.headers.get("Content-Type", ""), await r.text(),
        )


def _first_unit(motive: dict) -> str:
    for item in motive.get("vehicles") or []:
        number = (item.get("vehicle") or {}).get("number")
        if number:
            return str(number)
    return ""


async def run(version: str, size: int, unit: str) -> int:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        # 1) Motive first.
        motive = await call_motive(s, version, size)

        total = (motive.get("pagination") or {}).get("total")
        if total is not None:
            print(f"\n[Motive pagination.total = {total}]")

        # 2) GreenLight for a chosen unit (explicit --unit, else first from Motive).
        target = unit or _first_unit(motive)
        if not target:
            print("\nNo unit number to look up on GreenLight "
                  "(Motive returned none and --unit was not given).")
            return 1
        await call_greenlight(s, target)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Probe Motive then GreenLight.")
    p.add_argument("--version", choices=["v1", "v3"], default="v1",
                   help="Motive feed version (default v1)")
    p.add_argument("--size", type=int, default=5,
                   help="Motive page size (default 5)")
    p.add_argument("--unit", default="",
                   help="GreenLight unit to look up (default: first from Motive)")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.version, args.size, args.unit)))


if __name__ == "__main__":
    main()
