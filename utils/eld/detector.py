"""Cross-reference GoMotive movement against GreenLight ELD report freshness.

Flow: GoMotive tells us which vehicles are moving; for each we have already
looked up its GreenLight record (by unit number). An anomaly is a vehicle that
is moving on GoMotive while its GreenLight ELD report is stale (disconnected).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from . import greenlight
from .gomotive import GoMotiveVehicle
from .greenlight import GreenLightVehicle


@dataclass
class Anomaly:
    unit_number: str
    greenlight: GreenLightVehicle
    gomotive: GoMotiveVehicle


def find_anomalies(
    moving: Dict[str, GoMotiveVehicle],
    gl_lookup: Dict[str, Optional[GreenLightVehicle]],
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> List[Anomaly]:
    """``moving`` is keyed by GoMotive unit number; ``gl_lookup`` maps the same
    unit numbers to their GreenLight record (or None if not found).

    ``now`` defaults to UTC, matching GreenLight's (naive) UTC report times."""
    now = now or datetime.utcnow()
    anomalies: List[Anomaly] = []
    for unit_number, gm in moving.items():
        gl = gl_lookup.get(unit_number)
        if gl is None:
            continue
        if greenlight.is_disconnected(gl, threshold_seconds=threshold_seconds, now=now):
            anomalies.append(Anomaly(unit_number, gl, gm))
    return anomalies
