"""Async store for ELD-disconnection anomaly events.

An "anomaly event" is a single continuous period during which a vehicle is
disconnected on Quantum ELD while still moving on GoMotive. While the event
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

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from data import config

logger = logging.getLogger(__name__)

# --- schema (one per dialect; only the id/real types differ) -----------------

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS companies (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT             NOT NULL UNIQUE,
    gomotive_token      TEXT,
    samsara_token       TEXT,
    quantum_token       TEXT,
    alert_chat_id       TEXT,
    active              INTEGER          NOT NULL DEFAULT 1,
    created_at          TEXT
);
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
    resolved_at         TEXT,
    motive_vehicle_id   TEXT,
    last_lat            DOUBLE PRECISION,
    last_lon            DOUBLE PRECISION,
    last_alert_at       TEXT,
    stop_notified       INTEGER          NOT NULL DEFAULT 0,
    reminders_muted     INTEGER          NOT NULL DEFAULT 0,
    company_id          INTEGER,
    stopped_at          TEXT,
    provider            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
"""

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    gomotive_token      TEXT,
    samsara_token       TEXT,
    quantum_token       TEXT,
    alert_chat_id       TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT
);
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
    resolved_at         TEXT,
    motive_vehicle_id   TEXT,
    last_lat            DOUBLE PRECISION,
    last_lon            DOUBLE PRECISION,
    last_alert_at       TEXT,
    stop_notified       INTEGER NOT NULL DEFAULT 0,
    reminders_muted     INTEGER NOT NULL DEFAULT 0,
    company_id          INTEGER,
    stopped_at          TEXT,
    provider            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
