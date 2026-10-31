"""Vitality / Zero facts for the internal API, without extra load on DriveHOS.

Where each fact comes from:
  * the fleet (position, speed, status, last report, the driver_id on each
    truck): the poller's latest ``/v2/latest-vehicle-status`` answer
    (vitalityeld.latest_raw) when it is no older than the caller's
    ``max_age``; otherwise ONE on-demand fetch through vitalityeld.fetch_units
    (the same code the poller runs: Vitality Plus fallback, deactivated-truck
    list), shared by every request that arrives while it runs. That fetch
    becomes the new latest data. A failed fetch is remembered for a while
    (FETCH_ERROR_TTL / UNAVAILABLE_TTL) so a burst of requests cannot hammer a
    dead feed. ``max_age`` is clamped to at least MIN_MAX_AGE.
  * driver names and phones: the ``/v2/drivers`` roster, shared with the
    poller's hourly name cache (vitalityeld.remember_roster). A driver missing
    from it is fetched alone (``/v2/driver/{id}``) at most once an hour.
  * duty status: ``/v2/latest-driver-status?driver_id=``, cached DUTY_TTL per
    driver. The poller never asks for it, so this is the only new kind of
    request; it is made only for the trucks a caller asks about.

Every answer carries its age so the caller can judge freshness.
"""

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

from data import config
from utils.eld import store, vitalityeld

from . import unit_match

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE = 120     # s: the fleet data a request accepts by default
MIN_MAX_AGE = 30          # s: no caller makes us refetch more often than this
MAX_MAX_AGE = 3600
FETCH_ERROR_TTL = 45.0    # s: a failed fleet fetch is not retried sooner
UNAVAILABLE_TTL = 300.0   # s: key refused / empty fleet: look again in 5 min
DUTY_TTL = 45.0
DUTY_ERROR_TTL = 15.0
ROSTER_TTL = 3600.0
ROSTER_RETRY = 300.0      # s: a failed roster fetch is retried after this
COMPANY_TTL = 60.0        # s: the companies table is re-read this often
MAX_DUTY_LOOKUPS = 3      # duty statuses fetched per unit request at most

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)
_DRIVER_PAGE_SIZE = 1000
_MAX_PAGES = 20
_PHONE_FIELDS = ("phone", "phone_number", "mobile", "mobile_phone", "cell_phone")
_HEADING_FIELDS = ("heading", "bearing", "course", "direction")
_DRIVER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# latest-driver-status current_status codes.
_ON_DUTY = {"DS_D", "DS_ON", "DR_IND_YM"}
_OFF_DUTY = {"DS_OFF", "DS_SB"}
_PERSONAL_CONVEYANCE = "DR_IND_PC"
_YARD_MOVE = "DR_IND_YM"


