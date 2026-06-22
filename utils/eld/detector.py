"""Cross-reference movement-provider data against Quantum ELD report freshness.

Flow: the movement provider (GoMotive or Samsara, per company) tells us which
vehicles are moving; for each we have already looked up its Quantum record (by
unit number). An anomaly is a vehicle that is moving on the provider while its
Quantum ELD report is stale (disconnected).
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

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
    quantum: QuantumVehicle
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
    moving: Dict[str, object],
    quantum_lookup: Dict[str, Optional[QuantumVehicle]],
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> List[Anomaly]:
    """``moving`` is keyed by the movement provider's unit number;
    ``quantum_lookup`` maps the same unit numbers to their Quantum record (or
    None if not found).

    ``now`` defaults to UTC, matching Quantum's (naive) UTC report times."""
    now = now or datetime.utcnow()
    anomalies: List[Anomaly] = []
    for unit_number, mv in moving.items():
        q = quantum_lookup.get(unit_number)
        if q is None:
            continue
        # Same unit number, different truck: fleets reuse unit numbers, so the
        # Quantum record found by unit number can belong to a DIFFERENT vehicle
        # than the one the movement provider reports moving. Comparing VINs
        # (Motive/Samsara vs Quantum) catches that collision so we don't flag —
        # or read staleness off — the wrong truck. VIN stays a guard on top of
        # the unit-number match: only a clear mismatch is rejected.
        if vins_conflict(getattr(mv, "vin", None), q.vin):
            logger.info(
                "Skipping %s: VIN mismatch (provider %s vs Quantum %s) — "
                "same unit number, different truck.",
                unit_number, getattr(mv, "vin", None), q.vin,
            )
            continue
        if quantumeld.is_disconnected(q, threshold_seconds=threshold_seconds, now=now):
            anomalies.append(Anomaly(unit_number, q, mv))
    return anomalies