"""

# Created after migrations run, since on a legacy DB ``company_id`` only exists
# once the migration loop has added it.
_COMPANY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_events_company_active "
    "ON events (company_id, unit_number, resolved)"
)

# Enforce the one-chat-one-company invariant at the DB level (a chat maps to a
# single company; /bindhere also guards this in app code). Partial so multiple
# unbound companies (alert_chat_id IS NULL) are still allowed. Same syntax on
# Postgres and SQLite. Best-effort: if a legacy DB already holds a duplicate
# binding, creation is skipped with a warning rather than failing startup.
_COMPANY_CHAT_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_alert_chat "
    "ON companies (alert_chat_id) WHERE alert_chat_id IS NOT NULL"
)

# Columns added after the first release. CREATE TABLE above only covers fresh
# installs, so init_db also applies these to a pre-existing table, idempotently.
_MIGRATIONS = [
    ("motive_vehicle_id", "TEXT"),
    ("last_lat", "DOUBLE PRECISION"),
    ("last_lon", "DOUBLE PRECISION"),
    ("last_alert_at", "TEXT"),
    ("stop_notified", "INTEGER NOT NULL DEFAULT 0"),
    ("reminders_muted", "INTEGER NOT NULL DEFAULT 0"),
    ("company_id", "INTEGER"),
    ("stopped_at", "TEXT"),
    # Which movement API flagged the unit ("gomotive"/"samsara"); the tracker
    # re-queries motive_vehicle_id against that provider. NULL = gomotive
    # (every pre-Samsara event came from GoMotive).
    ("provider", "TEXT"),
]

# Columns added to a pre-existing `companies` table (same idempotent ADD COLUMN
# treatment as the events migrations above).
_COMPANY_MIGRATIONS = [
    # Second movement provider: companies whose trucks carry Samsara devices.
    ("samsara_token", "TEXT"),
]

# Migrations on a pre-existing `companies` table (CREATE TABLE above only covers
# fresh installs). The provider was renamed GreenLight -> Quantum ELD and the
# same carrier token works against Quantum, so the token column is renamed in
# place (value preserved). RENAME COLUMN needs SQLite >= 3.25.
_COMPANY_RENAMES = [
    ("greenlight_token", "quantum_token"),
]

# The per-company base-URL override was dropped: every company uses the single
# config.QUANTUM_BASE_URL. Drop the column under either name if a pre-existing DB
# still has it (values were NULL or the old GreenLight default — no real loss).
# DROP COLUMN needs SQLite >= 3.35; on Postgres it's DROP COLUMN IF EXISTS.
_COMPANY_DROPS = ["greenlight_base_url", "quantum_base_url"]


@dataclass
class Company:
    id: int
    name: str
    gomotive_token: Optional[str] = None
    samsara_token: Optional[str] = None
    quantum_token: Optional[str] = None
    alert_chat_id: Optional[str] = None
    active: int = 1
    created_at: Optional[str] = None


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
    motive_vehicle_id: Optional[str] = None
    last_lat: Optional[float] = None
    last_lon: Optional[float] = None
    last_alert_at: Optional[str] = None
    stop_notified: int = 0
    reminders_muted: int = 0
    company_id: Optional[int] = None
    # Set when a moving anomaly's unit stops: the span is paused (not resolved),
    # so a later reconnect still raises the all-clear and duration freezes here.
    stopped_at: Optional[str] = None
    # Movement API that flagged the unit ("gomotive"/"samsara"); the tracker
    # re-queries motive_vehicle_id against it. NULL = gomotive (legacy rows).
    provider: Optional[str] = None

    @property
    def disconnect_dt(self) -> Optional[datetime]:
        return _parse(self.eld_disconnect_time)

    def duration_seconds(self, now: Optional[datetime] = None) -> int:
        """How long this anomaly has run, measured from ``first_detected`` (when
        the unit was first seen moving-while-disconnected for this span) to now,
        or to resolution if resolved.

        An anomaly counts moving-disconnected time ONLY: the event is opened when
        the unit is seen moving and frozen when it stops, so its lifetime is the
        moving span. This is deliberately NOT measured from ``eld_disconnect_time``
        (the ELD's last report) — that can predate the movement, e.g. a unit that
        sat parked-and-disconnected for hours before it ever started rolling.

        The end is the stop time (if the unit has stopped — the span is over even
        while we keep the event open to watch for a reconnect), else the resolved
        time, else now."""
        start = _parse(self.first_detected) or self.disconnect_dt
        if start is None:
            return 0
        if self.stopped_at:
            end = _parse(self.stopped_at)
        elif self.resolved:
            end = _parse(self.resolved_at)
        else:
            end = None
        if end is None:
            end = now or datetime.utcnow()
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
            cols = {
                r["column_name"] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'events'"
                )
            }
            if "vehicle_id" in cols and "motive_vehicle_id" not in cols:
                await conn.execute(
                    "ALTER TABLE events RENAME COLUMN vehicle_id TO motive_vehicle_id"
                )
            for name, ddl in _MIGRATIONS:
                await conn.execute(
                    f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {name} {ddl}"
                )
            company_cols = {
                r["column_name"] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'companies'"
                )
            }
            for old, new in _COMPANY_RENAMES:
                if old in company_cols and new not in company_cols:
                    await conn.execute(
                        f"ALTER TABLE companies RENAME COLUMN {old} TO {new}"
                    )
            for col in _COMPANY_DROPS:
                await conn.execute(
                    f"ALTER TABLE companies DROP COLUMN IF EXISTS {col}"
                )
            for name, ddl in _COMPANY_MIGRATIONS:
                await conn.execute(
                    f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS {name} {ddl}"
                )
            await conn.execute(_COMPANY_INDEX)
            try:
                await conn.execute(_COMPANY_CHAT_UNIQUE_INDEX)
            except Exception:
                logger.warning(
                    "Could not create unique index on companies.alert_chat_id "
                    "(a duplicate binding may already exist); skipping.",
                    exc_info=True,
                )
    else:
        import aiosqlite

        _is_pg = False
        _sqlite_path = config.DB_PATH
        async with aiosqlite.connect(_sqlite_path) as db:
            await db.executescript(_SCHEMA_SQLITE)
            cur = await db.execute("PRAGMA table_info(events)")
            existing = {row[1] for row in await cur.fetchall()}
            if "vehicle_id" in existing and "motive_vehicle_id" not in existing:
                await db.execute(
                    "ALTER TABLE events RENAME COLUMN vehicle_id TO motive_vehicle_id"
                )
                existing.add("motive_vehicle_id")
            for name, ddl in _MIGRATIONS:
                if name not in existing:
                    await db.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
            cur = await db.execute("PRAGMA table_info(companies)")
            company_cols = {row[1] for row in await cur.fetchall()}
            for old, new in _COMPANY_RENAMES:
                if old in company_cols and new not in company_cols:
                    await db.execute(
                        f"ALTER TABLE companies RENAME COLUMN {old} TO {new}"
                    )
                    company_cols.discard(old)
                    company_cols.add(new)
            for col in _COMPANY_DROPS:
                if col in company_cols:
                    await db.execute(f"ALTER TABLE companies DROP COLUMN {col}")
                    company_cols.discard(col)
            for name, ddl in _COMPANY_MIGRATIONS:
                if name not in company_cols:
                    await db.execute(f"ALTER TABLE companies ADD COLUMN {name} {ddl}")
            await db.execute(_COMPANY_INDEX)
            try:
                await db.execute(_COMPANY_CHAT_UNIQUE_INDEX)
            except Exception:
                logger.warning(
                    "Could not create unique index on companies.alert_chat_id "
                    "(a duplicate binding may already exist); skipping.",
                    exc_info=True,
                )
            await db.commit()

    await _seed_default_company()


async def _seed_default_company() -> None:
    """One-time adoption of the pre-multi-company production setup. Fires only when
    the companies table is empty, so the live company is adopted exactly once and
    never re-seeded; backend-agnostic (uses the generic helpers).

    A "default" company is created and every company-less event is stamped with its
    id when EITHER the legacy token env vars are configured OR a legacy DB already
    holds events. The second case matters: if an operator upgrades after clearing
    the now-"seed-only" env vars, we must still adopt the existing rows — otherwise
    every read query (all scoped by company_id) would render that history invisible
    and unrecoverable. Such a company is created token-less and simply isn't polled
    until its tokens are set via /addcompany (or the env vars are restored)."""
    existing = await _fetchrow("SELECT id FROM companies LIMIT 1")
    if existing is not None:
        return

    has_legacy_tokens = bool(
        (config.GOMOTIVE_TOKEN or config.SAMSARA_TOKEN) and config.QUANTUM_TOKEN
    )
    orphan = await _fetchrow("SELECT id FROM events WHERE company_id IS NULL LIMIT 1")
    has_orphan_events = orphan is not None
    if not has_legacy_tokens and not has_orphan_events:
        return  # fresh install, nothing to adopt

    company = await add_company(
        name="default",
        gomotive_token=config.GOMOTIVE_TOKEN or None,
        quantum_token=config.QUANTUM_TOKEN or None,
        samsara_token=config.SAMSARA_TOKEN or None,
    )
    if config.ALERT_CHAT_ID:
        await bind_company_chat(company.id, config.ALERT_CHAT_ID)
    await _execute(
        "UPDATE events SET company_id = $1 WHERE company_id IS NULL", company.id
    )
    if not has_legacy_tokens:
        logger.warning(
            "Adopted pre-existing event history into a token-less 'default' "
            "company (legacy GOMOTIVE_TOKEN/QUANTUM_TOKEN are not set). Its "
            "history is preserved but it won't be polled until you set its tokens "
            "via /addcompany or restore the seed env vars."
        )


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


def _row_to_company(row: dict) -> Company:
    return Company(**row)


# --- companies ---------------------------------------------------------------

async def add_company(
    *,
    name: str,
    gomotive_token: Optional[str],
    quantum_token: Optional[str],
    samsara_token: Optional[str] = None,
) -> Company:
    """Create a company and return it. ``alert_chat_id`` is left NULL — bind it to
    a Telegram group with :func:`bind_company_chat` before it will be polled."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    company_id = await _insert_returning_id(
        "INSERT INTO companies (name, gomotive_token, samsara_token, "
        "quantum_token, created_at) VALUES ($1, $2, $3, $4, $5)",
        name, gomotive_token, samsara_token, quantum_token, now,
    )
    row = await _fetchrow("SELECT * FROM companies WHERE id = $1", company_id)
    return _row_to_company(row)