class ApiError(Exception):
    """An answer other than 200, as {"error": code, "message": message}."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def driver_id_ok(value: str) -> bool:
    return bool(_DRIVER_ID.match(value or ""))


# ── time / JSON helpers ──────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(dt: Optional[datetime], now: datetime) -> Optional[int]:
    return None if dt is None else max(0, int((now - dt).total_seconds()))


def _parse_time(value) -> Optional[datetime]:
    """"2026-09-17T15:03:40Z" -> tz-aware UTC (a naive value is read as UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean(value) -> str:
    return " ".join(str(value or "").split())


# ── companies ────────────────────────────────────────────────────────────────

_companies_cache: Tuple[float, List[store.Company]] = (0.0, [])


def _norm_name(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").casefold())


def aliases() -> Dict[str, str]:
    """INTERNAL_API_COMPANY_ALIASES as {normalized alias: target}."""
    out = {}
    for part in re.split(r"[,;]", config.INTERNAL_API_COMPANY_ALIASES or ""):
        alias, sep, target = part.partition("=")
        if sep and _norm_name(alias) and target.strip():
            out[_norm_name(alias)] = target.strip()
    return out


async def companies() -> List[store.Company]:
    global _companies_cache
    if time.monotonic() - _companies_cache[0] < COMPANY_TTL and _companies_cache[1]:
        return _companies_cache[1]
    rows = await store.list_companies()
    _companies_cache = (time.monotonic(), rows)
    return rows


def _by_target(rows: List[store.Company], target: str) -> List[store.Company]:
    if target.isdigit():
        return [c for c in rows if c.id == int(target)]
    exact = [c for c in rows if c.name.casefold() == target.casefold()]
    return exact or [c for c in rows if _norm_name(c.name) == _norm_name(target)]


async def resolve_company(ref: str) -> store.Company:
    """The company a caller means by `ref`: an INTERNAL_API_COMPANY_ALIASES
    alias, else a numeric company id, else the company whose name equals it
    ignoring case and punctuation. 404 when none, 409 when several."""
    rows = await companies()
    target = aliases().get(_norm_name(ref))
    if target is not None:
        found = _by_target(rows, target)
        how = "alias"
    elif ref.strip().isdigit():
        found = [c for c in rows if c.id == int(ref.strip())]
        how = "id"
    else:
        found = [c for c in rows if _norm_name(c.name) == _norm_name(ref)]
        how = "name"
    if not found:
        raise ApiError(404, "unknown_company", f"no company matches {ref!r} (by {how})")
    if len(found) > 1:
        raise ApiError(409, "company_ambiguous",
                       f"{len(found)} companies match {ref!r}; set an alias",
                       company_ids=[c.id for c in found])
    return found[0]


def company_json(company: store.Company) -> dict:
    return {"id": company.id, "name": company.name, "active": bool(company.active)}


# ── which DriveHOS feed ──────────────────────────────────────────────────────

@dataclass
class Feed:
    company: store.Company
    brand: str          # "Vitality" / "Zero"
    company_key: str
    provider_key: str   # the configured one; Plus is tried by vitalityeld
    base_url: str

    @property
    def key(self) -> Tuple[str, str]:
        return (self.base_url.rstrip("/"), self.company_key)

    def provider_name(self, used_key: Optional[str] = None) -> str:
        if self.brand == "Zero":
            return "zero"
        used = used_key or vitalityeld._provider_used.get(self.company_key)
        plus = config.VITALITY_PLUS_PROVIDER_KEY
        return "vitality_plus" if plus and used == plus else "vitality"

    def provider_keys(self) -> List[str]:
        """Provider keys to try for single requests: the one that last produced
        this carrier's fleet first, then (Vitality only) the other one."""
        keys = [self.provider_key]
        plus = config.VITALITY_PLUS_PROVIDER_KEY
        if self.brand == "Vitality" and plus and plus != self.provider_key:
            keys.append(plus)
        keys = [k for k in dict.fromkeys(keys) if k]
        entry = vitalityeld.latest_raw(self.company_key, self.base_url)
        known = (entry.provider_key if entry and entry.rows else None) \
            or vitalityeld._provider_used.get(self.company_key)
        if known in keys:
            keys.remove(known)
            keys.insert(0, known)
        return keys


def select_feed(company: store.Company, provider: str) -> Feed:
    """provider: "" / "auto" (Vitality when the company has a key, else Zero),
    "vitality" or "zero"."""
    provider = (provider or "auto").strip().lower()
    if provider not in ("auto", "vitality", "zero"):
        raise ApiError(400, "bad_request", "provider must be auto, vitality or zero")
    if provider == "auto":
        provider = ("vitality" if company.vitality_company_key
                    else "zero" if company.zero_company_key else "vitality")
    if provider == "vitality":
        ck, pk, base, brand = (company.vitality_company_key, config.VITALITY_PROVIDER_KEY,
                               config.VITALITY_BASE_URL, "Vitality")
    else:
        ck, pk, base, brand = (company.zero_company_key, config.ZERO_PROVIDER_KEY,
                               config.ZERO_BASE_URL, "Zero")
    if not ck:
        raise ApiError(404, "not_configured",
                       f"company {company.name!r} has no {brand} company key")
    if not pk:
        raise ApiError(503, "provider_not_configured",
                       f"this bot has no {brand} provider key")
    return Feed(company, brand, ck, pk, base)


# ── HTTP for the requests the poller never makes ─────────────────────────────

_session: Optional[aiohttp.ClientSession] = None


def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT)
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


class _Refused(Exception):
    """401/403 from DriveHOS for this provider key."""


async def _get_once(feed: Feed, provider_key: str, path: str, params: dict) -> dict:
    url = feed.base_url.rstrip("/") + path
    headers = {"accept": "application/json", "X-API-Provider-Key": provider_key,
               "X-API-Company-Key": feed.company_key}
    async with _http().get(url, headers=headers, params=params) as resp:
        if resp.status in (401, 403):
            raise _Refused(f"{feed.brand} {path} HTTP {resp.status}")
        if resp.status != 200:
            raise RuntimeError(f"{feed.brand} {path} HTTP {resp.status}")
        data = await resp.json(content_type=None)
    if not isinstance(data, dict):
        raise RuntimeError(f"{feed.brand} {path} unexpected body")
    if data.get("status_code") in (401, 403):
        raise _Refused(f"{feed.brand} {path} status_code {data.get('status_code')}")
    if data.get("description") != "success":
        raise RuntimeError(f"{feed.brand} {path} error: {str(data.get('description'))[:120]}")
    return data


async def _get(feed: Feed, path: str, params: Optional[dict] = None) -> dict:
    """One GET, trying the next provider key when one is refused."""
    last: Optional[Exception] = None
    for key in feed.provider_keys():
        try:
            return await _get_once(feed, key, path, params or {})
        except _Refused as e:
            last = e
    raise last or RuntimeError(f"{feed.brand} {path}: no provider key")


async def _get_pages(feed: Feed, path: str, page_size: int) -> List[dict]:
    items: List[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        env = await _get(feed, path, {"limit": page_size, "page": page})
        data = env.get("data")
        if isinstance(data, dict):
            data = [data]
        items.extend(d for d in (data or []) if isinstance(d, dict))
        try:
            total = int(env.get("total_pages") or 1)
        except (TypeError, ValueError):
            total = 1
        if not data or page >= total:
            break
        page += 1
    return items


# ── the fleet ────────────────────────────────────────────────────────────────

# feed.key -> (monotonic expiry, error code, message)
_fetch_errors: Dict[Tuple[str, str], Tuple[float, str, str]] = {}
_inflight: Dict[Tuple[str, str], asyncio.Task] = {}


def clamp_max_age(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_MAX_AGE
    return max(MIN_MAX_AGE, min(MAX_MAX_AGE, int(value)))


async def _fetch_fleet(feed: Feed) -> None:
    started = time.monotonic()
    try:
        await vitalityeld.fetch_units(feed.company_key, feed.provider_key, feed.base_url,
                                      brand=feed.brand)
    except vitalityeld.DriveHOSAuthError as e:
        _fetch_errors[feed.key] = (time.monotonic() + UNAVAILABLE_TTL, "upstream_auth_rejected",
                                   f"{feed.brand} refused the carrier key (HTTP {e.status})")
        logger.warning("Internal API: %s fleet fetch for %s refused (HTTP %s)",
                       feed.brand, feed.company.name, e.status)
        raise
    except Exception as e:
        _fetch_errors[feed.key] = (time.monotonic() + FETCH_ERROR_TTL, "upstream_unavailable",
                                   f"{feed.brand} fleet fetch failed ({type(e).__name__})")
        logger.warning("Internal API: %s fleet fetch for %s failed: %s",
                       feed.brand, feed.company.name, e)
        raise
    entry = vitalityeld.latest_raw(feed.company_key, feed.base_url)
    if entry is not None and entry.fetched_mono >= started:
        entry.source = "on_demand"
    if entry is None or not entry.rows:
        _fetch_errors[feed.key] = (time.monotonic() + UNAVAILABLE_TTL, "upstream_empty",
                                   f"{feed.brand} returned no vehicles for this carrier")
    else:
        _fetch_errors.pop(feed.key, None)


def _forget_task(key, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        _inflight.pop(key, None)
    if not task.cancelled():
        task.exception()  # retrieved: nobody may be awaiting it any more


async def fleet(feed: Feed, max_age: int) -> vitalityeld.RawFleet:
    """The carrier's latest fleet no older than `max_age` seconds (see the
    module docstring). Raises ApiError 503 when DriveHOS cannot give one."""
    def usable(entry) -> bool:
        return (entry is not None
                and time.monotonic() - entry.fetched_mono <= max_age)

    entry = vitalityeld.latest_raw(feed.company_key, feed.base_url)
    if not usable(entry):
        err = _fetch_errors.get(feed.key)
        if err and time.monotonic() < err[0]:
            raise ApiError(503, err[1], err[2], retry_after_seconds=int(err[0] - time.monotonic()) + 1)
        task = _inflight.get(feed.key)
        if task is None:
            task = asyncio.ensure_future(_fetch_fleet(feed))
            _inflight[feed.key] = task
            task.add_done_callback(lambda t, k=feed.key: _forget_task(k, t))
        try:
            await asyncio.wait_for(asyncio.shield(task), config.INTERNAL_API_UPSTREAM_TIMEOUT)
        except asyncio.TimeoutError:
            raise ApiError(503, "upstream_timeout",
                           f"{feed.brand} did not answer within "
                           f"{config.INTERNAL_API_UPSTREAM_TIMEOUT:g}s; try again shortly")
        except Exception:
            err = _fetch_errors.get(feed.key)
            code, message = (err[1], err[2]) if err else ("upstream_unavailable",
                                                          f"{feed.brand} fleet fetch failed")
            raise ApiError(503, code, message)
        entry = vitalityeld.latest_raw(feed.company_key, feed.base_url)
    if entry is None or not entry.rows:
        raise ApiError(503, "upstream_empty",
                       f"{feed.brand} returned no vehicles for this carrier")
    return entry


def snapshot_json(entry: vitalityeld.RawFleet, now: datetime) -> dict:
    return {"fetched_at": iso(entry.fetched_at), "age_seconds": _age(entry.fetched_at, now),
            "source": entry.source, "vehicle_count": len(entry.rows)}


# ── drivers: names and phones ────────────────────────────────────────────────

@dataclass
class DriverRecord:
    id: str
    name: str
    phone: str

    def json(self, with_phone: bool = True) -> dict:
        out = {"id": self.id, "name": self.name or None}
        if with_phone:
            out["phone"] = self.phone or None
        return out


def _record(row: dict, driver_id: str = "") -> DriverRecord:
    name = _clean(f"{row.get('first_name') or ''} {row.get('last_name') or ''}") \
        or _clean(row.get("username"))
    phone = next((str(row.get(f)).strip() for f in _PHONE_FIELDS
                  if str(row.get(f) or "").strip()), "")
    return DriverRecord(str(row.get("driver_id") or driver_id).strip(), name, phone)


_roster_index: Dict[str, Tuple[float, Dict[str, DriverRecord]]] = {}
_roster_retry_at: Dict[str, float] = {}
_roster_locks: Dict[str, asyncio.Lock] = {}
# (company_key, driver_id) -> (monotonic expiry, record or None)
_single_drivers: Dict[Tuple[str, str], Tuple[float, Optional[DriverRecord]]] = {}


def _indexed(company_key: str, cached: Tuple[float, List[dict]]) -> Dict[str, DriverRecord]:
    memo = _roster_index.get(company_key)
    if memo and memo[0] == cached[0]:
        return memo[1]
    index = {}
    for row in cached[1]:
        rec = _record(row)
        if rec.id:
            index[rec.id] = rec
    _roster_index[company_key] = (cached[0], index)
    return index


async def roster(feed: Feed) -> Dict[str, DriverRecord]:
    """{driver_id: record} from the hourly roster. Never raises: a failed
    fetch keeps the old roster (or none) and is retried after ROSTER_RETRY."""
    ck = feed.company_key
    cached = vitalityeld.roster_rows(ck)
    if cached and time.monotonic() - cached[0] < ROSTER_TTL:
        return _indexed(ck, cached)
    lock = _roster_locks.setdefault(ck, asyncio.Lock())
    async with lock:
        cached = vitalityeld.roster_rows(ck)
        if cached and time.monotonic() - cached[0] < ROSTER_TTL:
            return _indexed(ck, cached)
        if time.monotonic() < _roster_retry_at.get(ck, 0):
            return _indexed(ck, cached) if cached else {}
        try:
            rows = await _get_pages(feed, "/v2/drivers", _DRIVER_PAGE_SIZE)
        except Exception as e:
            logger.warning("Internal API: %s roster fetch for %s failed: %s",
                           feed.brand, feed.company.name, type(e).__name__)
            _roster_retry_at[ck] = time.monotonic() + ROSTER_RETRY
            return _indexed(ck, cached) if cached else {}
        vitalityeld.remember_roster(ck, rows)
        return _indexed(ck, vitalityeld.roster_rows(ck))


async def driver(feed: Feed, driver_id: str, *, single_fetch: bool = True
                 ) -> Optional[DriverRecord]:
    """The driver's record: the roster's, else (single_fetch) ``/v2/driver/{id}``
    remembered for ROSTER_TTL. None when DriveHOS knows no such driver."""
    rec = (await roster(feed)).get(driver_id)
    if rec is not None or not single_fetch or not driver_id_ok(driver_id):
        return rec
    ck = (feed.company_key, driver_id)
    cached = _single_drivers.get(ck)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    try:
        env = await _get(feed, f"/v2/driver/{driver_id}")
    except Exception as e:
        logger.info("Internal API: %s driver fetch failed: %s", feed.brand, type(e).__name__)
        return None
    data = env.get("data")
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    rec = _record(data, driver_id) if isinstance(data, dict) else None
    _single_drivers[ck] = (time.monotonic() + ROSTER_TTL, rec)
    return rec


# ── duty status ──────────────────────────────────────────────────────────────

# (company_key, driver_id) -> (monotonic expiry, item or None, fetched_at, error)
_duty: Dict[Tuple[str, str], Tuple[float, Optional[dict], datetime, str]] = {}


def duty_json(item: Optional[dict], fetched_at: datetime, now: datetime) -> dict:
    code = _clean((item or {}).get("current_status")).upper() or None
    on_duty = (True if code in _ON_DUTY else
               False if code in _OFF_DUTY or code == _PERSONAL_CONVEYANCE else None)
    return {
        "status": code,
        "on_duty": on_duty,
        "personal_conveyance": code == _PERSONAL_CONVEYANCE,
        "yard_move": code == _YARD_MOVE,
        "username": _clean((item or {}).get("username")) or None,
        "fetched_at": iso(fetched_at),
        "age_seconds": _age(fetched_at, now),
    }


async def duty(feed: Feed, driver_id: str) -> Tuple[Optional[dict], str, datetime]:
    """(latest-driver-status item or None, error code or "", when it was
    fetched). Cached DUTY_TTL (errors DUTY_ERROR_TTL). Never raises."""
    ck = (feed.company_key, driver_id)
    cached = _duty.get(ck)
    if cached and time.monotonic() < cached[0]:
        return cached[1], cached[3], cached[2]
    try:
        env = await _get(feed, "/v2/latest-driver-status", {"driver_id": driver_id})
    except Exception as e:
        code = "upstream_auth_rejected" if isinstance(e, _Refused) else "upstream_unavailable"
        logger.info("Internal API: %s duty status failed: %s", feed.brand, type(e).__name__)
        _duty[ck] = (time.monotonic() + DUTY_ERROR_TTL, None, _now(), code)
        return None, code, _duty[ck][2]
    data = env.get("data")
    items = [data] if isinstance(data, dict) else [d for d in (data or []) if isinstance(d, dict)]
    item = next((d for d in items if str(d.get("driver_id") or "") == driver_id), None)
    if item is None and len(items) == 1 and not items[0].get("driver_id"):
        item = items[0]
    _duty[ck] = (time.monotonic() + DUTY_TTL, item, _now(), "")
    return item, "", _duty[ck][2]


# ── vehicles ─────────────────────────────────────────────────────────────────

def _position(row: dict) -> Optional[dict]:
    lat, lon = _num(row.get("lat")), _num(row.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None  # "0","0" = no fix
    return {"lat": lat, "lon": lon}


def _heading(row: dict) -> Optional[float]:
    for field in _HEADING_FIELDS:
        v = _num(row.get(field))
        if v is not None and -360 <= v <= 360:
            return v % 360.0
    return None


def vehicle_json(row: dict, entry: vitalityeld.RawFleet, drivers: Dict[str, DriverRecord],
                 now: datetime, with_phone: bool = True) -> dict:
    """One vehicle as the API returns it. with_phone=False (the bulk fleet
    listing) leaves the driver's phone out altogether."""
    reported = _parse_time(row.get("timestamp"))
    age = _age(reported, now)
    status = _clean(row.get("status")).upper() or None
    vehicle_id = _clean(row.get("vehicle_id")) or None
    active = None
    if entry.active_ids is not None and vehicle_id:
        active = vehicle_id in entry.active_ids
    driver_id = _clean(row.get("driver_id")) or None
    rec = drivers.get(driver_id) if driver_id else None
    return {
        "number": _clean(row.get("number")),
        "vehicle_id": vehicle_id,
        "vin": _clean(row.get("vin")) or None,
        "active": active,
        "status": status,
        "online": bool(status != "OFFLINE" and age is not None
                       and age <= config.ELD_STALE_THRESHOLD),
        "position": _position(row),
        "location": _clean(row.get("calc_location")) or None,
        "speed": _num(row.get("speed")),
        "heading": _heading(row),
        "odometer": _num(row.get("odometer")),
        "last_report_at": iso(reported),
        "report_age_seconds": age,
        "driver": (rec or DriverRecord(driver_id, "", "")).json(with_phone)
        if driver_id else None,
    }


def match_unit(entry: vitalityeld.RawFleet, unit: str) -> List[Tuple[dict, str]]:
    return unit_match.match_rows(entry.rows, unit)


def _reset() -> None:
    """Tests only."""
    global _companies_cache
    _companies_cache = (0.0, [])
    _fetch_errors.clear()
    _inflight.clear()
    _roster_index.clear()
    _roster_retry_at.clear()
    _roster_locks.clear()
    _single_drivers.clear()
    _duty.clear()
