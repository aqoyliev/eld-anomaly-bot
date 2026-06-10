"""Quick probe for the Quantum ELD vehicle-location API.

Calls GET /vehicles/current with the token from .env and prints the raw JSON
response, plus a short summary. Handy for confirming the carrier-issued token
actually authenticates against Quantum before pointing the bot at it.

Run from the project root:
    .venv/Scripts/python.exe scripts/quantum_probe.py
    .venv/Scripts/python.exe scripts/quantum_probe.py --page 0 --size 10
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


async def probe(page: int, size: int) -> int:
    if not config.QUANTUM_TOKEN:
        print("QUANTUM_TOKEN is not set in .env")
        return 1

    url = f"{config.QUANTUM_BASE_URL.rstrip('/')}/vehicles/current"
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {config.QUANTUM_TOKEN}",
    }
    params = {"page": page, "size": size}

    print(f"GET {url}?page={page}&size={size}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url, headers=headers, params=params) as r:
            status = r.status
            content_type = r.headers.get("Content-Type", "")
            text = await r.text()

    print(f"HTTP status : {status}")
    print(f"Content-Type: {content_type}")
    print("Raw body:")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        return 0 if status == 200 else 2

    print(json.dumps(data, indent=2, ensure_ascii=False))

    # Summary
    code = (data.get("result") or {}).get("code")
    content = data.get("content")
    pageable = data.get("pageable") or {}
    print("\n--- summary ---")
    print(f"result.code     : {code}")
    if isinstance(content, list):
        print(f"vehicles (page) : {len(content)}")
    if pageable:
        print(f"pageable.total  : {pageable.get('total')}  "
              f"(current={pageable.get('current')}, next={pageable.get('next')})")
    if code != "ok":
        print("\nToken did not authenticate (result.code != 'ok'). Check the "
              "QUANTUM_TOKEN value and that the carrier has granted it access.")
        return 3
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the Quantum ELD API.")
    parser.add_argument("--page", type=int, default=0, help="page number (default 0)")
    parser.add_argument("--size", type=int, default=10, help="page size (default 10)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(probe(args.page, args.size)))


if __name__ == "__main__":
    main()