async def bind_company_chat(company_id: int, chat_id) -> None:
    """Bind a company to the Telegram chat that receives its alerts. Stored as
    text so it compares cleanly against ``str(message.chat.id)``. Pass
    ``chat_id=None`` to clear the binding (unbind)."""
    value = None if chat_id is None else str(chat_id)
    await _execute(
        "UPDATE companies SET alert_chat_id = $1 WHERE id = $2",
        value, company_id,
    )


async def set_company_active(company_id: int, value: int) -> None:
    await _execute(
        "UPDATE companies SET active = $1 WHERE id = $2", value, company_id
    )


async def get_company(company_id: int) -> Optional[Company]:
    row = await _fetchrow("SELECT * FROM companies WHERE id = $1", company_id)
    return _row_to_company(row) if row else None


async def get_company_by_name(name: str) -> Optional[Company]:
    row = await _fetchrow("SELECT * FROM companies WHERE name = $1", name)
    return _row_to_company(row) if row else None


async def get_company_by_chat(chat_id) -> Optional[Company]:
    row = await _fetchrow(
        "SELECT * FROM companies WHERE alert_chat_id = $1", str(chat_id)
    )
    return _row_to_company(row) if row else None


async def list_companies(active_only: bool = False) -> List[Company]:
    sql = "SELECT * FROM companies"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    rows = await _fetch(sql)
    return [_row_to_company(r) for r in rows]


