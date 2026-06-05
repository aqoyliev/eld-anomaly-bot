"""Async store for ELD-disconnection anomaly events.

An "anomaly event" is a single continuous period during which a vehicle is
disconnected on GreenLight ELD while still moving on GoMotive. While the event
is ongoing it stays ``resolved = 0`` (an *active*/flagged vehicle); once the
vehicle stops being anomalous it is marked resolved. This keeps a single row
per ongoing event, which is what gives us alert de-duplication.

Backends (chosen at :func:`init_db` time, transparent to callers):
  * PostgreSQL via asyncpg when ``config.DATABASE_URL`` is set (e.g. Railway).
  * SQLite via aiosqlite otherwise (local dev fallback at ``config.DB_PATH``).

Timestamps are stored as ISO-8601 **text** (naive UTC) in both backends, so the
``AnomalyEvent`` dataclass and all the formatting/duration logic stay backend-
and timezone-agnostic. SQL is written once with asyncpg ``$1`` placeholders and
translated to ``?`` for SQLite (our queries never repeat or reorder a param).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from data import config

# --- schema (one per dialect; only the id/real types differ) -----------------

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS events (
    id                  BIGSERIAL PRIMARY KEY,
    unit_number         TEXT             NOT NULL,
    vin                 TEXT,
    driver              TEXT,
    eld_disconnect_time TEXT,
    first_detected      TEXT             NOT NULL,
    last_seen           TEXT             NOT NULL,
    last_speed          DOUBLE PRECISION,
    last_location       TEXT,
    resolved            INTEGER          NOT NULL DEFAULT 0,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
"""

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number         TEXT    NOT NULL,
    vin                 TEXT,
    driver              TEXT,
    eld_disconnect_time TEXT,
    first_detected      TEXT    NOT NULL,
    last_seen           TEXT    NOT NULL,
    last_speed          REAL,
    last_location       TEXT,
    resolved            INTEGER NOT NULL DEFAULT 0,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
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


# --- backend plumbing --------------------------------------------------------

_is_pg = False
_pool = None            # asyncpg pool (PostgreSQL)
_sqlite_path = None     # path string (SQLite)

_PLACEHOLDER = re.compile(r"\$\d+")


def _qmark(sql: str) -> str:
    """Translate asyncpg ``$1, $2, ...`` placeholders to SQLite ``?``.

    Safe here because every query lists its params once, in order."""
    return _PLACEHOLDER.sub("?", sql)


async def init_db() -> None:
    """Connect the chosen backend and ensure the schema exists. Idempotent: the
    asyncpg pool is created once; both schemas use IF NOT EXISTS."""
    global _is_pg, _pool, _sqlite_path

    if config.DATABASE_URL:
        import asyncpg

        _is_pg = True
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn=config.DATABASE_URL, min_size=1, max_size=5
            )
        async with _pool.acquire() as conn:
            await conn.execute(_SCHEMA_PG)
    else:
        import aiosqlite

        _is_pg = False
        _sqlite_path = config.DB_PATH
        async with aiosqlite.connect(_sqlite_path) as db:
            await db.executescript(_SCHEMA_SQLITE)
            await db.commit()


async def close() -> None:
    """Close the asyncpg pool (no-op for SQLite). Call on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _fetchrow(sql: str, *args) -> Optional[dict]:
    if _is_pg:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None
    import aiosqlite
    async with aiosqlite.connect(_sqlite_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_qmark(sql), args)
        row = await cur.fetchone()
    return dict(row) if row else None


async def _fetch(sql: str, *args) -> List[dict]:
    if _is_pg:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]
    import aiosqlite
    async with aiosqlite.connect(_sqlite_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_qmark(sql), args)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _execute(sql: str, *args) -> None:
    if _is_pg:
        async with _pool.acquire() as conn:
            await conn.execute(sql, *args)
        return
    import aiosqlite
    async with aiosqlite.connect(_sqlite_path) as db:
        await db.execute(_qmark(sql), args)
        await db.commit()


async def _insert_returning_id(sql: str, *args) -> int:
    if _is_pg:
        async with _pool.acquire() as conn:
            return await conn.fetchval(sql + " RETURNING id", *args)
    import aiosqlite
    async with aiosqlite.connect(_sqlite_path) as db:
        cur = await db.execute(_qmark(sql), args)
        await db.commit()
        return cur.lastrowid


def _row_to_event(row: dict) -> AnomalyEvent:
    return AnomalyEvent(**row)


# --- public API (same names/semantics as the old sync store) -----------------

async def get_active_event(unit_number: str) -> Optional[AnomalyEvent]:
    row = await _fetchrow(
        "SELECT * FROM events WHERE unit_number = $1 AND resolved = 0 "
        "ORDER BY id DESC LIMIT 1",
        unit_number,
    )
    return _row_to_event(row) if row else None


async def open_event(
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
    event_id = await _insert_returning_id(
        "INSERT INTO events (unit_number, vin, driver, eld_disconnect_time, "
        "first_detected, last_seen, last_speed, last_location) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        unit_number, vin, driver, eld_disconnect_time, now, now, speed, location,
    )
    row = await _fetchrow("SELECT * FROM events WHERE id = $1", event_id)
    return _row_to_event(row)


async def touch_event(
    event_id: int,
    *,
    speed: Optional[float],
    location: Optional[str],
) -> None:
    """Update an ongoing event with the latest reading (no new alert)."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    await _execute(
        "UPDATE events SET last_seen = $1, last_speed = $2, last_location = $3 "
        "WHERE id = $4",
        now, speed, location, event_id,
    )


async def resolve_event(event_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    await _execute(
        "UPDATE events SET resolved = 1, resolved_at = $1 WHERE id = $2",
        now, event_id,
    )


async def get_active_events() -> List[AnomalyEvent]:
    rows = await _fetch(
        "SELECT * FROM events WHERE resolved = 0 ORDER BY first_detected DESC"
    )
    return [_row_to_event(r) for r in rows]


async def active_unit_numbers() -> set:
    return {e.unit_number for e in await get_active_events()}


async def get_recent_events(limit: int = 20) -> List[AnomalyEvent]:
    rows = await _fetch(
        "SELECT * FROM events ORDER BY first_detected DESC LIMIT $1", limit
    )
    return [_row_to_event(r) for r in rows]


async def delete_events_by_driver(driver: str) -> int:
    """Delete events for a driver name; returns how many were removed.
    Used by scripts/mock_alert.py --cleanup to purge TEST DRIVER rows."""
    if _is_pg:
        async with _pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM events WHERE driver = $1", driver
            )
        return int(status.split()[-1])  # asyncpg returns e.g. "DELETE 3"
    import aiosqlite
    async with aiosqlite.connect(_sqlite_path) as db:
        cur = await db.execute("DELETE FROM events WHERE driver = ?", (driver,))
        await db.commit()
        return cur.rowcount
