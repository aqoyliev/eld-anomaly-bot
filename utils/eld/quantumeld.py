"""Client for the Quantum ELD vehicle-location API.

The bot drives detection from GoMotive: for each vehicle GoMotive reports as
moving, we look it up here by unit number (``GET /vehicles/{unit_number}``) to
read its last Quantum ELD report time. A report older than
``ELD_STALE_THRESHOLD`` means the ELD looks disconnected / offline.

Quantum also exposes a list endpoint (``GET /vehicles/current``), but the design
stays GoMotive-first → per-unit lookups by choice: detection always needs
GoMotive's ground-truth movement/speed to compare against ELD freshness, so we
only look up the (few) units GoMotive reports moving rather than paging the
whole fleet.

GoMotive appends owner/tag suffixes to unit numbers (e.g. "0942  O/O") while
Quantum stores the bare number ("0942"), so we strip the suffix before looking
up — see :func:`quantum_key`.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class QuantumVehicle:
    unit_number: str
    vin: Optional[str]
    driver: Optional[str]
    state: Optional[str]
    location: Optional[str]
    last_report_time: Optional[datetime]
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @property
    def location_label(self) -> str:
        parts = [p for p in (self.location, self.state) if p]
        return ", ".join(parts) if parts else "unknown"

    @property
    def coordinates_label(self) -> str:
        """Quantum's last-reported lat,long (the point where the ELD went
        dark). Falls back to the place name if coordinates are missing."""
        if self.latitude is not None and self.longitude is not None:
            return f"{self.latitude}, {self.longitude}"
        return self.location_label


def quantum_key(unit_number: str) -> str:
    """Derive the bare unit number Quantum stores from a GoMotive unit number.

    "0942  O/O" -> "0942", "2512 O/O" -> "2512", "002  ZM" -> "002",
    "1277  CROSS USA" -> "1277", "1137" -> "1137",
    "unit 775263 CARLOS PEREZ" -> "775263",
    "unit 228005 IVAN AGUILAR/JORGE ESPEJO" -> "228005".
    A wide (2+ space) gap usually separates the number from a tag; otherwise, a
    leading numeric token followed by a tag/driver name is reduced to just the
    number. Some providers also prefix the literal word "unit", which is dropped.
    """
    u = unit_number.strip()
    # Drop a literal "unit" prefix some providers prepend (e.g. "unit 1013 ...").
    u = re.sub(r"^unit\s+", "", u, flags=re.IGNORECASE)
    head = re.split(r"\s{2,}", u)[0].strip()  # drop tag after a wide gap
    tokens = head.split()
    if len(tokens) > 1 and tokens[0].isdigit():
        return tokens[0]  # e.g. "2512 O/O" -> "2512", "228005 IVAN/JORGE" -> "228005"
    return head


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Quantum: could not parse time %r", value)
        return None


def _parse_vehicle(content: dict) -> Optional[QuantumVehicle]:
    vehicle = content.get("vehicle") or {}
    unit_number = vehicle.get("unit_number")
    if not unit_number:
        return None
    location = content.get("location") or {}
    driver = (content.get("driver") or {}).get("name")
    coords = location.get("vehicle_coordinates") or {}
    return QuantumVehicle(
        unit_number=str(unit_number),
        vin=vehicle.get("vin"),
        driver=driver,
        state=location.get("state"),
        location=location.get("location"),
        last_report_time=_parse_time(location.get("time")),
        latitude=coords.get("latitude"),
        longitude=coords.get("longitude"),
    )


async def fetch_vehicle(
    session: aiohttp.ClientSession, unit_number: str, token: str, base_url: str
) -> Optional[QuantumVehicle]:
    """Look up one vehicle by unit number. Returns None if Quantum has no
    record for it (HTTP 200 with content=null). Raises on auth/HTTP errors so a
    token problem surfaces instead of silently yielding zero anomalies."""
    key = quantum_key(unit_number)
    # safe="" so a "/" in the key escapes to %2F instead of splitting the path
    # (e.g. dual-driver names like "IVAN AGUILAR/JORGE ESPEJO" -> a bogus 401).
    url = f"{base_url.rstrip('/')}/vehicles/{quote(key, safe='')}"
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {token}",
    }
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            # Truncate the body — upstream 5xx pages are full HTML and flood logs.
            body = " ".join((await resp.text()).split())[:160]
            raise RuntimeError(f"Quantum /vehicles/{key} HTTP {resp.status}: {body}")
        data = await resp.json()

    code = (data.get("result") or {}).get("code")
    if code != "ok":
        description = (data.get("result") or {}).get("description", code)
        raise RuntimeError(f"Quantum /vehicles/{key} error: {code} ({description})")

    content = data.get("content")
    if not content:
        return None  # token is fine, this unit just isn't in Quantum
    return _parse_vehicle(content)


async def fetch_vehicles(
    unit_numbers: List[str], token: str, base_url: str, concurrency: int = 10
) -> Dict[str, Optional[QuantumVehicle]]:
    """Look up many vehicles concurrently. Returns {original_unit_number: vehicle
    or None}, keyed by the GoMotive unit number that was passed in.

    A per-unit failure (e.g. a transient Quantum ``502``/timeout on one
    vehicle) is swallowed and that unit is recorded as ``None`` for this cycle,
    so a single bad response can't abort the whole company's poll/track pass.
    ``None`` is the safe default everywhere: the poller treats it as "not in
    Quantum" (skipped, no false anomaly) and the tracker leaves the unit
    flagged (no false resolve). If *every* unit fails it's logged at error level,
    since that points at a token/endpoint problem rather than a blip."""
    results: Dict[str, Optional[QuantumVehicle]] = {}
    failures: Dict[str, Exception] = {}
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def one(unit: str) -> None:
            async with sem:
                try:
                    results[unit] = await fetch_vehicle(session, unit, token, base_url)
                except Exception as err:  # noqa: BLE001 — isolate one unit's failure
                    results[unit] = None
                    failures[unit] = err

        await asyncio.gather(*(one(u) for u in unit_numbers))

    if failures:
        sample = "; ".join(f"{u} ({failures[u]})" for u in list(failures)[:3])
        if len(failures) == len(unit_numbers):
            logger.error(
                "Quantum lookup failed for ALL %d unit(s) this cycle — likely a "
                "token or endpoint problem. Sample: %s", len(failures), sample,
            )
        else:
            logger.warning(
                "Quantum lookup failed for %d/%d unit(s) this cycle (treated as "
                "not-found): %s", len(failures), len(unit_numbers), sample,
            )

    return results


def is_disconnected(
    vehicle: QuantumVehicle,
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """True if the vehicle's last Quantum report is older than the threshold
    (i.e. the ELD looks disconnected/offline).

    Quantum report times are UTC (naive), so compare against UTC now."""
    if vehicle.last_report_time is None:
        return False
    now = now or datetime.utcnow()
    return (now - vehicle.last_report_time).total_seconds() >= threshold_seconds
