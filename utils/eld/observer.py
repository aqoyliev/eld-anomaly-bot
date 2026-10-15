"""Spot an ELD that reports a position but records no engine data.

The detector judges an ELD by how fresh its last report is. That misses a unit
whose ELD still pings its GPS position (fresh timestamp) while it has lost the
truck's ECM — the driver log shows no driving and the engine-hours error fires
(seen on KINGS ROAD 055 and MZ CARGO 1018, 2026-10-14, and on ~9 units over one
day of logs afterwards).

It compares the movement provider (moving, with a speed) against the
DriveHOS-platform ELD records (Vitality / Zero), the only ones whose status feed
carries speed, status and odometer. Every disagreement is LOGGED, tagged
``ELD-MISMATCH``:

  * status OFFLINE while its timestamp is still fresh (a precursor of the stale
    check — log only),
  * the ELD says STATIONARY / speed 0 while the movement provider says moving,
  * the odometer unchanged for ``_ODOMETER_FROZEN_SECONDS`` while moving (log
    only: some devices report a frozen odometer while their speed is right).

Only the strong case — the ELD stationary while the movement provider shows the
truck at ``config.NO_DATA_MIN_SPEED`` mph or more — becomes an alert, and only
after it has held for ``config.NO_DATA_ALERT_CYCLES`` consecutive polls (the two
feeds are sampled seconds apart, so one cycle of disagreement is usually just a
truck pulling off). :func:`observe` returns those units; the poller opens the
events.

A unit is a candidate only if EVERY fresh, VIN-plausible record that knows it
agrees it is stationary and no fresh Quantum/EVO record holds it either (their
feeds carry no speed, so they cannot confirm the truck is not recording).

A unit is logged once flagged in ``_MIN_CYCLES`` consecutive polls, re-logged
every ``_REMIND_EVERY`` cycles, and a "cleared" line is logged when it agrees
again. State is in memory, so it resets on a restart.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from data import config
from .detector import vins_conflict
from .quantumeld import is_disconnected

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
# (company_id, unit_number) -> consecutive polls in the strong "not recording" state
_strong: Dict[Tuple[int, str], int] = {}


@dataclass
class NoDataAnomaly:
    """A moving unit whose fresh ELD record says it is stationary."""
    unit_number: str
    eld: object          # the VitalityVehicle record (vin, driver, speed, ...)
    eld_provider: str    # "vitality" / "zero"
    movement: object     # GoMotiveVehicle / SamsaraVehicle
    cycles: int          # consecutive polls it has held


def _is_stationary(rec: object) -> bool:
    """The ELD says the truck is not moving (OFFLINE is not "stationary": that
    is the stale check's business)."""
    status = getattr(rec, "status", None)
    speed = getattr(rec, "speed", None)
    if status == "OFFLINE":
        return False
    return status == "STATIONARY" or (speed is not None and speed == 0)


def _reasons(
    rec: object, key: Tuple[int, str, str], now: datetime,
) -> List[str]:
    reasons: List[str] = []
    status = getattr(rec, "status", None)
    if status == "OFFLINE":
        reasons.append("ELD status OFFLINE but still reporting")
    elif _is_stationary(rec):
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


def _fresh(rec: object, threshold_seconds: int, now: datetime) -> bool:
    reported = getattr(rec, "last_report_time", None)
    return reported is not None and (now - reported).total_seconds() <= threshold_seconds


def observe(
    company_id: int,
    company_name: str,
    moving: Iterable[object],
    *,
    vitality_lookup: Optional[Dict[str, object]] = None,
    zero_lookup: Optional[Dict[str, object]] = None,
    quantum_lookup: Optional[Dict[str, object]] = None,
    evo_lookup: Optional[Dict[str, object]] = None,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> List[NoDataAnomaly]:
    """Log units whose DriveHOS ELD record disagrees with the movement provider
    and return the ones that have held the strong "not recording" state long
    enough to alert on."""
    now = now or datetime.utcnow()
    quantum_lookup = quantum_lookup or {}
    evo_lookup = evo_lookup or {}
    systems = (("vitality", vitality_lookup or {}), ("zero", zero_lookup or {}))
    compared = 0
    now_flagged: Dict[Tuple[int, str, str], str] = {}
    strong_now: Dict[Tuple[int, str], NoDataAnomaly] = {}

    for mv in moving:
        plausible: List[Tuple[str, object]] = []
        for system, lookup in systems:
            rec = lookup.get(mv.unit_number)
            if rec is None or vins_conflict(getattr(mv, "vin", None), getattr(rec, "vin", None)):
                continue
            # A stale record is the detector's business, not ours.
            if not _fresh(rec, threshold_seconds, now):
                continue
            plausible.append((system, rec))
            compared += 1
            key = (company_id, system, mv.unit_number)
            reasons = _reasons(rec, key, now)
            if not reasons:
                continue
            now_flagged[key] = (
                f"{'; '.join(reasons)} - {system}: status={rec.status} "
                f"speed={rec.speed} odometer={rec.odometer} "
                f"report {int((now - rec.last_report_time).total_seconds())}s old, "
                f"{getattr(mv, 'provider', 'movement')} speed={getattr(mv, 'speed', None)}"
            )

        if not plausible:
            continue
        # Strong case: every plausible DriveHOS record says stationary, the
        # truck is clearly driving, and no speedless Quantum/EVO record that is
        # fresh could say otherwise.
        speed = getattr(mv, "speed", None)
        other_fresh = any(
            rec is not None
            and not vins_conflict(getattr(mv, "vin", None), getattr(rec, "vin", None))
            and not is_disconnected(rec, threshold_seconds=threshold_seconds, now=now)
            for rec in (quantum_lookup.get(mv.unit_number), evo_lookup.get(mv.unit_number))
        )
        if (
            speed is not None and speed >= config.NO_DATA_MIN_SPEED
            and all(_is_stationary(rec) for _, rec in plausible)
            and not other_fresh
        ):
            system, rec = plausible[0]
            strong_now[(company_id, mv.unit_number)] = NoDataAnomaly(
                unit_number=mv.unit_number, eld=rec, eld_provider=system,
                movement=mv, cycles=0,
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

    # Consecutive-cycle counter for the alert case; a unit that drops out of the
    # strong state (or out of the moving list) starts over.
    for key in [k for k in _strong if k[0] == company_id and k not in strong_now]:
        del _strong[key]
    candidates: List[NoDataAnomaly] = []
    for key, item in strong_now.items():
        _strong[key] = _strong.get(key, 0) + 1
        item.cycles = _strong[key]
        if item.cycles >= config.NO_DATA_ALERT_CYCLES:
            candidates.append(item)

    logger.info(
        "ELD-OBS[%s]: %d unit/ELD pairs compared, %d disagree, %d not recording",
        company_name, compared, len(now_flagged), len(candidates),
    )
    return candidates
