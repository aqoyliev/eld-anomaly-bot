"""Quick probe for the EVO ELD tracking API.

Calls GET /units-by-usdot/{usdot} with the given credentials and prints each
unit's number, VIN, coordinates, and last-report age, marking the ones the bot
would treat as STALE (>= ELD_STALE_THRESHOLD). Handy for confirming a
carrier's api key / provider token / USDOT before adding the company.

Run from the project root:
    .venv/Scripts/python.exe scripts/evo_probe.py --api-key K --provider-token T --usdot 123456
    .venv/Scripts/python.exe scripts/evo_probe.py ... --raw   (dump the raw JSON too)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp

# Allow running as a standalone script from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402
from utils.eld import evoeld  # noqa: E402


async def probe(api_key: str, provider_token: str, usdot: str,
                base_url: str, raw: bool) -> int:
    url = f"{base_url.rstrip('/')}/units-by-usdot/{usdot}"
    headers = {
        "accept": "application/json",
        "x-api-key": api_key,
        "provider-token": provider_token,
    }
    print(f"GET {url}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url, headers=headers) as r:
            status = r.status
            text = await r.text()
    print(f"HTTP status : {status}")
    if status != 200:
        print(text[:2000])
        return 2

    data = json.loads(text)
    if raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))

    units = [u for u in (evoeld._parse_unit(x) for x in data.get("units") or [])
             if u is not None]
    now = datetime.utcnow()
    stale = 0
    print(f"\n{'unit':<12} {'vin':<20} {'last report (UTC)':<20} {'age':<10} coordinates")
    for v in sorted(units, key=lambda x: x.unit_number):
        if v.last_report_time is not None:
            age_s = int((now - v.last_report_time).total_seconds())
            age = f"{age_s // 3600}h {age_s % 3600 // 60}m" if age_s >= 3600 else f"{age_s // 60}m {age_s % 60}s"
            is_stale = age_s >= config.ELD_STALE_THRESHOLD
            reported = v.last_report_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            age, is_stale, reported = "?", True, "(none)"
        stale += is_stale
        marker = "  [STALE]" if is_stale else ""
        print(f"{v.unit_number:<12} {(v.vin or '-'):<20} {reported:<20} {age:<10} "
              f"{v.coordinates_label}{marker}")

    print(f"\n--- summary ---")
    print(f"units       : {len(units)}")
    print(f"fresh       : {len(units) - stale}")
    print(f"stale/no-ts : {stale}  (threshold {config.ELD_STALE_THRESHOLD}s)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the EVO ELD tracking API.")
    parser.add_argument("--api-key", required=True, help="x-api-key header value")
    parser.add_argument("--provider-token", required=True,
                        help="provider-token header value")
    parser.add_argument("--usdot", required=True, help="carrier USDOT number")
    parser.add_argument("--base-url", default=config.EVO_BASE_URL,
                        help=f"API base URL (default {config.EVO_BASE_URL})")
    parser.add_argument("--raw", action="store_true", help="also dump raw JSON")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(probe(
        args.api_key, args.provider_token, args.usdot, args.base_url, args.raw
    )))


if __name__ == "__main__":
    main()
