"""SQLite-backed store for ELD-disconnection anomaly events.

An "anomaly event" is a single continuous period during which a vehicle is
disconnected on GreenLight ELD while still moving on GoMotive. While the event
is ongoing it stays ``resolved = 0`` (an *active*/flagged vehicle); once the
vehicle stops being anomalous it is marked resolved. This keeps a single row
per ongoing event, which is what gives us alert de-duplication.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from data import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number         TEXT    NOT NULL,
    vin                 TEXT,
    driver              TEXT,
    eld_disconnect_time TEXT,            -- last GreenLight report time (ISO)
    first_detected      TEXT    NOT NULL,-- when we first flagged the anomaly
    last_seen           TEXT    NOT NULL,-- last poll the anomaly was still true
    last_speed          REAL,
    last_location       TEXT,
    resolved            INTEGER NOT NULL DEFAULT 0,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active
    ON events (unit_number, resolved);
"""


@dataclass
class AnomalyEvent:
    id: int
    unit_number: str
    vin: Optional[str]
    driver: Optional[str]
    eld_disconnect_time: Optional[str]
    first_detected: str
    last_seen: str
    last_speed: Optional[float]
    last_location: Optional[str]
    resolved: int
    resolved_at: Optional[str]

    @property
    def disconnect_dt(self) -> Optional[datetime]:
        return _parse(self.eld_disconnect_time)

    def duration_seconds(self, now: Optional[datetime] = None) -> int:
        """Length of the anomaly, from the ELD disconnect time to now (or to
        resolution if already resolved)."""
        start = self.disconnect_dt or _parse(self.first_detected)
        if start is None:
            return 0
        end = _parse(self.resolved_at) if self.resolved else (now or datetime.utcnow())
        return max(0, int((end - start).total_seconds()))


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _row_to_event(row: sqlite3.Row) -> AnomalyEvent:
    return AnomalyEvent(**{k: row[k] for k in row.keys()})


def get_active_event(unit_number: str) -> Optional[AnomalyEvent]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE unit_number = ? AND resolved = 0 "
            "ORDER BY id DESC LIMIT 1",
            (unit_number,),
        ).fetchone()
    return _row_to_event(row) if row else None


def open_event(
    *,
    unit_number: str,
    vin: Optional[str],
    driver: Optional[str],
    eld_disconnect_time: Optional[str],
    speed: Optional[float],
    location: Optional[str],
) -> AnomalyEvent:
    """Create a new active anomaly event and return it (caller then alerts)."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (unit_number, vin, driver, eld_disconnect_time, "
            "first_detected, last_seen, last_speed, last_location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (unit_number, vin, driver, eld_disconnect_time, now, now, speed, location),
        )
        event_id = cur.lastrowid
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_event(row)


def touch_event(
    event_id: int,
    *,
    speed: Optional[float],
    location: Optional[str],
) -> None:
    """Update an ongoing event with the latest reading (no new alert)."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE events SET last_seen = ?, last_speed = ?, last_location = ? "
            "WHERE id = ?",
            (now, speed, location, event_id),
        )


def resolve_event(event_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE events SET resolved = 1, resolved_at = ? WHERE id = ?",
            (now, event_id),
        )


def get_active_events() -> List[AnomalyEvent]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE resolved = 0 ORDER BY first_detected DESC"
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def active_unit_numbers() -> set:
    return {e.unit_number for e in get_active_events()}


def get_recent_events(limit: int = 20) -> List[AnomalyEvent]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY first_detected DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_event(r) for r in rows]
