"""Async store for ELD-disconnection anomaly events.

An "anomaly event" is a single continuous period during which a vehicle is
disconnected on its EVO ELD while still moving on its movement
provider (GoMotive/Samsara). While the event
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
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from data import config
from .units import unit_key

logger = logging.getLogger(__name__)

# --- schema (one per dialect; only the id/real types differ) -----------------

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS companies (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT             NOT NULL UNIQUE,
    gomotive_token      TEXT,
    samsara_token       TEXT,
    evo_api_key         TEXT,
    evo_provider_token  TEXT,
    evo_usdot           TEXT,
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
    provider            TEXT,
    eld_provider        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
CREATE TABLE IF NOT EXISTS unit_ignores (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          INTEGER          NOT NULL,
    unit_key            TEXT             NOT NULL,
    label               TEXT,
    note                TEXT,
    until               TEXT,
    created_at          TEXT             NOT NULL,
    created_by          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_ignores ON unit_ignores (company_id, unit_key);
"""

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    gomotive_token      TEXT,
    samsara_token       TEXT,
    evo_api_key         TEXT,
    evo_provider_token  TEXT,
    evo_usdot           TEXT,
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
    provider            TEXT,
    eld_provider        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_active ON events (unit_number, resolved);
CREATE TABLE IF NOT EXISTS unit_ignores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL,
    unit_key            TEXT    NOT NULL,
    label               TEXT,
    note                TEXT,
    until               TEXT,
    created_at          TEXT    NOT NULL,
    created_by          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_ignores ON unit_ignores (company_id, unit_key);
