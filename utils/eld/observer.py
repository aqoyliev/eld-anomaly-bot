"""Observation-only check for an ELD that reports a position but no engine data.

The detector judges an ELD by how fresh its last report is. That misses a unit
whose ELD still pings its GPS position (fresh timestamp) while it has lost the
truck's ECM — the driver log shows no driving and the engine-hours error fires
(seen on KINGS ROAD 055 and MZ CARGO 1018, 2026-10-14).

This module only LOGS such units, tagged ``ELD-MISMATCH``; it never opens an
event or sends an alert. It compares the movement provider (moving, with a
speed) against the DriveHOS-platform ELD records (Vitality / Zero), which are
the only ones whose status feed carries speed, status and odometer:

  * status OFFLINE while its timestamp is still fresh,
  * the ELD says STATIONARY / speed 0 while the movement provider says moving,
  * the odometer unchanged for ``_ODOMETER_FROZEN_SECONDS`` while moving.

A unit is logged once it has been flagged in ``_MIN_CYCLES`` consecutive polls
(the two feeds are sampled a few seconds apart, so one cycle of disagreement is
usually just a truck pulling off), then re-logged every ``_REMIND_EVERY``
cycles, and a "cleared" line is logged when it agrees again. State is in
memory, so it resets on a restart.
"""

import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from .detector import vins_conflict

logger = logging.getLogger(__name__)

_MIN_CYCLES = 2
_REMIND_EVERY = 12
_ODOMETER_FROZEN_SECONDS = 600
# A unit not seen moving for this long starts its odometer history over.
_ODOMETER_GAP_SECONDS = 900

# (company_id, system, unit_number) -> (odometer, first seen at this value, last seen)
_odometer: Dict[Tuple[int, str, str], Tuple[float, datetime, datetime]] = {}
# (company_id, system, unit_number) -> {"count": int, "since": datetime, "logged": bool}
_flagged: Dict[Tuple[int, str, str], dict] = {}


def _reasons(
    mv: object, rec: object, key: Tuple[int, str, str], now: datetime,
) -> List[str]:
    reasons: List[str] = []
    status = getattr(rec, "status", None)
    speed = getattr(rec, "speed", None)
    if status == "OFFLINE":
        reasons.append("ELD status OFFLINE but still reporting")
    elif status == "STATIONARY" or (speed is not None and speed == 0):
        reasons.append("ELD says stationary")

    odometer = getattr(rec, "odometer", None)
    if odometer is not None:
        prev = _odometer.get(key)
        if prev and prev[0] == odometer and (now - prev[2]).total_seconds() <= _ODOMETER_GAP_SECONDS:
            frozen_for = (now - prev[1]).total_seconds()
            _odometer[key] = (odometer, prev[1], now)
            if frozen_for >= _ODOMETER_FROZEN_SECONDS:
                reasons.append(f"odometer frozen {int(frozen_for // 60)} min")
        else:
            _odometer[key] = (odometer, now, now)
    return reasons


def observe(
    company_id: int,
    company_name: str,
    moving: Iterable[object],
    *,
    vitality_lookup: Optional[Dict[str, object]] = None,
    zero_lookup: Optional[Dict[str, object]] = None,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> int:
    """Log units whose DriveHOS ELD record disagrees with the movement provider.
    Returns how many units are currently flagged for this company."""
    now = now or datetime.utcnow()
    systems = (("vitality", vitality_lookup or {}), ("zero", zero_lookup or {}))
    compared = 0
    now_flagged: Dict[Tuple[int, str, str], str] = {}

    for mv in moving:
        for system, lookup in systems:
            rec = lookup.get(mv.unit_number)
            if rec is None or vins_conflict(getattr(mv, "vin", None), getattr(rec, "vin", None)):
                continue
            reported = getattr(rec, "last_report_time", None)
            # A stale record is the detector's business, not ours.
            if reported is None or (now - reported).total_seconds() > threshold_seconds:
                continue
            compared += 1
            key = (company_id, system, mv.unit_number)
            reasons = _reasons(mv, rec, key, now)
            if not reasons:
                continue
            now_flagged[key] = (
                f"{'; '.join(reasons)} - {system}: status={rec.status} "
                f"speed={rec.speed} odometer={rec.odometer} "
                f"report {int((now - reported).total_seconds())}s old, "
                f"{getattr(mv, 'provider', 'movement')} speed={getattr(mv, 'speed', None)}"
            )

    for key, detail in now_flagged.items():
        state = _flagged.setdefault(key, {"count": 0, "since": now, "logged": False})
        state["count"] += 1
        count = state["count"]
        if count == _MIN_CYCLES or (count > _MIN_CYCLES and (count - _MIN_CYCLES) % _REMIND_EVERY == 0):
            state["logged"] = True
            logger.warning(
                "ELD-MISMATCH[%s] unit %s: %s (flagged %d cycles, since %s UTC)",
                company_name, key[2], detail, count, state["since"].strftime("%H:%M"),
            )

    for key in [k for k in _flagged if k[0] == company_id and k not in now_flagged]:
        state = _flagged.pop(key)
        if state["logged"]:
            logger.info(
                "ELD-MISMATCH[%s] unit %s (%s) cleared after %d cycles",
                company_name, key[2], key[1], state["count"],
            )

    logger.info(
        "ELD-OBS[%s]: %d unit/ELD pairs compared, %d disagree",
        company_name, compared, len(now_flagged),
    )
    return len(now_flagged)
