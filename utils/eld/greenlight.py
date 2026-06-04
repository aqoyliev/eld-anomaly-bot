"""Client for the GreenLight ELD (GL ELD) vehicle-location API.

The carrier's token is authorized for the *per-vehicle* endpoint
``GET /vehicles/{unit_number}`` (the list endpoint /vehicles/current is not), so
the bot drives detection from GoMotive: for each vehicle GoMotive reports as
moving, we look it up here by unit number to read its last GreenLight report
time. A report older than ``ELD_STALE_THRESHOLD`` means the ELD looks
disconnected / offline.

GoMotive appends owner/tag suffixes to unit numbers (e.g. "0942  O/O") while
GreenLight stores the bare number ("0942"), so we strip the suffix before
looking up — see :func:`greenlight_key`.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote

import aiohttp

from data import config

logger = logging.getLogger(__name__)


@dataclass
class GreenLightVehicle:
    unit_number: str
    vin: Optional[str]
    driver: Optional[str]
    state: Optional[str]
    location: Optional[str]
    last_report_time: Optional[datetime]

    @property
    def location_label(self) -> str:
        parts = [p for p in (self.location, self.state) if p]
        return ", ".join(parts) if parts else "unknown"


def greenlight_key(unit_number: str) -> str:
    """Derive the bare unit number GreenLight stores from a GoMotive unit number.

    "0942  O/O" -> "0942", "2512 O/O" -> "2512", "002  ZM" -> "002",
    "1277  CROSS USA" -> "1277", "1137" -> "1137".
    A wide (2+ space) gap usually separates the number from a tag; otherwise, a
    leading numeric token followed by a tag is reduced to just the number.
    """
    u = unit_number.strip()
    head = re.split(r"\s{2,}", u)[0].strip()  # drop tag after a wide gap
    tokens = head.split()
    if len(tokens) > 1 and tokens[0].isdigit():
        return tokens[0]  # e.g. "2512 O/O" (single space) -> "2512"
    return head


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("GreenLight: could not parse time %r", value)
        return None


def _parse_vehicle(content: dict) -> Optional[GreenLightVehicle]:
    vehicle = content.get("vehicle") or {}
    unit_number = vehicle.get("unit_number")
    if not unit_number:
        return None
    location = content.get("location") or {}
    driver = (content.get("driver") or {}).get("name")
    return GreenLightVehicle(
        unit_number=str(unit_number),
        vin=vehicle.get("vin"),
        driver=driver,
        state=location.get("state"),
        location=location.get("location"),
        last_report_time=_parse_time(location.get("time")),
    )


async def fetch_vehicle(
    session: aiohttp.ClientSession, unit_number: str
) -> Optional[GreenLightVehicle]:
    """Look up one vehicle by unit number. Returns None if GreenLight has no
    record for it (HTTP 200 with content=null). Raises on auth/HTTP errors so a
    token problem surfaces instead of silently yielding zero anomalies."""
    key = greenlight_key(unit_number)
    url = f"{config.GREENLIGHT_BASE_URL.rstrip('/')}/vehicles/{quote(key)}"
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {config.GREENLIGHT_TOKEN}",
    }
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"GreenLight /vehicles/{key} HTTP {resp.status}: {body}")
        data = await resp.json()

    code = (data.get("result") or {}).get("code")
    if code != "ok":
        description = (data.get("result") or {}).get("description", code)
        raise RuntimeError(f"GreenLight /vehicles/{key} error: {code} ({description})")

    content = data.get("content")
    if not content:
        return None  # token is fine, this unit just isn't in GreenLight
    return _parse_vehicle(content)


async def fetch_vehicles(
    unit_numbers: List[str], concurrency: int = 10
) -> Dict[str, Optional[GreenLightVehicle]]:
    """Look up many vehicles concurrently. Returns {original_unit_number: vehicle
    or None}, keyed by the GoMotive unit number that was passed in."""
    results: Dict[str, Optional[GreenLightVehicle]] = {}
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def one(unit: str) -> None:
            async with sem:
                results[unit] = await fetch_vehicle(session, unit)

        await asyncio.gather(*(one(u) for u in unit_numbers))

    return results


def is_disconnected(
    vehicle: GreenLightVehicle,
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """True if the vehicle's last GreenLight report is older than the threshold
    (i.e. the ELD looks disconnected/offline).

    GreenLight report times are UTC (naive), so compare against UTC now."""
    if vehicle.last_report_time is None:
        return False
    now = now or datetime.utcnow()
    return (now - vehicle.last_report_time).total_seconds() >= threshold_seconds
