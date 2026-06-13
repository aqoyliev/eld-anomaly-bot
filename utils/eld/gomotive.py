"""Client for the GoMotive (Motive) fleet API — the ground-truth movement
source used to confirm a vehicle is actually moving while its Quantum ELD
looks disconnected.

Strategy: query v3 first (Motive Vehicle Gateway feed, speed in km/h), then fill
in any vehicles v3 didn't return using v1 (speed in mph). Speeds are normalised
to mph internally so the rest of the system is unit-agnostic.

  v3: GET /v3/vehicle_locations
      speed at vehicle.current_location.kph (km/h)
      location at vehicle.current_location.current_location
  v1: GET /v1/vehicle_locations
      speed at vehicle.current_location.speed (mph)
      location at vehicle.current_location.description

Docs: https://developer-docs.gomotive.com/reference/
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

KPH_TO_MPH = 0.621371

# Safety cap so a misbehaving API (e.g. total never satisfied) can't loop forever.
# At page_size=50 this allows up to 10,000 vehicles.
_MAX_PAGES = 200

# The vehicle_locations endpoint caps vehicle_ids[] at 100 per request.
_MAX_IDS_PER_CALL = 100


@dataclass
class GoMotiveVehicle:
    unit_number: str
    vin: Optional[str]               # join key against Quantum
    speed: Optional[float]            # mph (always normalised to mph)
    location: Optional[str]          # human-readable last known location
    time: Optional[datetime]         # timestamp of this reading
    source: str = ""                 # "v3" or "v1" (for diagnostics)
    vehicle_id: Optional[int] = None  # GoMotive id (for targeted re-queries)
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Stamped onto events so the tracker knows which API to re-query (the
    # Samsara client carries "samsara"; see utils/eld/samsara.py).
    provider = "gomotive"

    @property
    def coordinates_label(self) -> str:
        if self.latitude is not None and self.longitude is not None:
            return f"{self.latitude}, {self.longitude}"
        return self.location or "unknown"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _fetch_raw(
    version: str, token: str, base_url: str, page_size: int = 50
) -> List[dict]:
    """Page through /{version}/vehicle_locations and return the raw ``vehicles``
    items. Returns [] (and logs) if the token is missing or the call fails, so a
    failure on one version never blocks the other."""
    if not token:
        logger.warning("GoMotive token not configured; skipping GoMotive poll.")
        return []

    headers = {
        "accept": "application/json",
        "X-Api-Key": token,
        "X-Metric-Units": "false",  # ask for imperial; we also convert kph ourselves
    }
    url = f"{base_url.rstrip('/')}/{version}/vehicle_locations"
    timeout = aiohttp.ClientTimeout(total=30)

    # The fleet is live, so pagination.total can change between page requests.
    # Don't rely on it: page until a page is short/empty (= last page), dedupe by
    # vehicle id (reordering between pages can repeat a vehicle), and cap pages.
    by_id: Dict[object, dict] = {}
    extras: List[dict] = []
    page_no = 1

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while page_no <= _MAX_PAGES:
            params = {
                "per_page": page_size,
                "page_no": page_no,
                "vehicle_status": "active",
            }
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"GoMotive {version} returned {resp.status}: {body}"
                    )
                data = await resp.json()

            page = data.get("vehicles") or []
            for item in page:
                vid = (item.get("vehicle") or {}).get("id")
                if vid is None:
                    extras.append(item)
                else:
                    by_id[vid] = item

            if len(page) < page_size:  # short or empty page => last page
                break
            page_no += 1
        else:
            logger.warning("GoMotive %s: hit page cap (%d); fleet may be larger.",
                           version, _MAX_PAGES)

    return list(by_id.values()) + extras


def _parse_vehicle(record: dict, version: str) -> Optional[GoMotiveVehicle]:
    vehicle = record.get("vehicle") or record
    number = vehicle.get("number")
    if not number:
        return None
    loc = vehicle.get("current_location") or {}

    if version == "v3":
        kph = loc.get("kph")
        speed = kph * KPH_TO_MPH if kph is not None else None
        location = loc.get("current_location")
    else:  # v1
        speed = loc.get("speed")
        location = loc.get("description")

    return GoMotiveVehicle(
        unit_number=str(number),
        vin=vehicle.get("vin"),
        speed=speed,
        location=location,
        time=_parse_time(loc.get("located_at")),
        source=version,
        vehicle_id=vehicle.get("id"),
        latitude=loc.get("lat"),
        longitude=loc.get("lon"),
    )


async def _fetch_version(
    version: str, token: str, base_url: str
) -> Dict[str, GoMotiveVehicle]:
    """Return {unit_number: vehicle} for one API version. Swallows errors so one
    version failing doesn't break the other."""
    out: Dict[str, GoMotiveVehicle] = {}
    try:
        records = await _fetch_raw(version, token, base_url)
    except Exception:
        logger.exception("GoMotive %s fetch failed", version)
        return out
    for record in records:
        v = _parse_vehicle(record, version)
        if v is not None:
            out[v.unit_number] = v
    return out


