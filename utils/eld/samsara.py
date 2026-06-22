"""Client for the Samsara fleet API — the second supported ground-truth
movement source (some companies run Samsara devices in their trucks instead of
GoMotive). Mirrors :mod:`gomotive`'s interface so the poller/tracker can use
either provider interchangeably: ``fetch_moving_vehicles`` for the full-fleet
sweep and ``fetch_by_ids`` for targeted re-checks.

  GET /fleet/vehicles/stats?types=gps
      Bearer auth; snapshot of the latest GPS reading per vehicle.
      speed at gps.speedMilesPerHour (mph), position at gps.latitude/longitude,
      address at gps.reverseGeo.formattedLocation, reading time at gps.time.
      Cursor pagination: request param ``after``, response
      ``pagination.endCursor`` / ``pagination.hasNextPage``.
      ``vehicleIds`` (comma-separated) filters to specific vehicles.

Docs: https://developers.samsara.com/reference/getvehiclestats
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Safety cap so a misbehaving cursor (hasNextPage never false) can't loop
# forever. At the default page size (512) this allows a huge fleet.
_MAX_PAGES = 200

# Keep vehicleIds batches well under URL-length limits (Samsara ids are long
# numeric strings); 100 matches the GoMotive client's batching.
_MAX_IDS_PER_CALL = 100


@dataclass
class SamsaraVehicle:
    """Same attribute surface as :class:`gomotive.GoMotiveVehicle` — the
    poller/tracker/detector access these duck-typed."""
    unit_number: str
    vin: Optional[str]                # not in the stats feed; kept for parity
    speed: Optional[float]            # mph (Samsara reports mph directly)
    location: Optional[str]           # human-readable last known location
    time: Optional[datetime]          # timestamp of this reading
    source: str = "samsara"
    vehicle_id: Optional[str] = None  # Samsara id (for targeted re-queries)
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Stamped onto events so the tracker knows which API to re-query.
    provider = "samsara"

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


def _parse_vehicle(record: dict) -> Optional[SamsaraVehicle]:
    """One ``data[]`` item from /fleet/vehicles/stats. ``name`` is the unit
    number (same convention as GoMotive's ``number``)."""
    number = record.get("name")
    if not number:
        return None
    gps = record.get("gps") or {}
    reverse_geo = gps.get("reverseGeo") or {}
    vid = record.get("id")
    return SamsaraVehicle(
        unit_number=str(number),
        vin=None,
        speed=gps.get("speedMilesPerHour"),
        location=reverse_geo.get("formattedLocation"),
        time=_parse_time(gps.get("time")),
        vehicle_id=str(vid) if vid is not None else None,
        latitude=gps.get("latitude"),
        longitude=gps.get("longitude"),
    )


async def _fetch_stats(
    token: str, base_url: str, extra_params: Optional[List[tuple]] = None
) -> List[dict]:
    """Page through /fleet/vehicles/stats?types=gps and return the raw
    ``data`` items. Raises on HTTP errors so a token problem surfaces."""
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    url = f"{base_url.rstrip('/')}/fleet/vehicles/stats"
    timeout = aiohttp.ClientTimeout(total=30)

    out: List[dict] = []
    after: Optional[str] = None
    pages = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while pages < _MAX_PAGES:
            params: List[tuple] = [("types", "gps")] + list(extra_params or [])
            if after:
                params.append(("after", after))
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    body = " ".join((await resp.text()).split())[:160]
                    raise RuntimeError(
                        f"Samsara /fleet/vehicles/stats HTTP {resp.status}: {body}"
                    )
                data = await resp.json()

            out.extend(data.get("data") or [])
            pages += 1

            pagination = data.get("pagination") or {}
            after = pagination.get("endCursor")
            if not pagination.get("hasNextPage") or not after:
                break
        else:
            logger.warning("Samsara: hit page cap (%d); fleet may be larger.",
                           _MAX_PAGES)

    return out


def _is_fresh(v: SamsaraVehicle, max_age_seconds: Optional[int]) -> bool:
    """Whether the vehicle's GPS reading is recent enough to count as a CURRENT
    observation. The stats snapshot returns the last-known reading even for a
    gateway that died months ago (frozen at highway speed!), so a stale reading
    must not be treated as the truck moving right now. A missing reading time
    can't be confirmed current, so it is stale too."""
    if max_age_seconds is None:
        return True
    if v.time is None:
        return False
    now = datetime.now(timezone.utc)
    return (now - v.time).total_seconds() <= max_age_seconds


async def fetch_moving_vehicles(
    token: str,
    base_url: str,
    threshold_mph: float = 0.0,
    freshness_seconds: Optional[int] = None,
) -> Dict[str, SamsaraVehicle]:
    """Return {unit_number: vehicle} for vehicles moving above the threshold.

    ``freshness_seconds`` drops vehicles whose reading is older than that —
    REQUIRED for anomaly detection (the poller passes
    ``config.SAMSARA_GPS_FRESHNESS``): verified live, a fleet showed 67
    vehicles with speed > 0 of which only 7 had a current reading; the rest
    were dead/deactivated gateways frozen at speed, each a permanent false
    alarm. ``None`` (no filter) is for diagnostics like samsara_probe.
    """
    if not token:
        logger.warning("Samsara token not configured; skipping Samsara poll.")
        return {}

    records = await _fetch_stats(token, base_url)
    vehicles: Dict[str, SamsaraVehicle] = {}
    for record in records:
        v = _parse_vehicle(record)
        if v is not None:
            vehicles[v.unit_number] = v

    moving = {
        unit: v
        for unit, v in vehicles.items()
        if v.speed is not None and v.speed > threshold_mph
    }
    fresh = {
        unit: v for unit, v in moving.items() if _is_fresh(v, freshness_seconds)
    }
    logger.info(
        "Samsara: %d vehicles, %d at speed, %d with a fresh reading (counted "
        "as moving)", len(vehicles), len(moving), len(fresh),
    )
    return fresh


async def fetch_vins(
    vehicle_ids: List[str], token: str, base_url: str
) -> Dict[str, str]:
    """Return {str(id): vin} for the given vehicle ids.

    The stats feed (/fleet/vehicles/stats) doesn't carry VINs, so the poller
    enriches the moving Samsara vehicles via this separate /fleet/vehicles
    lookup. The VIN is what lets detection tell two trucks that share a unit
    number apart (compared against the Quantum VIN). Vehicles without a VIN on
    record are simply omitted (the detector falls back to the unit-number match
    when a VIN is missing)."""
    ids = [str(v) for v in vehicle_ids if v]
    if not ids or not token:
        return {}

    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    url = f"{base_url.rstrip('/')}/fleet/vehicles"
    timeout = aiohttp.ClientTimeout(total=30)

    out: Dict[str, str] = {}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for start in range(0, len(ids), _MAX_IDS_PER_CALL):
            chunk = ids[start:start + _MAX_IDS_PER_CALL]
            after: Optional[str] = None
            pages = 0
            while pages < _MAX_PAGES:
                params: List[tuple] = [("ids", ",".join(chunk))]
                if after:
                    params.append(("after", after))
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        body = " ".join((await resp.text()).split())[:160]
                        raise RuntimeError(
                            f"Samsara /fleet/vehicles HTTP {resp.status}: {body}"
                        )
                    data = await resp.json()
                for record in data.get("data") or []:
                    vid = record.get("id")
                    vin = record.get("vin")
                    if vid is not None and vin:
                        out[str(vid)] = str(vin)
                pages += 1
                pagination = data.get("pagination") or {}
                after = pagination.get("endCursor")
                if not pagination.get("hasNextPage") or not after:
                    break
    return out


async def fetch_by_ids(
    vehicle_ids: List[str], token: str, base_url: str
) -> Dict[str, SamsaraVehicle]:
    """Targeted lookup for specific Samsara vehicle ids, returned as
    {str(id): vehicle}. Used by the 2-min tracker so it re-checks only the
    handful of disconnected units instead of the whole fleet."""
    ids = [str(v) for v in vehicle_ids if v]
    if not ids or not token:
        return {}

    out: Dict[str, SamsaraVehicle] = {}
    for start in range(0, len(ids), _MAX_IDS_PER_CALL):
        chunk = ids[start:start + _MAX_IDS_PER_CALL]
        records = await _fetch_stats(
            token, base_url, extra_params=[("vehicleIds", ",".join(chunk))]
        )
        for record in records:
            v = _parse_vehicle(record)
            if v is not None and v.vehicle_id is not None:
                out[v.vehicle_id] = v
    return out
