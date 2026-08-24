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


class QuantumError(RuntimeError):
    """A Quantum request that did not come back as a usable 200/ok."""


class QuantumAuthError(QuantumError):
    """Quantum answered HTTP 401 ``invalid_token``.

    This is *not* proof that the token is bad. Quantum also answers 401 for a
    vehicle key it does not recognise (rather than 404, or 200 with
    ``content: null``), so a per-unit 401 usually just means "no such unit in
    this account". Only a 401 from the fleet-list endpoint proves the token
    itself is being rejected — see :func:`token_accepted`.
    """


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
    "unit 228005 IVAN AGUILAR/JORGE ESPEJO" -> "228005",
    "147109." -> "147109",
    "UTNI: 2631 GUELSO DORCANT / ALCE GARRY" -> "2631".
    A wide (2+ space) gap usually separates the number from a tag; otherwise, a
    leading numeric token followed by a tag/driver name is reduced to just the
    number. Some providers also prefix the literal word "unit", which is dropped.
    A trailing dot (e.g. "147109.") is stray punctuation and is stripped too.
    """
    u = unit_number.strip()
    # Drop a literal "unit" prefix some providers prepend (e.g. "unit 1013 ...").
    u = re.sub(r"^unit\s+", "", u, flags=re.IGNORECASE)
    head = re.split(r"\s{2,}", u)[0].strip()  # drop tag after a wide gap
    tokens = head.split()
    # Strip a trailing dot so "147109." matches Quantum's bare "147109".
    if len(tokens) > 1 and tokens[0].rstrip(".").isdigit():
        return tokens[0].rstrip(".")  # e.g. "2512 O/O" -> "2512", "228005 IVAN/JORGE" -> "228005"
    head = head.rstrip(".")
    if head.isdigit():
        return head
    # The number is not the leading token: the "unit" prefix is misspelled or
    # punctuated ("UTNI: 2631 ...", "TRUCK #418"), so the rules above leave the
    # whole driver-name label in place and it gets sent to Quantum as the unit
    # number. Fall back to the first run of digits anywhere in the label.
    match = re.search(r"\d+", u)
    return match.group(0) if match else head


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
    """Look up one vehicle by unit number. Returns None when Quantum answers
    200 with content=null. Raises :class:`QuantumError` on any other outcome so
    a real problem surfaces instead of silently yielding zero anomalies.

    Note that "not in Quantum" reaches us two different ways: the 200/null
    above, and a 401 :class:`QuantumAuthError` for an unrecognised unit number.
    :func:`fetch_vehicles` sorts out which 401s mean what."""
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
            message = f"Quantum /vehicles/{key} HTTP {resp.status}: {body}"
            if resp.status == 401:
                raise QuantumAuthError(message)
            raise QuantumError(message)
        data = await resp.json()

    code = (data.get("result") or {}).get("code")
    if code != "ok":
        description = (data.get("result") or {}).get("description", code)
        raise QuantumError(f"Quantum /vehicles/{key} error: {code} ({description})")

    content = data.get("content")
    if not content:
        return None  # token is fine, this unit just isn't in Quantum
    return _parse_vehicle(content)


async def token_accepted(
    session: aiohttp.ClientSession, token: str, base_url: str
) -> bool:
    """True if Quantum accepts the token itself.

    Asks the fleet-list endpoint, which does not depend on any one unit number
    existing. This is the only way to tell a genuinely dead credential apart
    from a cycle where every unit looked up simply is not in Quantum — both
    look identical at the per-unit level, since Quantum answers 401 for either.
    A network error returns True: a timeout is not evidence of a bad token."""
    url = f"{base_url.rstrip('/')}/vehicles/current"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    try:
        async with session.get(url, headers=headers) as resp:
            return resp.status != 401
    except Exception:  # noqa: BLE001 — a blip must not be reported as auth failure
        logger.debug("Quantum: token check failed to reach %s", url, exc_info=True)
        return True


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
    flagged (no false resolve).

    Failures are only logged at error level once they are shown to be real. A
    401 on its own means nothing — Quantum returns it for an unknown unit
    number as readily as for a bad token — so an all-401 cycle is confirmed
    against :func:`token_accepted` before it is reported as a token problem."""
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
        auth_only = all(
            isinstance(err, QuantumAuthError) for err in failures.values()
        )
        if not auth_only:
            level = logger.error if len(failures) == len(unit_numbers) else logger.warning
            level(
                "Quantum lookup failed for %d/%d unit(s) this cycle (treated as "
                "not-found): %s", len(failures), len(unit_numbers), sample,
            )
        elif len(failures) < len(unit_numbers):
            # Other units resolved on this same token, so these 401s are
            # unknown unit numbers, not a credential problem.
            logger.info(
                "Quantum: %d/%d unit(s) not in Quantum this cycle (401 on an "
                "unrecognised unit number): %s",
                len(failures), len(unit_numbers), sample,
            )
        else:
            # Every unit 401'd. Dead token, or a company whose moving units are
            # all absent from Quantum? Ask the fleet-list endpoint rather than
            # guessing — guessing here is what produced false token alarms.
            async with aiohttp.ClientSession(timeout=timeout) as session:
                token_ok = await token_accepted(session, token, base_url)
            if token_ok:
                logger.info(
                    "Quantum: none of this cycle's %d unit(s) are in Quantum "
                    "(the token itself is accepted): %s", len(failures), sample,
                )
            else:
                logger.error(
                    "Quantum token rejected — every lookup this cycle returned 401 "
                    "and the fleet-list endpoint refuses the token too. Sample: %s",
                    sample,
                )

    return results


def is_disconnected(
    vehicle,
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """True if the vehicle's last ELD report is older than the threshold
    (i.e. the ELD looks disconnected/offline). Duck-typed on
    ``last_report_time``, so it accepts a QuantumVehicle or an EvoVehicle —
    both ELD-side systems share this staleness rule.

    Report times are UTC (naive), so compare against UTC now."""
    if vehicle.last_report_time is None:
        return False
    now = now or datetime.utcnow()
    return (now - vehicle.last_report_time).total_seconds() >= threshold_seconds
