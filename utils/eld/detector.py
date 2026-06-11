"""Cross-reference movement-provider data against Quantum ELD report freshness.

Flow: the movement provider (GoMotive or Samsara, per company) tells us which
vehicles are moving; for each we have already looked up its Quantum record (by
unit number). An anomaly is a vehicle that is moving on the provider while its
Quantum ELD report is stale (disconnected).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from . import quantumeld
from .quantumeld import QuantumVehicle


@dataclass
class Anomaly:
    unit_number: str
    quantum: QuantumVehicle
    # GoMotiveVehicle or SamsaraVehicle — both expose the same attribute
    # surface (speed, latitude/longitude, vehicle_id, coordinates_label,
    # provider), so downstream code is duck-typed.
    movement: object


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
        if quantumeld.is_disconnected(q, threshold_seconds=threshold_seconds, now=now):
            anomalies.append(Anomaly(unit_number, q, mv))
    return anomalies
