"""One-off: dump raw Motive + Quantum JSON for a single unit of a named
company, using that company's tokens from the DB (not the seed .env values).

    .venv/Scripts/python.exe scripts/probe_2560.py --company "CROSS USA" --unit 2560
"""

import argparse
import asyncio
import json
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402
from utils.eld import store  # noqa: E402
from utils.eld.quantumeld import quantum_key  # noqa: E402


def _dump(title, status, text):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(f"HTTP status : {status}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        return {}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return data


async def motive_find(session, token, unit, version):
    """Page Motive's {version}/vehicle_locations until the unit shows up; dump
    just that vehicle's raw record."""
    url = f"{config.GOMOTIVE_BASE_URL.rstrip('/')}/{version}/vehicle_locations"
    headers = {"accept": "application/json", "X-Api-Key": token,
               "X-Metric-Units": "false"}
    page = 1
    while page <= 200:
        params = {"per_page": 50, "page_no": page, "vehicle_status": "active"}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status != 200:
                _dump(f"MOTIVE {version} ERROR", r.status, await r.text())
                return
            data = await r.json()
        vehicles = data.get("vehicles") or []
        for item in vehicles:
            v = item.get("vehicle") or {}
            if str(v.get("number")) == str(unit):
                _dump(f"MOTIVE {version} — unit {unit} (page {page})",
                      r.status, json.dumps(item))
                return
        if len(vehicles) < 50:
            break
        page += 1
    print(f"\n[MOTIVE {version}: unit {unit} not found in active vehicles]")


async def quantum_dump(session, token, base_url, unit):
    key = quantum_key(unit)
    url = f"{base_url.rstrip('/')}/vehicles/{key}"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    print(f"\nGET {url}   (unit {unit!r} -> key {key!r})")
    async with session.get(url, headers=headers) as r:
        _dump(f"QUANTUM — unit {key}", r.status, await r.text())


async def run(company_name, unit):
    await store.init_db()
    try:
        company = await store.get_company_by_name(company_name)
        if company is None:
            print(f"No company named {company_name!r} in the DB.")
            return 1
        base_url = company.quantum_base_url or config.QUANTUM_BASE_URL
        if base_url == "null" or not base_url:
            base_url = config.QUANTUM_BASE_URL
        print(f"Company id={company.id} name={company.name!r}")
        print(f"  gomotive_token : {company.gomotive_token}")
        print(f"  quantum_token  : {company.quantum_token}")
        print(f"  quantum_base   : {base_url}")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as s:
            await motive_find(s, company.gomotive_token, unit, "v1")
            await motive_find(s, company.gomotive_token, unit, "v3")
            await quantum_dump(s, company.quantum_token, base_url, unit)
    finally:
        await store.close()
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company", default="CROSS USA")
    p.add_argument("--unit", default="2560")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.company, args.unit)))


if __name__ == "__main__":
    main()