"""

# Created after migrations run, since on a legacy DB ``company_id`` only exists
# once the migration loop has added it.
_COMPANY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_events_company_active "
    "ON events (company_id, unit_number, resolved)"
)

# A chat may host several companies (e.g. two carriers sharing one dispatch
# group), so the old one-chat-one-company unique index is gone; this drops it
# from legacy DBs. Same syntax on Postgres and SQLite.
_COMPANY_CHAT_UNIQUE_INDEX_DROP = "DROP INDEX IF EXISTS uq_companies_alert_chat"

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
    # Which ELD system's stale record flagged the unit ("evo"; the column
    # predates this branch being EVO-only, so NULL rows are EVO too).
    ("eld_provider", "TEXT"),
]

# Columns added to a pre-existing `companies` table (same idempotent ADD COLUMN
# treatment as the events migrations above).
_COMPANY_MIGRATIONS = [
    # Second movement provider: companies whose trucks carry Samsara devices.
    ("samsara_token", "TEXT"),
    # Second ELD-side provider: EVO ELD (all three must be set to be used —
    # see Company.evo_configured).
    ("evo_api_key", "TEXT"),
    ("evo_provider_token", "TEXT"),
    ("evo_usdot", "TEXT"),
]

# Column renames on a pre-existing `companies` table (CREATE TABLE above only
# covers fresh installs). Nothing to rename on this branch: the one entry
# that used to live here (greenlight_token -> quantum_token) became moot when
# the Quantum provider was removed, so both names are simply dropped below.
_COMPANY_RENAMES: list = []

# Columns of providers this branch no longer has. They must be DROPPED, not
# just ignored: every read does SELECT * and feeds the row to Company(**row),
# so a leftover column raises TypeError on each poll cycle. The ELD-side ones
# (greenlight = the old name for quantum) went with the Quantum and Vitality
# providers; the base-URL overrides were replaced by a single config value.
# DROP COLUMN needs SQLite >= 3.35; on Postgres it's DROP COLUMN IF EXISTS.
_COMPANY_DROPS = ["greenlight_base_url", "quantum_base_url",
                  "vitality_company_key", "greenlight_token",
                  "quantum_token"]


@dataclass
class Company:
    id: int
    name: str
    gomotive_token: Optional[str] = None
    samsara_token: Optional[str] = None
    evo_api_key: Optional[str] = None
    evo_provider_token: Optional[str] = None
    evo_usdot: Optional[str] = None
    alert_chat_id: Optional[str] = None
    active: int = 1
    created_at: Optional[str] = None

    @property
    def evo_configured(self) -> bool:
        """EVO needs all three credentials (api key + provider token + USDOT);
        a partial set is treated as not configured."""
        return bool(self.evo_api_key and self.evo_provider_token and self.evo_usdot)

    @property
    def has_eld(self) -> bool:
        """At least one ELD-side system is usable for this company."""
        return bool(self.evo_configured)


@dataclass
class UnitIgnore:
    """One unit an admin told the bot to stop flagging (see the ignored-units
    section below for why that is ever the right answer)."""
    id: int
    company_id: int
    unit_key: str
    label: Optional[str]
    note: Optional[str]
    until: Optional[str]
    created_at: str
    created_by: Optional[str]

    @property
    def expires_in_seconds(self) -> Optional[int]:
        """Seconds left before the entry lapses; None when it never expires."""
        until = _parse(self.until)
        if until is None:
            return None
        return max(0, int((until - datetime.utcnow()).total_seconds()))


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
    # ELD system whose stale record flagged the unit ("evo"); NULL on rows
    # written before the column existed.
    eld_provider: Optional[str] = None

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
            await conn.execute(_COMPANY_CHAT_UNIQUE_INDEX_DROP)
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
            await db.execute(_COMPANY_CHAT_UNIQUE_INDEX_DROP)
            await db.commit()

    await _seed_default_company()


async def _seed_default_company() -> None:
    """One-time adoption of the pre-multi-company production setup. Fires only when
    the companies table is empty, so the live company is adopted exactly once and
    never re-seeded; backend-agnostic (uses the generic helpers).

    A "default" company is created and every company-less event is stamped with
    its id when a legacy DB holds such events: all reads are scoped by
    company_id, so leaving them unstamped would render that history invisible
    and unrecoverable. The ELD credentials cannot be seeded from the
    environment (EVO's are per-company only), so the company is created without
    them and simply isn't polled until they are set with /seteld."""
    existing = await _fetchrow("SELECT id FROM companies LIMIT 1")
    if existing is not None:
        return

    orphan = await _fetchrow("SELECT id FROM events WHERE company_id IS NULL LIMIT 1")
    if orphan is None:
        return  # fresh install, nothing to adopt

    company = await add_company(
        name="default",
        gomotive_token=config.GOMOTIVE_TOKEN or None,
        samsara_token=config.SAMSARA_TOKEN or None,
    )
    if config.ALERT_CHAT_ID:
        await bind_company_chat(company.id, config.ALERT_CHAT_ID)
    await _execute(
        "UPDATE events SET company_id = $1 WHERE company_id IS NULL", company.id
    )
    logger.warning(
        "Adopted pre-existing event history into a 'default' company with no "
        "ELD credentials. Its history is preserved but it won't be polled "
        "until you set them with /seteld <company> evo."
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
    samsara_token: Optional[str] = None,
    evo_api_key: Optional[str] = None,
    evo_provider_token: Optional[str] = None,
    evo_usdot: Optional[str] = None,
) -> Company:
    """Create a company and return it. ``alert_chat_id`` is left NULL — bind it to
    a Telegram group with :func:`bind_company_chat` before it will be polled."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    company_id = await _insert_returning_id(
        "INSERT INTO companies (name, gomotive_token, samsara_token, "
        "evo_api_key, evo_provider_token, evo_usdot, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        name, gomotive_token, samsara_token,
        evo_api_key, evo_provider_token, evo_usdot, now,
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


# Per-company ELD credentials editable after creation (/seteld). Maps the
# /seteld system name to the column(s) it writes; a whitelist, so the column
# names are safe to interpolate.
ELD_CREDENTIAL_COLUMNS = {
    "evo": ("evo_api_key", "evo_provider_token", "evo_usdot"),
}


async def set_eld_credentials(
    company_id: int, system: str, values: Sequence[Optional[str]]
) -> None:
    """Set (or clear, with all-None ``values``) one ELD system's credentials.

    Written in a single UPDATE: EVO needs three values and a partial set counts
    as not configured (see Company.evo_configured), so writing them one at a
    time could leave the company with no usable ELD system between calls."""
    columns = ELD_CREDENTIAL_COLUMNS[system]
    if len(values) != len(columns):
        raise ValueError(f"{system} takes {len(columns)} value(s), got {len(values)}")
    assignments = ", ".join(
        f"{column} = ${i}" for i, column in enumerate(columns, start=1)
    )
    await _execute(
        f"UPDATE companies SET {assignments} WHERE id = ${len(columns) + 1}",
        *values, company_id,
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


async def get_companies_by_chat(chat_id) -> List[Company]:
    """All companies bound to this chat — a chat may host more than one."""
    rows = await _fetch(
        "SELECT * FROM companies WHERE alert_chat_id = $1 ORDER BY id",
        str(chat_id),
    )
    return [_row_to_company(r) for r in rows]


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


# --- ignored units -----------------------------------------------------------

# Units an admin has told the bot to stop flagging. The real case this exists
# for: a carrier pulls the Motive device out of a parked truck and runs it in a
# different one. The movement provider then keeps reporting the OLD unit number
# as moving while that truck's own ELD is legitimately offline — a permanent
# false anomaly that no ELD-side credential can fix and no VIN check can catch
# (both sides still carry the parked truck's VIN). Muting the unit is the only
# honest answer until the carrier reassigns the device, so entries are normally
# time-limited: a forgotten mute is an unmonitored truck.


def ignore_key(unit_number: str) -> str:
    """Canonical form used to match an ignore entry against a live unit number.

    Uses the same normalization as the ELD lookups (:func:`unit_key`, which
    strips owner tags and driver names), so an admin can copy the number
    straight out of an alert: "unit 796801  MAXIMO PAEZ" and "796801" both land
    on the key "796801"."""
    return unit_key(str(unit_number)).strip().upper()


async def _purge_expired_ignores() -> None:
    """Drop lapsed entries so an ignore can never outlive its window. Logged,
    because the unit is monitored again from that moment on. Timestamps are
    ISO-8601 text in a fixed format, so a string compare is a time compare."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    rows = await _fetch(
        "SELECT * FROM unit_ignores WHERE until IS NOT NULL AND until <= $1", now
    )
    if not rows:
        return
    await _execute(
        "DELETE FROM unit_ignores WHERE until IS NOT NULL AND until <= $1", now
    )
    for row in rows:
        logger.info(
            "Ignore window expired for company %s unit %s — flagging it again.",
            row["company_id"], row["label"] or row["unit_key"],
        )


