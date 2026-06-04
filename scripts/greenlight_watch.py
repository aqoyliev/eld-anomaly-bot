"""Poll the GreenLight ELD API until the token becomes authorized.

Calls GET /vehicles/current on an interval and keeps going while the response
is `forbidden`/`invalid_token`. Exits 0 (printing a vehicle count) the moment it
returns `ok`, or exits 4 if it hits the max number of attempts.

Run from the project root:
    .venv/Scripts/python.exe scripts/greenlight_watch.py
    .venv/Scripts/python.exe scripts/greenlight_watch.py --interval 180 --max-tries 240
"""

import argparse
import asyncio
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402


async def check() -> tuple[str, object]:
    url = f"{config.GREENLIGHT_BASE_URL.rstrip('/')}/vehicles/current"
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {config.GREENLIGHT_TOKEN}",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url, headers=headers, params={"page": 0, "size": 10}) as r:
            data = await r.json()
    code = (data.get("result") or {}).get("code")
    return code, data


async def watch(interval: int, max_tries: int) -> int:
    for attempt in range(1, max_tries + 1):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            code, data = await check()
        except Exception as e:
            print(f"[{stamp}] try {attempt}: request error: {e}", flush=True)
            code = None

        if code == "ok":
            content = data.get("content") or []
            total = (data.get("pageable") or {}).get("total")
            print(f"[{stamp}] AUTHORIZED ✅  token is now active. "
                  f"vehicles on page={len(content)}, total={total}", flush=True)
            return 0

        print(f"[{stamp}] try {attempt}/{max_tries}: still '{code}', "
              f"waiting {interval}s…", flush=True)
        if attempt < max_tries:
            await asyncio.sleep(interval)

    print(f"Gave up after {max_tries} attempts; token still not authorized.",
          flush=True)
    return 4


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch GreenLight token auth.")
    parser.add_argument("--interval", type=int, default=180,
                        help="seconds between checks (default 180)")
    parser.add_argument("--max-tries", type=int, default=240,
                        help="max checks before giving up (default 240 = ~12h)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(watch(args.interval, args.max_tries)))


if __name__ == "__main__":
    main()
