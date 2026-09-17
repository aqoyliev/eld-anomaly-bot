"""Client for the Vitality ELD fleet API (DriveHOS "Global Integration API v2").

Docs: https://purple-balaur-ae4.notion.site/Global-Integration-API-v2-2f1bc443208e800b8b47d45f6b4a3e10

Vitality is a third ELD-side provider, playing the same role as Quantum and
EVO: the freshness of a unit's last report decides whether its ELD looks
disconnected (see detector.find_anomalies for the mixed-fleet rule).

Like EVO, the whole fleet comes back from one endpoint
(``GET /v2/latest-vehicle-status``, paginated, max 200 per page), so a poll
cycle costs one or two requests. Every request needs two headers:
  * ``X-API-Provider-Key`` — identifies this bot (global, from config). It is
    the rate-limited one, so we page with the max ``limit``.
  * ``X-API-Company-Key``  — the carrier's key (per company, from the DB).

The status feed carries only a ``driver_id``; names come from ``/v2/drivers``
and are cached for an hour.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

from .quantumeld import quantum_key

logger = logging.getLogger(__name__)

# latest-vehicle-status caps ``limit`` at 200, drivers at 1,000. A safety cap on
# pages guards against a misbehaving ``total_pages``.
_VEHICLE_PAGE_SIZE = 200
_DRIVER_PAGE_SIZE = 1000
_MAX_PAGES = 50

# Driver names change rarely; cache them per company key so the rate-limited
# provider key isn't spent on the roster every cycle.
_DRIVER_TTL_SECONDS = 3600
_driver_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}


@dataclass
class VitalityVehicle:
    unit_number: str
    vin: Optional[str]
    last_report_time: Optional[datetime]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    driver: Optional[str] = None
    location: Optional[str] = None  # human-readable, e.g. "1.0mi E of Carney, MT"

    @property
    def coordinates_label(self) -> str:
        """Vitality's last-reported lat,lon (the point where the ELD went dark)."""
        if self.latitude is not None and self.longitude is not None:
            return f"{self.latitude}, {self.longitude}"
        return self.location or "unknown"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """"2026-09-17T15:03:40Z" -> naive UTC (the codebase-wide convention)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Vitality: could not parse time %r", value)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_coord(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_unit(raw: dict, drivers: Dict[str, str]) -> Optional[VitalityVehicle]:
    number = (raw.get("number") or "").strip()
    if not number:
        return None
    lat, lon = _parse_coord(raw.get("lat")), _parse_coord(raw.get("lon"))
    if lat == 0 and lon == 0:
        lat = lon = None  # "0","0" = no fix (e.g. a device that never reported)
    return VitalityVehicle(
        unit_number=number,
        vin=raw.get("vin") or None,
        last_report_time=_parse_time(raw.get("timestamp")),
        latitude=lat,
        longitude=lon,
        driver=drivers.get(raw.get("driver_id") or ""),
        location=raw.get("calc_location") or None,
    )


def _headers(company_key: str, provider_key: str) -> dict:
    return {
        "accept": "application/json",
        "X-API-Provider-Key": provider_key,
        "X-API-Company-Key": company_key,
    }


async def _fetch_pages(
    session: aiohttp.ClientSession, url: str, headers: dict, page_size: int
) -> List[dict]:
    """Collect ``data`` across every page of a page/limit endpoint."""
    items: List[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        params = {"limit": page_size, "page": page}
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                # Truncate the body — error pages can be full HTML.
                body = " ".join((await resp.text()).split())[:160]
                raise RuntimeError(f"Vitality {url} HTTP {resp.status}: {body}")
            data = await resp.json(content_type=None)
        if data.get("description") != "success":
            raise RuntimeError(f"Vitality {url} error: {data.get('description')}")
        items.extend(data.get("data") or [])
        if page >= (data.get("total_pages") or 1):
            break
        page += 1
    return items


async def _driver_names(
    session: aiohttp.ClientSession, base_url: str, headers: dict, company_key: str
) -> Dict[str, str]:
    """{driver_id: "FIRST LAST"}, cached for an hour. A roster failure is not
    fatal — alerts just go out without a driver name."""
    cached = _driver_cache.get(company_key)
    if cached and time.monotonic() - cached[0] < _DRIVER_TTL_SECONDS:
        return cached[1]
    try:
        rows = await _fetch_pages(
            session, f"{base_url}/v2/drivers", headers, _DRIVER_PAGE_SIZE
        )
    except Exception:
        logger.warning("Vitality: driver roster lookup failed", exc_info=True)
        return cached[1] if cached else {}
    names = {}
    for r in rows:
        # Roster names carry stray spaces ("ELISE ", "PRINCE JAMES "); collapse.
        name = " ".join(f"{r.get('first_name') or ''} {r.get('last_name') or ''}".split())
        if r.get("driver_id") and name:
            names[r["driver_id"]] = name.upper()  # same style as Quantum's names
    _driver_cache[company_key] = (time.monotonic(), names)
    return names


async def fetch_units(
    company_key: str, provider_key: str, base_url: str
) -> Dict[str, VitalityVehicle]:
    """Fetch the full Vitality fleet snapshot for a carrier, keyed by NORMALIZED
    unit number (:func:`quantum_key`, same as EVO). Each unit is also indexed
    without leading zeros so "0942" and "942" meet; look up with :func:`lookup`.

    Raises on HTTP/auth errors so a credential problem surfaces instead of
    silently yielding zero anomalies."""
    base_url = base_url.rstrip("/")
    headers = _headers(company_key, provider_key)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        raw_units = await _fetch_pages(
            session, f"{base_url}/v2/latest-vehicle-status", headers,
            _VEHICLE_PAGE_SIZE,
        )
        drivers = (
            await _driver_names(session, base_url, headers, company_key)
            if any(r.get("driver_id") for r in raw_units) else {}
        )

    units: Dict[str, VitalityVehicle] = {}
    for raw in raw_units:
        vehicle = _parse_unit(raw, drivers)
        if vehicle is None:
            continue
        key = quantum_key(vehicle.unit_number)
        units.setdefault(key, vehicle)
        units.setdefault(key.lstrip("0") or key, vehicle)
    return units


def lookup(units: Dict[str, VitalityVehicle], unit_number: str) -> Optional[VitalityVehicle]:
    """Find a movement-provider unit number in a :func:`fetch_units` snapshot."""
    key = quantum_key(unit_number)
    return units.get(key) or units.get(key.lstrip("0") or key)
