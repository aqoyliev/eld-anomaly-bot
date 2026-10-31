"""The internal HTTP API: an aiohttp server in the bot's own event loop.

Started from app.on_startup only when INTERNAL_API_KEY is set; it listens on
PORT (Railway's convention). Routes:

  GET /health                                   no auth
  GET /api/internal/vitality/companies
  GET /api/internal/vitality/fleet?company=
  GET /api/internal/vitality/unit?company=&unit=
  GET /api/internal/vitality/driver?company=&driver_id=

Every /api/internal route needs the X-Api-Key header (constant-time compare)
and answers 404 while INTERNAL_API_KEY is empty. The contract is in
docs/internal-api.md.
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from data import config

from . import vitality as facts
from .vitality import ApiError

logger = logging.getLogger(__name__)

PREFIX = "/api/internal/vitality/"
API_VERSION = 1


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _internal_key_refusal(request: web.Request) -> Optional[web.Response]:
    """None when the request may go on; else the 404 (API off) or 401 answer."""
    expected = config.INTERNAL_API_KEY
    if not expected:
        return _json({"error": "not_found", "message": "not found"}, 404)
    sent = request.headers.get("X-Api-Key", "")
    if not secrets.compare_digest(sent.encode(), expected.encode()):
        return _json({"error": "unauthorized", "message": "bad or missing X-Api-Key"}, 401)
    return None


@web.middleware
async def _errors(request: web.Request, handler):
    try:
        response = await handler(request)
    except ApiError as e:
        response = _json(e.body(), e.status)
    except web.HTTPNotFound:
        response = _json({"error": "not_found", "message": "not found"}, 404)
    except web.HTTPMethodNotAllowed:
        response = _json({"error": "method_not_allowed", "message": "method not allowed"}, 405)
    except Exception:
        logger.exception("Internal API: %s failed", request.path)
        response = _json({"error": "internal_error", "message": "internal error"}, 500)
    if request.path.startswith(PREFIX):
        logger.info("Internal API: %s %s -> %s", request.method, request.path, response.status)
    return response


# ── parameters ───────────────────────────────────────────────────────────────

def _param(request: web.Request, name: str, *, required: bool = True,
           max_len: int = 100) -> str:
    value = request.query.get(name, "").strip()
    if required and not value:
        raise ApiError(400, "bad_request", f"{name} is required")
    if len(value) > max_len:
        raise ApiError(400, "bad_request", f"{name} is too long")
    return value


def _max_age(request: web.Request) -> int:
    raw = request.query.get("max_age", "").strip()
    if not raw:
        return facts.clamp_max_age(None)
    if not raw.isdigit():
        raise ApiError(400, "bad_request", "max_age must be a whole number of seconds")
    return facts.clamp_max_age(int(raw))


def _flag(request: web.Request, name: str, default: bool) -> bool:
    raw = request.query.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise ApiError(400, "bad_request", f"{name} must be 1 or 0")


async def _feed(request: web.Request) -> facts.Feed:
    company = await facts.resolve_company(_param(request, "company"))
    return facts.select_feed(company, request.query.get("provider", ""))


def _head(feed: facts.Feed, entry=None) -> dict:
    out = {"api_version": API_VERSION, "company": facts.company_json(feed.company),
           "provider": feed.provider_name(entry.provider_key if entry else None)}
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── handlers ─────────────────────────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return _json({"status": "ok"})


async def api_companies(request: web.Request) -> web.Response:
    if (refusal := _internal_key_refusal(request)) is not None:
        return refusal
    alias_map = facts.aliases()
    out = []
    for c in await facts.companies():
        names = sorted(a for a, t in alias_map.items() if facts._by_target([c], t))
        out.append({**facts.company_json(c), "aliases": names,
                    "vitality": bool(c.vitality_company_key),
                    "zero": bool(c.zero_company_key)})
    return _json({"api_version": API_VERSION, "companies": out})


async def api_fleet(request: web.Request) -> web.Response:
    if (refusal := _internal_key_refusal(request)) is not None:
        return refusal
    max_age = _max_age(request)
    feed = await _feed(request)
    entry = await facts.fleet(feed, max_age)
    drivers = await facts.roster(feed) if any(r.get("driver_id") for r in entry.rows) else {}
    now = _now()
    vehicles = [facts.vehicle_json(r, entry, drivers, now) for r in entry.rows
                if str(r.get("number") or "").strip()]
    return _json({**_head(feed, entry), "snapshot": facts.snapshot_json(entry, now),
                  "vehicles": vehicles})


async def api_unit(request: web.Request) -> web.Response:
    if (refusal := _internal_key_refusal(request)) is not None:
        return refusal
    unit = _param(request, "unit", max_len=64)
    max_age = _max_age(request)
    with_duty = _flag(request, "duty", True)
    feed = await _feed(request)
    entry = await facts.fleet(feed, max_age)
    now = _now()
    matches = facts.match_unit(entry, unit)
    if not matches:
        raise ApiError(404, "unit_not_found", f"unit {unit!r} is not in the {feed.brand} fleet",
                       snapshot=facts.snapshot_json(entry, now))

    drivers = {}
    ids = [str(r.get("driver_id")).strip() for r, _ in matches if r.get("driver_id")]
    for did in dict.fromkeys(i for i in ids if i):
        rec = await facts.driver(feed, did)
        if rec is not None:
            drivers[did] = rec

    out = []
    duty_lookups = 0
    for row, how in matches:
        item = {**facts.vehicle_json(row, entry, drivers, now), "match": how}
        did = (item["driver"] or {}).get("id")
        if with_duty and did:
            if duty_lookups < facts.MAX_DUTY_LOOKUPS and facts.driver_id_ok(did):
                duty_lookups += 1
                status_item, error, fetched_at = await facts.duty(feed, did)
                item["duty"] = None if error else facts.duty_json(status_item, fetched_at, _now())
                item["duty_error"] = error or None
            else:
                item["duty"], item["duty_error"] = None, "skipped"
        else:
            item["duty"], item["duty_error"] = None, None
        out.append(item)
    return _json({**_head(feed, entry), "snapshot": facts.snapshot_json(entry, now),
                  "query": unit, "ambiguous": len(out) > 1, "matches": out})


async def api_driver(request: web.Request) -> web.Response:
    if (refusal := _internal_key_refusal(request)) is not None:
        return refusal
    driver_id = _param(request, "driver_id", max_len=64)
    if not facts.driver_id_ok(driver_id):
        raise ApiError(400, "bad_request", "driver_id may hold only letters, digits, - and _")
    with_duty = _flag(request, "duty", True)
    feed = await _feed(request)
    rec = await facts.driver(feed, driver_id)
    if rec is None:
        raise ApiError(404, "driver_not_found",
                       f"{feed.brand} knows no driver {driver_id!r} for this company")
    out = {**_head(feed), "driver": rec.json(), "duty": None, "duty_error": None}
    if with_duty:
        status_item, error, fetched_at = await facts.duty(feed, driver_id)
        out["duty"] = None if error else facts.duty_json(status_item, fetched_at, _now())
        out["duty_error"] = error or None
    return _json(out)


def build_app() -> web.Application:
    app = web.Application(middlewares=[_errors])
    app.router.add_get("/health", health)
    app.router.add_get(PREFIX + "companies", api_companies)
    app.router.add_get(PREFIX + "fleet", api_fleet)
    app.router.add_get(PREFIX + "unit", api_unit)
    app.router.add_get(PREFIX + "driver", api_driver)
    return app


_runner: Optional[web.AppRunner] = None


async def start() -> Optional[web.AppRunner]:
    """Serve the API on PORT. Does nothing (returns None) without
    INTERNAL_API_KEY."""
    global _runner
    if not config.INTERNAL_API_KEY or _runner is not None:
        return _runner
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.INTERNAL_API_HOST or None, config.PORT).start()
    _runner = runner
    logger.info("Internal API listening on port %s", config.PORT)
    return runner


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
    await facts.close()
