"""Cross-reference movement-provider data against ELD report freshness.

Flow: the movement provider (GoMotive or Samsara, per company) tells us which
vehicles are moving; for each we have already looked up its ELD record(s) by
unit number — Quantum, EVO, or both (a company may run a mixed ELD fleet).

Quantum is authoritative wherever it holds the truck: an anomaly is a moving
vehicle whose Quantum record is stale, and a fresh EVO reading must NOT
suppress that — trucks can carry both devices, and a driver unplugging the
Quantum ELD while the EVO box keeps reporting is exactly the fraud this bot
exists to catch (real case: MZ CARGO unit 218118, rolling with Quantum dark
for hours while fresh on EVO). EVO's staleness decides only for trucks
Quantum doesn't hold under that (VIN-plausible) unit number.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from . import quantumeld
from .quantumeld import QuantumVehicle

logger = logging.getLogger(__name__)

# Two VINs are treated as the SAME truck if they differ by at most this many
# single-character edits (Damerau-Levenshtein, so a transposition counts as 1).
# Real fleets carry occasional VIN-entry typos between Quantum and the movement
# provider — a transposition (unit 1137: "…MD…" vs "…DM…") or a mistyped digit
# — so an exact-match requirement would drop genuine anomalies. Anything beyond
# this is a different vehicle that merely shares a unit number.
_VIN_MISMATCH_TOLERANCE = 2


@dataclass
class Anomaly:
    unit_number: str
    # QuantumVehicle or EvoVehicle — both expose the same attribute surface
    # (vin, driver, last_report_time), so downstream code is duck-typed. The
    # authoritative record for the unit: Quantum's when it holds the truck,
    # else EVO's.
    eld: object
    # Which ELD system the record above came from ("quantum"/"evo").
    eld_provider: str
    # GoMotiveVehicle or SamsaraVehicle — both expose the same attribute
    # surface (speed, latitude/longitude, vehicle_id, coordinates_label,
    # provider), so downstream code is duck-typed.
    movement: object


def _normalize_vin(vin: Optional[str]) -> str:
    """Upper-case and strip everything but A-Z/0-9 so cosmetic differences
    (spaces, hyphens, case) don't read as a VIN conflict."""
    if not vin:
        return ""
    return re.sub(r"[^A-Z0-9]", "", vin.upper())


def _edit_distance(a: str, b: str, *, cap: int) -> int:
    """Damerau-Levenshtein distance, short-circuiting once it exceeds ``cap``
    (we only care whether two VINs are near-identical, not the exact distance)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev_prev: List[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            val = min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
            if (
                i > 1 and j > 1
                and ca == b[j - 2] and a[i - 2] == cb
            ):
                val = min(val, prev_prev[j - 2] + 1)  # transposition
            cur[j] = val
            row_min = min(row_min, val)
        if row_min > cap:
            return cap + 1
        prev_prev, prev = prev, cur
    return prev[len(b)]


def vins_conflict(a: Optional[str], b: Optional[str]) -> bool:
    """True only when both VINs are present AND clearly belong to different
    vehicles. A missing VIN on either side is not a conflict — VIN is a
    disambiguation guard layered on top of the unit-number match, never the
    primary key, so we never reject a match for lack of VIN data."""
    na, nb = _normalize_vin(a), _normalize_vin(b)
    if not na or not nb:
        return False
    if na == nb:
        return False
    return _edit_distance(na, nb, cap=_VIN_MISMATCH_TOLERANCE) > _VIN_MISMATCH_TOLERANCE


def find_anomalies(
    moving: Iterable[object],
    quantum_lookup: Dict[str, Optional[QuantumVehicle]],
    *,
    evo_lookup: Optional[Dict[str, object]] = None,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> List[Anomaly]:
    """``moving`` is an iterable of movement vehicles (each exposes
    ``unit_number``/``vin``); ``quantum_lookup``/``evo_lookup`` map unit numbers
    to that system's record (or None if not found there).

    A list rather than a unit-keyed dict because two different trucks can share
    a unit number across providers (GoMotive vs Samsara) — both must be checked,
    and the VIN comparison below decides which one (if either) is the truck
    the ELD system holds under that number.

    ``now`` defaults to UTC, matching the (naive) UTC report times."""
    now = now or datetime.utcnow()
    evo_lookup = evo_lookup or {}
    anomalies: List[Anomaly] = []
    for mv in moving:
        unit_number = mv.unit_number
        candidates = [
            (system, rec)
            for system, rec in (
                ("quantum", quantum_lookup.get(unit_number)),
                ("evo", evo_lookup.get(unit_number)),
            )
            if rec is not None
        ]
        if not candidates:
            continue
        # Same unit number, different truck: fleets reuse unit numbers (and two
        # providers can each have a different truck under one number), so the
        # ELD record found by unit number can belong to a DIFFERENT vehicle
        # than this one. Comparing VINs (Motive/Samsara vs the ELD) catches that
        # collision so we don't flag — or read staleness off — the wrong truck.
        # VIN stays a guard on top of the unit-number match: only a clear
        # mismatch is rejected.
        matching = []
        for system, rec in candidates:
            if vins_conflict(getattr(mv, "vin", None), rec.vin):
                logger.info(
                    "Skipping %s: VIN mismatch (provider %s vs %s %s) — "
                    "same unit number, different truck.",
                    unit_number, getattr(mv, "vin", None), system, rec.vin,
                )
                continue
            matching.append((system, rec))
        if not matching:
            continue
        # Quantum is authoritative when it holds the truck: its staleness
        # alone decides, and a fresh EVO reading must not suppress it — a
        # truck can carry both devices, and Quantum going dark while the EVO
        # box keeps reporting is precisely the disconnection to flag (unit
        # 218118). EVO decides only for trucks Quantum doesn't hold.
        # ``candidates`` lists quantum first, so the first match wins.
        system, rec = matching[0]
        if quantumeld.is_disconnected(
            rec, threshold_seconds=threshold_seconds, now=now
        ):
            anomalies.append(Anomaly(unit_number, rec, system, mv))
    return anomalies
