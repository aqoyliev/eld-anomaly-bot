"""Client for the EVO ELD tracking API (read.evoeld.com).

EVO is a second ELD-side provider, playing the same role as Quantum: the
freshness of a unit's last report decides whether its ELD looks disconnected.
A company may run Quantum, EVO, or both (mixed fleet) — Quantum stays
authoritative for trucks it holds; EVO's staleness decides only for units
Quantum doesn't know (see detector.find_anomalies for the precedence rule).

Unlike Quantum's per-unit lookups, EVO returns the whole fleet in one call
(``GET /units-by-usdot/{usdot}``), so a poll cycle costs a single request.
Auth is two headers: ``x-api-key`` plus ``provider-token``; the carrier's
USDOT number goes in the URL path.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp

from .quantumeld import quantum_key

logger = logging.getLogger(__name__)


@dataclass
class EvoVehicle:
    unit_number: str
    vin: Optional[str]
    last_report_time: Optional[datetime]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # EVO's tracking feed carries no driver name; the field exists for parity
    # with QuantumVehicle so downstream code (poller/detector) stays duck-typed.
    driver: Optional[str] = None

    @property
    def coordinates_label(self) -> str:
        """EVO's last-reported lat,lng (the point where the ELD went dark)."""
        if self.latitude is not None and self.longitude is not None:
            return f"{self.latitude}, {self.longitude}"
        return "unknown"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """Parse EVO's ISO timestamp into naive UTC (the codebase-wide convention:
    all staleness math is naive-UTC). An offset-aware value ('Z' or +hh:mm) is
    converted; a naive one is assumed to already be UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("EVO: could not parse time %r", value)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_unit(raw: dict) -> Optional[EvoVehicle]:
    number = raw.get("truck_number")
    if not number:
        return None
    coords = raw.get("coordinates") or {}
    return EvoVehicle(
        unit_number=str(number),
        vin=raw.get("vin"),
        last_report_time=_parse_time(raw.get("timestamp")),
        latitude=coords.get("lat"),
        longitude=coords.get("lng"),
    )


async def fetch_units(
    api_key: str, provider_token: str, usdot: str, base_url: str
) -> Dict[str, EvoVehicle]:
    """Fetch the full EVO fleet snapshot for a carrier, keyed by NORMALIZED
    unit number (:func:`quantum_key`, same normalization applied to movement-
    provider unit numbers before matching).

    Raises on HTTP/auth errors so a credential problem surfaces instead of
    silently yielding zero anomalies."""
    url = f"{base_url.rstrip('/')}/units-by-usdot/{usdot}"
    headers = {
        "accept": "application/json",
        "x-api-key": api_key,
        "provider-token": provider_token,
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                # Truncate the body — error pages can be full HTML.
                body = " ".join((await resp.text()).split())[:160]
                raise RuntimeError(
                    f"EVO /units-by-usdot/{usdot} HTTP {resp.status}: {body}"
                )
            data = await resp.json(content_type=None)

    units: Dict[str, EvoVehicle] = {}
    for raw in data.get("units") or []:
        vehicle = _parse_unit(raw)
        if vehicle is not None:
            units[quantum_key(vehicle.unit_number)] = vehicle
    return units