def _is_fresh(v: GoMotiveVehicle, max_age_seconds: Optional[int]) -> bool:
    """Whether the vehicle's location reading is recent enough to count as a
    CURRENT observation. GoMotive returns the last-known reading even after the
    gateway is powered off (frozen at its final speed), so a stale reading must
    not be treated as the truck moving right now. A missing reading time can't
    be confirmed current, so it is stale too."""
    if max_age_seconds is None:
        return True
    if v.time is None:
        return False
    # located_at is normally tz-aware (Z/offset); treat a naive time as UTC so
    # the subtraction never raises and stays consistent with the UTC clock.
    reading = v.time if v.time.tzinfo else v.time.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - reading).total_seconds() <= max_age_seconds


async def fetch_moving_vehicles(
    token: str,
    base_url: str,
    threshold_mph: float = 0.0,
    freshness_seconds: Optional[int] = None,
) -> Dict[str, GoMotiveVehicle]:
    """Return {unit_number: vehicle} for vehicles moving above the threshold.

    Coverage = v3 with v1 fallback: take v1 as the base, then let v3 override
    (v3 is the preferred Gateway feed), so vehicles only present in one feed are
    still included.

    ``freshness_seconds`` drops vehicles whose reading (current_location.
    located_at) is older than that — REQUIRED for anomaly detection (the poller
    passes ``config.GOMOTIVE_GPS_FRESHNESS``): GoMotive keeps a powered-off
    gateway frozen at its last speed, so without this a truck whose device died
    days ago is a permanent false anomaly. ``None`` (no filter) is for
    diagnostics like the probe scripts.
    """
    v1 = await _fetch_version("v1", token, base_url)
    v3 = await _fetch_version("v3", token, base_url)

    merged: Dict[str, GoMotiveVehicle] = dict(v1)
    merged.update(v3)  # v3 wins where both have the vehicle

    moving = {
        unit: v
        for unit, v in merged.items()
        if v.speed is not None and v.speed > threshold_mph
    }
    fresh = {
        unit: v for unit, v in moving.items() if _is_fresh(v, freshness_seconds)
    }

    logger.info(
        "GoMotive: v1=%d, v3=%d, merged=%d vehicles, %d at speed, %d with a "
        "fresh reading (counted as moving)",
        len(v1), len(v3), len(merged), len(moving), len(fresh),
    )

    return fresh


async def fetch_by_ids(
    vehicle_ids: List[str], token: str, base_url: str
) -> Dict[str, GoMotiveVehicle]:
    """Targeted v1 lookup for specific GoMotive vehicle ids, returned as
    {str(id): vehicle}. Used by the 2-min tracker so it re-checks only the
    handful of disconnected units instead of re-paging the whole fleet.

    v1 is used because it honours the ``vehicle_ids[]`` filter (v3 ignores it)
    and reports speed in mph directly. The endpoint caps ``vehicle_ids[]`` at
    100 per call, so ids are requested in chunks of 100.
    """
    ids = [str(v) for v in vehicle_ids if v]
    if not ids or not token:
        return {}

    headers = {
        "accept": "application/json",
        "X-Api-Key": token,
        "X-Metric-Units": "false",
    }
    url = f"{base_url.rstrip('/')}/v1/vehicle_locations"

    out: Dict[str, GoMotiveVehicle] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for start in range(0, len(ids), _MAX_IDS_PER_CALL):
            chunk = ids[start:start + _MAX_IDS_PER_CALL]
            params = [("vehicle_ids[]", vid) for vid in chunk]
            params.append(("per_page", str(len(chunk))))
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"GoMotive fetch_by_ids HTTP {resp.status}: {body}"
                    )
                data = await resp.json()
            for record in data.get("vehicles") or []:
                v = _parse_vehicle(record, "v1")
                if v is not None and v.vehicle_id is not None:
                    out[str(v.vehicle_id)] = v
    return out