async def active_companies() -> List[Company]:
    """Companies ready to poll: active and bound to an alert chat."""
    rows = await _fetch(
        "SELECT * FROM companies WHERE active = 1 AND alert_chat_id IS NOT NULL "
        "ORDER BY id"
    )
    return [_row_to_company(r) for r in rows]


# --- public API (same names/semantics as the old sync store) -----------------

async def get_active_event(
    company_id: int, unit_number: str
) -> Optional[AnomalyEvent]:
    row = await _fetchrow(
        "SELECT * FROM events WHERE company_id = $1 AND unit_number = $2 "
        "AND resolved = 0 ORDER BY id DESC LIMIT 1",
        company_id, unit_number,
    )
    return _row_to_event(row) if row else None


async def open_event(
    *,
    company_id: int,
    unit_number: str,
    vin: Optional[str],
    driver: Optional[str],
    eld_disconnect_time: Optional[str],
    speed: Optional[float],
    location: Optional[str],
    motive_vehicle_id: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    provider: Optional[str] = None,
) -> AnomalyEvent:
    """Create a new active anomaly event and return it (caller then alerts).
    ``last_alert_at`` is seeded with ``now`` since the caller sends the initial
    alert immediately; the 30-min reminder clock counts from there."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    event_id = await _insert_returning_id(
        "INSERT INTO events (company_id, unit_number, vin, driver, "
        "eld_disconnect_time, first_detected, last_seen, last_speed, last_location, "
        "motive_vehicle_id, last_lat, last_lon, last_alert_at, provider) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
        company_id, unit_number, vin, driver, eld_disconnect_time, now, now, speed,
        location, motive_vehicle_id, lat, lon, now, provider,
    )
    row = await _fetchrow("SELECT * FROM events WHERE id = $1", event_id)
    return _row_to_event(row)


async def touch_event(
    event_id: int,
    *,
    speed: Optional[float],
    location: Optional[str],
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> None:
    """Update an ongoing event with the latest reading (no new alert)."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    await _execute(
        "UPDATE events SET last_seen = $1, last_speed = $2, last_location = $3, "
        "last_lat = $4, last_lon = $5 WHERE id = $6",
        now, speed, location, lat, lon, event_id,
    )


async def mark_alerted(event_id: int) -> None:
    """Record that a group notification (initial or reminder) was just sent."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    await _execute("UPDATE events SET last_alert_at = $1 WHERE id = $2", now, event_id)


async def mark_stopped(event_id: int) -> None:
    """Pause a moving anomaly because its unit stopped. The event stays active
    (unresolved) so a later reconnect still raises the all-clear; ``stopped_at``
    freezes the anomaly's duration at the stop and silences reminders."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    await _execute(
        "UPDATE events SET stopped_at = $1 WHERE id = $2", now, event_id
    )


async def mute_reminders(event_id: int) -> None:
    await _execute(
        "UPDATE events SET reminders_muted = 1 WHERE id = $1", event_id
    )


async def resolve_event(event_id: int, at: Optional[str] = None) -> None:
    """Close an event. ``at`` overrides the resolution timestamp (e.g. close a
    paused span at its stop time rather than now); defaults to now."""
    resolved_at = at or datetime.utcnow().isoformat(timespec="seconds")
    await _execute(
        "UPDATE events SET resolved = 1, resolved_at = $1 WHERE id = $2",
        resolved_at, event_id,
    )


async def get_active_events(company_id: int) -> List[AnomalyEvent]:
    rows = await _fetch(
        "SELECT * FROM events WHERE company_id = $1 AND resolved = 0 "
        "ORDER BY first_detected DESC",
        company_id,
    )
    return [_row_to_event(r) for r in rows]


async def active_unit_numbers(company_id: int) -> set:
    return {e.unit_number for e in await get_active_events(company_id)}


async def get_recent_events(company_id: int, limit: int = 20) -> List[AnomalyEvent]:
    rows = await _fetch(
        "SELECT * FROM events WHERE company_id = $1 "
        "ORDER BY first_detected DESC LIMIT $2",
        company_id, limit,
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