async def add_unit_ignore(
    company_id: int,
    unit_number: str,
    *,
    days: Optional[int] = None,
    note: Optional[str] = None,
    created_by: Optional[str] = None,
) -> UnitIgnore:
    """Ignore a unit for this company, replacing any existing entry for it.

    ``days=None`` never expires. ``label`` keeps the number as the admin typed
    it, so the list reads like the alert it came from."""
    now = datetime.utcnow()
    until = (
        (now + timedelta(days=days)).isoformat(timespec="seconds")
        if days else None
    )
    key = ignore_key(unit_number)
    await _execute(
        "DELETE FROM unit_ignores WHERE company_id = $1 AND unit_key = $2",
        company_id, key,
    )
    await _execute(
        "INSERT INTO unit_ignores (company_id, unit_key, label, note, until, "
        "created_at, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        company_id, key, unit_number.strip(), note, until,
        now.isoformat(timespec="seconds"), created_by,
    )
    row = await _fetchrow(
        "SELECT * FROM unit_ignores WHERE company_id = $1 AND unit_key = $2",
        company_id, key,
    )
    return UnitIgnore(**row)


async def remove_unit_ignore(
    company_id: int, unit_number: str
) -> Optional[UnitIgnore]:
    """Stop ignoring a unit. Returns the entry that was removed, or None when
    the unit wasn't on the list."""
    key = ignore_key(unit_number)
    row = await _fetchrow(
        "SELECT * FROM unit_ignores WHERE company_id = $1 AND unit_key = $2",
        company_id, key,
    )
    if row is None:
        return None
    await _execute(
        "DELETE FROM unit_ignores WHERE company_id = $1 AND unit_key = $2",
        company_id, key,
    )
    return UnitIgnore(**row)


async def list_unit_ignores(company_id: Optional[int] = None) -> List[UnitIgnore]:
    """Ignore entries in force (all companies when ``company_id`` is None),
    newest first. Lapsed ones are purged first, so the list is never a lie."""
    await _purge_expired_ignores()
    if company_id is None:
        rows = await _fetch(
            "SELECT * FROM unit_ignores ORDER BY company_id, created_at DESC"
        )
    else:
        rows = await _fetch(
            "SELECT * FROM unit_ignores WHERE company_id = $1 "
            "ORDER BY created_at DESC",
            company_id,
        )
    return [UnitIgnore(**r) for r in rows]


async def ignored_unit_keys(company_id: int) -> set:
    """Normalized keys of this company's currently-ignored units, for the poller
    and tracker to skip. Lapsed entries are purged first."""
    await _purge_expired_ignores()
    rows = await _fetch(
        "SELECT unit_key FROM unit_ignores WHERE company_id = $1", company_id
    )
    return {r["unit_key"] for r in rows}


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
    eld_provider: Optional[str] = None,
) -> AnomalyEvent:
    """Create a new active anomaly event and return it (caller then alerts).
    ``last_alert_at`` is seeded with ``now`` since the caller sends the initial
    alert immediately; the 30-min reminder clock counts from there."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    event_id = await _insert_returning_id(
        "INSERT INTO events (company_id, unit_number, vin, driver, "
        "eld_disconnect_time, first_detected, last_seen, last_speed, last_location, "
        "motive_vehicle_id, last_lat, last_lon, last_alert_at, provider, "
        "eld_provider) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
        company_id, unit_number, vin, driver, eld_disconnect_time, now, now, speed,
        location, motive_vehicle_id, lat, lon, now, provider, eld_provider,
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
