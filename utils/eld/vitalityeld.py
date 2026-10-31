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

Zero ELD runs on the same DriveHOS platform, so this client serves it too; the
``brand`` argument only changes the name in errors and logs.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

from data import config

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

# Trucks come and go (deactivated, re-activated) a few times a day at most.
_ACTIVE_TTL_SECONDS = 600
_active_cache: Dict[str, Tuple[float, set]] = {}

# Which provider key last produced units for a company (Vitality vs Vitality Plus).
_provider_used: Dict[str, str] = {}

# The internal API (utils/internal_api) serves other bots from the data this
# client already fetched instead of asking DriveHOS again:
#   * _latest_raw: (base_url, company_key) -> the last latest-vehicle-status
#     rows, BEFORE the deactivated-truck filter, with when and how they came;
#   * _roster_rows: company_key -> (monotonic time, the /v2/drivers rows), kept
#     next to _driver_cache so one roster fetch an hour serves both.
_latest_raw: Dict[Tuple[str, str], "RawFleet"] = {}
_roster_rows: Dict[str, Tuple[float, List[dict]]] = {}


class DriveHOSAuthError(RuntimeError):
    """401/403: DriveHOS rejected the carrier's company key (or ours). Not a
    blip — it stays that way until someone fixes the key."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass
class VitalityVehicle:
    unit_number: str
    vin: Optional[str]
    last_report_time: Optional[datetime]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    driver: Optional[str] = None
    location: Optional[str] = None  # human-readable, e.g. "1.0mi E of Carney, MT"
    # Extra status-feed fields, used only by observer.py to spot an ELD that
    # still pings its position but records no engine data.
    speed: Optional[float] = None
    status: Optional[str] = None  # IN_MOTION / STATIONARY / OFFLINE
    odometer: Optional[float] = None

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
        logger.warning("DriveHOS: could not parse time %r", value)
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
        speed=_parse_coord(raw.get("speed")),
        status=(str(raw["status"]).upper() if raw.get("status") else None),
        odometer=_parse_coord(raw.get("odometer")),
    )


@dataclass
class RawFleet:
    """One latest-vehicle-status answer as DriveHOS gave it (see _latest_raw)."""
    fetched_at: datetime          # tz-aware UTC
    fetched_mono: float           # time.monotonic() at the fetch
    provider_key: str             # the provider key that produced it
    brand: str                    # "Vitality" / "Zero"
    rows: List[dict]              # every vehicle, deactivated ones included
    active_ids: Optional[set]     # /v2/vehicles ids; None = unknown
    source: str = "poller"        # "poller" / "on_demand" (set by the API)


def _publish_raw(
    company_key: str, provider_key: str, base_url: str, brand: str,
    rows: List[dict], active_ids: Optional[set],
) -> None:
    _latest_raw[(base_url.rstrip("/"), company_key)] = RawFleet(
        fetched_at=datetime.now(timezone.utc), fetched_mono=time.monotonic(),
        provider_key=provider_key, brand=brand, rows=list(rows),
        active_ids=set(active_ids) if active_ids is not None else None,
    )


def latest_raw(company_key: str, base_url: str) -> Optional[RawFleet]:
    """The last fleet fetched for this carrier key (by the poller or the
    internal API), or None."""
    return _latest_raw.get((base_url.rstrip("/"), company_key))


def roster_rows(company_key: str) -> Optional[Tuple[float, List[dict]]]:
    """(monotonic time, /v2/drivers rows) from the last roster fetch, or None."""
    return _roster_rows.get(company_key)


def remember_roster(company_key: str, rows: List[dict]) -> Dict[str, str]:
    """Store a freshly fetched /v2/drivers roster for both users of it and
    return {driver_id: "FIRST LAST"} (the poller's upper-case style)."""
    names = {}
    for r in rows:
        # Roster names carry stray spaces ("ELISE ", "PRINCE JAMES "); collapse.
        name = " ".join(f"{r.get('first_name') or ''} {r.get('last_name') or ''}".split())
        if r.get("driver_id") and name:
            names[r["driver_id"]] = name.upper()  # same style as Quantum's names
    now = time.monotonic()
    _driver_cache[company_key] = (now, names)
    _roster_rows[company_key] = (now, [r for r in rows if isinstance(r, dict)])
    return names


def _headers(company_key: str, provider_key: str) -> dict:
    return {
        "accept": "application/json",
        "X-API-Provider-Key": provider_key,
        "X-API-Company-Key": company_key,
    }


async def _fetch_pages(
    session: aiohttp.ClientSession, url: str, headers: dict, page_size: int,
    brand: str,
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
                message = f"{brand} {url} HTTP {resp.status}: {body}"
                if resp.status in (401, 403):
                    raise DriveHOSAuthError(message, resp.status)
                raise RuntimeError(message)
            data = await resp.json(content_type=None)
        if data.get("description") != "success":
            raise RuntimeError(f"{brand} {url} error: {data.get('description')}")
        items.extend(data.get("data") or [])
        if page >= (data.get("total_pages") or 1):
            break
        page += 1
    return items


async def _driver_names(
    session: aiohttp.ClientSession, base_url: str, headers: dict,
    company_key: str, brand: str,
) -> Dict[str, str]:
    """{driver_id: "FIRST LAST"}, cached for an hour. A roster failure is not
    fatal — alerts just go out without a driver name."""
    cached = _driver_cache.get(company_key)
    if cached and time.monotonic() - cached[0] < _DRIVER_TTL_SECONDS:
        return cached[1]
    try:
        rows = await _fetch_pages(
            session, f"{base_url}/v2/drivers", headers, _DRIVER_PAGE_SIZE, brand
        )
    except Exception:
        logger.warning("%s: driver roster lookup failed", brand, exc_info=True)
        return cached[1] if cached else {}
    return remember_roster(company_key, rows)


async def _active_vehicle_ids(
    session: aiohttp.ClientSession, base_url: str, headers: dict,
    company_key: str, brand: str,
) -> Optional[set]:
    """vehicle_ids of the carrier's ACTIVE trucks, cached for ten minutes.

    ``/v2/latest-vehicle-status`` also lists trucks deactivated in the panel
    (drivers drop a truck at the yard, it is deactivated, yard staff move it
    while the ELD is off — all looks like a disconnect), but ``/v2/vehicles``
    holds only the active ones. Returns None when the list is unavailable or
    empty, meaning "don't filter": a lookup failure must never hide a fleet."""
    cached = _active_cache.get(company_key)
    if cached and time.monotonic() - cached[0] < _ACTIVE_TTL_SECONDS:
        return cached[1]
    try:
        rows = await _fetch_pages(
            session, f"{base_url}/v2/vehicles", headers, _VEHICLE_PAGE_SIZE, brand
        )
    except Exception:
        logger.warning("%s: active-vehicle list lookup failed", brand, exc_info=True)
        return cached[1] if cached else None
    ids = {
        r["vehicle_id"] for r in rows
        if r.get("vehicle_id") and r.get("active") is not False
    }
    if not ids:
        return None
    _active_cache[company_key] = (time.monotonic(), ids)
    return ids


async def _fetch_snapshot(
    company_key: str, provider_key: str, base_url: str, *,
    brand: str = "Vitality",
) -> Dict[str, VitalityVehicle]:
    """Fetch the full DriveHOS fleet snapshot for a carrier (Vitality or Zero
    ELD — ``brand`` names it in errors), keyed by NORMALIZED unit number
    (:func:`quantum_key`, same as EVO). Each unit is also indexed without
    leading zeros so "0942" and "942" meet; look up with :func:`lookup`.

    Raises on HTTP/auth errors so a credential problem surfaces instead of
    silently yielding zero anomalies."""
    base_url = base_url.rstrip("/")
    headers = _headers(company_key, provider_key)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        raw_units = await _fetch_pages(
            session, f"{base_url}/v2/latest-vehicle-status", headers,
            _VEHICLE_PAGE_SIZE, brand,
        )
        all_rows = raw_units  # before the deactivated-truck filter
        active_ids = (
            await _active_vehicle_ids(session, base_url, headers, company_key, brand)
            if raw_units else None
        )
        if active_ids is not None:
            total = len(raw_units)
            raw_units = [
                r for r in raw_units
                if not r.get("vehicle_id") or r["vehicle_id"] in active_ids
            ]
            if len(raw_units) < total:
                logger.debug(
                    "%s: skipped %d deactivated truck(s)", brand, total - len(raw_units)
                )
        drivers = (
            await _driver_names(session, base_url, headers, company_key, brand)
            if any(r.get("driver_id") for r in raw_units) else {}
        )
    _publish_raw(company_key, provider_key, base_url, brand, all_rows, active_ids)

    units: Dict[str, VitalityVehicle] = {}
    for raw in raw_units:
        vehicle = _parse_unit(raw, drivers)
        if vehicle is None:
            continue
        key = quantum_key(vehicle.unit_number)
        units.setdefault(key, vehicle)
        units.setdefault(key.lstrip("0") or key, vehicle)
    return units


async def fetch_units(
    company_key: str, provider_key: str, base_url: str, *,
    brand: str = "Vitality",
) -> Dict[str, VitalityVehicle]:
    """:func:`_fetch_snapshot` with the Vitality Plus fallback.

    Vitality Plus is the same API under its own provider key
    (``VITALITY_PLUS_PROVIDER_KEY``), and carriers that moved to it keep their
    company key. The old provider key then answers 403 or an empty fleet, so
    for Vitality the Plus key is tried next, and whichever key produced units
    is remembered per company and tried first from then on."""
    keys = [provider_key]
    plus = config.VITALITY_PLUS_PROVIDER_KEY
    if brand == "Vitality" and plus and plus != provider_key:
        keys.append(plus)
        if _provider_used.get(company_key) == plus:
            keys.reverse()
    first_error: Optional[Exception] = None
    snapshot: Dict[str, VitalityVehicle] = {}
    for key in keys:
        try:
            snapshot = await _fetch_snapshot(
                company_key, key, base_url, brand=brand
            )
        except DriveHOSAuthError as exc:
            first_error = first_error or exc
            continue
        if snapshot:
            _provider_used[company_key] = key
            return snapshot
    if first_error is not None and not snapshot:
        raise first_error
    return snapshot


def lookup(units: Dict[str, VitalityVehicle], unit_number: str) -> Optional[VitalityVehicle]:
    """Find a movement-provider unit number in a :func:`fetch_units` snapshot."""
    key = quantum_key(unit_number)
    return units.get(key) or units.get(key.lstrip("0") or key)
