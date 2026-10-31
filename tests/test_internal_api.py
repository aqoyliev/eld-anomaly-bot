"""The internal HTTP API (utils/internal_api): auth, each endpoint, errors, and
that it serves the poller's data / its own short caches instead of asking
DriveHOS again. DriveHOS is a local fake; every name, phone and key here is
made up."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data import config
from tests.conftest import run
from utils.eld import store, vitalityeld
from utils.internal_api import server as api_server
from utils.internal_api import unit_match
from utils.internal_api import vitality as facts

API_KEY = "test-internal-key-0123456789-abcdefghij"  # >= 32 chars, made up
PRIMARY = "test-provider-key"   # conftest's VITALITY_PROVIDER_KEY
PLUS = "test-plus-key"
CARRIER_KEY = "carrier-key-one"
ZERO_KEY = "carrier-key-zero"
H = {"X-Api-Key": API_KEY}
P = "/api/internal/vitality/"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


def _rows():
    return [
        {"number": "4063", "vehicle_id": "v1", "vin": "TESTVIN0000000001", "driver_id": "d1",
         "timestamp": _iso(NOW - timedelta(seconds=30)), "lat": "35.1", "lon": "-111.6",
         "speed": 0, "status": "STATIONARY", "odometer": "1000", "calc_location": "Test place"},
        {"number": "4063z", "vehicle_id": "v2", "driver_id": "d2",
         "timestamp": _iso(NOW - timedelta(seconds=20)), "lat": "32.7", "lon": "-97.3",
         "speed": "61", "status": "IN_MOTION", "heading": "90"},
        {"number": "0942", "vehicle_id": "v3", "driver_id": "d3",
         "timestamp": _iso(NOW - timedelta(days=3)), "lat": "0", "lon": "0",
         "status": "OFFLINE"},
        {"number": "7001", "vehicle_id": "v4", "timestamp": _iso(NOW), "lat": "40", "lon": "-90",
         "status": "STATIONARY"},
    ]


ROSTER = [
    {"driver_id": "d1", "first_name": "TEST ", "last_name": "DRIVER  ONE",
     "phone": "+15550100001"},
    {"driver_id": "d2", "first_name": "Test", "last_name": "Driver Two",
     "mobile": "+15550100002"},
]
STATUSES = {"d1": "DS_OFF", "d2": "DS_D", "d3": "DR_IND_PC", "d9": "DS_ON"}


class FakeDriveHOS:
    """Answers like DriveHOS; `mode` changes latest-vehicle-status."""

    def __init__(self):
        self.calls = []          # (path, provider key)
        self.mode = "ok"         # ok | error | forbidden | empty | slow | plus_only
        self.rows = _rows()

    def _refused(self, request):
        key = request.headers.get("X-API-Provider-Key")
        if self.mode == "plus_only" and key != PLUS:
            return web.json_response({"status_code": 403, "description": "Unauthorized",
                                      "data": None}, status=403)
        return None

    def app(self):
        async def status(request):
            self.calls.append((request.path, request.headers.get("X-API-Provider-Key")))
            if (r := self._refused(request)) is not None:
                return r
            if self.mode == "error":
                return web.Response(status=500, text="<html>oops</html>")
            if self.mode == "forbidden":
                return web.json_response({"status_code": 403, "description": "Unauthorized"},
                                         status=403)
            if self.mode == "empty":
                return web.json_response({"description": "success", "data": None})
            if self.mode == "slow":
                await asyncio.sleep(1.0)
            return web.json_response({"description": "success", "data": self.rows,
                                      "total_pages": 1})

        async def vehicles(request):
            self.calls.append((request.path, request.headers.get("X-API-Provider-Key")))
            if (r := self._refused(request)) is not None:
                return r
            data = [{"vehicle_id": "v1"}, {"vehicle_id": "v2"}, {"vehicle_id": "v4"},
                    {"vehicle_id": "v3", "active": False}]
            return web.json_response({"description": "success", "data": data})

        async def drivers(request):
            self.calls.append((request.path, request.headers.get("X-API-Provider-Key")))
            if (r := self._refused(request)) is not None:
                return r
            return web.json_response({"description": "success", "data": ROSTER,
                                      "total_pages": 1})

        async def one_driver(request):
            self.calls.append((request.path, request.headers.get("X-API-Provider-Key")))
            if (r := self._refused(request)) is not None:
                return r
            did = request.match_info["did"]
            if did in ("d3", "d9"):
                return web.json_response({"description": "success", "data": {
                    "driver_id": did, "first_name": "Test", "last_name": "Driver " + did,
                    "phone_number": "+1555010000" + did[-1]}})
            return web.json_response({"description": "success", "data": None})

        async def driver_status(request):
            self.calls.append((request.path, request.headers.get("X-API-Provider-Key")))
            if (r := self._refused(request)) is not None:
                return r
            did = request.query.get("driver_id")
            if did not in STATUSES:
                return web.json_response({"description": "success", "data": []})
            return web.json_response({"description": "success", "data": [
                {"driver_id": did, "current_status": STATUSES[did], "username": "user_" + did}]})

        app = web.Application()
        app.router.add_get("/v2/latest-vehicle-status", status)
        app.router.add_get("/v2/vehicles", vehicles)
        app.router.add_get("/v2/drivers", drivers)
        app.router.add_get("/v2/driver/{did}", one_driver)
        app.router.add_get("/v2/latest-driver-status", driver_status)
        return app

    def count(self, path):
        return sum(1 for p, _ in self.calls if p == path)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_KEY", API_KEY)
    monkeypatch.setattr(config, "INTERNAL_API_COMPANY_ALIASES", "")
    monkeypatch.setattr(config, "INTERNAL_API_UPSTREAM_TIMEOUT", 3.0)
    monkeypatch.setattr(config, "VITALITY_PROVIDER_KEY", PRIMARY)
    monkeypatch.setattr(config, "VITALITY_PLUS_PROVIDER_KEY", "")
    monkeypatch.setattr(config, "ZERO_PROVIDER_KEY", PRIMARY)
    for cache in (vitalityeld._provider_used, vitalityeld._active_cache,
                  vitalityeld._driver_cache, vitalityeld._latest_raw, vitalityeld._roster_rows):
        cache.clear()
    facts._reset()
    api_server._auth_failures.clear()
    yield
    api_server._auth_failures.clear()
    for cache in (vitalityeld._provider_used, vitalityeld._active_cache,
                  vitalityeld._driver_cache, vitalityeld._latest_raw, vitalityeld._roster_rows):
        cache.clear()
    facts._reset()


@asynccontextmanager
async def _env(monkeypatch, companies=(("JRD", CARRIER_KEY, None),)):
    """(api client, fake DriveHOS) with these companies (name, vitality key, zero key)."""
    for name, vkey, zkey in companies:
        await store.add_company(name=name, gomotive_token=None, quantum_token=None,
                                vitality_company_key=vkey, zero_company_key=zkey)
    fake = FakeDriveHOS()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")
    monkeypatch.setattr(config, "VITALITY_BASE_URL", base)
    monkeypatch.setattr(config, "ZERO_BASE_URL", base)
    client = TestClient(TestServer(api_server.build_app()))
    await client.start_server()
    try:
        yield client, fake
    finally:
        await client.close()
        await facts.close()
        for t in list(facts._inflight.values()):
            t.cancel()
        await upstream.close()


async def _get(client, path, headers=H, **params):
    resp = await client.get(path, params=params, headers=headers)
    return resp.status, await resp.json()


# ── auth and health ──────────────────────────────────────────────────────────

def test_health_needs_no_key(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            assert await _get(client, "/health", headers={}) == (200, {"status": "ok"})
    run(go())


def test_missing_or_wrong_key_is_401(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            for headers in ({}, {"X-Api-Key": "wrong"}, {"X-Api-Key": ""}):
                status, body = await _get(client, P + "fleet", headers=headers, company="jrd")
                assert status == 401 and body["error"] == "unauthorized"
            assert fake.calls == []
    run(go())


def test_routes_are_404_without_internal_api_key(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            monkeypatch.setattr(config, "INTERNAL_API_KEY", "")
            for route in ("companies", "fleet", "unit", "driver"):
                status, body = await _get(client, P + route, company="jrd", unit="4063")
                assert status == 404 and body["error"] == "not_found"
            assert fake.calls == []
    run(go())


def test_server_does_not_start_without_key(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_KEY", "")
    assert run(api_server.start()) is None


def test_server_refuses_a_short_key(monkeypatch, caplog):
    monkeypatch.setattr(config, "INTERNAL_API_KEY", "x" * 31)
    with caplog.at_level("ERROR"):
        assert run(api_server.start()) is None
    assert "shorter than 32 characters" in caplog.text
    assert "x" * 31 not in caplog.text


def test_server_starts_with_a_long_key(monkeypatch):
    monkeypatch.setattr(config, "PORT", 0)  # any free port

    async def go():
        try:
            return await api_server.start()
        finally:
            await api_server.stop()
    assert run(go()) is not None


# ── failed-key throttle ──────────────────────────────────────────────────────

def _xff(ip, key="wrong-key"):
    return {"X-Api-Key": key, "X-Forwarded-For": f"{ip}, 10.0.0.1"}


def test_bad_keys_are_throttled_per_ip(db, monkeypatch, caplog):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            with caplog.at_level("INFO"):
                for _ in range(api_server.AUTH_FAIL_LIMIT):
                    status, _b = await _get(client, P + "fleet", headers=_xff("203.0.113.7"),
                                            company="jrd")
                    assert status == 401
                # the 11th, even with the right key, is refused
                resp = await client.get(P + "fleet", params={"company": "jrd"},
                                        headers=_xff("203.0.113.7", API_KEY))
                body = await resp.json()
                assert resp.status == 429 and body["error"] == "too_many_requests"
                assert 0 < int(resp.headers["Retry-After"]) <= 301
            # the key presented is never logged
            assert "wrong-key" not in caplog.text and API_KEY not in caplog.text
            assert "203.0.113.7" in caplog.text  # the throttled IP is
            # another client (first X-Forwarded-For hop) is not affected
            status, _b = await _get(client, P + "fleet", headers=_xff("198.51.100.2", API_KEY),
                                    company="jrd")
            assert status == 200
            # once the failures leave the window, the IP is let in again
            failures = api_server._auth_failures["203.0.113.7"]
            for i in range(len(failures)):
                failures[i] -= api_server.AUTH_FAIL_WINDOW
            status, _b = await _get(client, P + "fleet", headers=_xff("203.0.113.7", API_KEY),
                                    company="jrd")
            assert status == 200 and "203.0.113.7" not in api_server._auth_failures
    run(go())


def test_good_keys_are_never_throttled(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            for _ in range(api_server.AUTH_FAIL_LIMIT + 5):
                status, _b = await _get(client, P + "companies")
                assert status == 200
            assert api_server._auth_failures == {}
    run(go())


def test_client_ip_without_forwarded_header():
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/", headers={})
    assert api_server.client_ip(req)  # falls back to the peer, never empty
    req = make_mocked_request("GET", "/", headers={"X-Forwarded-For": " 192.0.2.5 , 10.1.1.1"})
    assert api_server.client_ip(req) == "192.0.2.5"


def test_unknown_route_is_json_404(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            status, body = await _get(client, P + "nope")
            assert status == 404 and body["error"] == "not_found"
    run(go())


# ── companies ────────────────────────────────────────────────────────────────

def test_companies_lists_mapping_without_keys(db, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_COMPANY_ALIASES", "jrdx=JRD")

    async def go():
        async with _env(monkeypatch, companies=(("JRD", CARRIER_KEY, None),
                                                ("Zero Co", None, ZERO_KEY))) as (client, _):
            resp = await client.get(P + "companies", headers=H)
            text = await resp.text()
            body = await resp.json()
            assert resp.status == 200
            assert CARRIER_KEY not in text and ZERO_KEY not in text
            jrd = next(c for c in body["companies"] if c["name"] == "JRD")
            assert jrd["vitality"] is True and jrd["zero"] is False and jrd["aliases"] == ["jrdx"]
    run(go())


def test_company_resolution(db, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_COMPANY_ALIASES", "acme=Acme Freight LLC")

    async def go():
        async with _env(monkeypatch, companies=(("JRD", CARRIER_KEY, None),
                                                ("Acme Freight LLC", CARRIER_KEY, None),
                                                ("A.B.C", CARRIER_KEY, None),
                                                ("abc", CARRIER_KEY, None),
                                                ("No Key", None, None))) as (client, _):
            # by name, ignoring case and punctuation
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 200 and body["company"]["name"] == "JRD"
            # by alias, by numeric id
            status, body = await _get(client, P + "fleet", company="ACME")
            assert status == 200 and body["company"]["name"] == "Acme Freight LLC"
            status, body = await _get(client, P + "fleet", company=str(body["company"]["id"]))
            assert status == 200 and body["company"]["name"] == "Acme Freight LLC"
            # unknown, ambiguous, no Vitality key
            status, body = await _get(client, P + "fleet", company="nobody")
            assert status == 404 and body["error"] == "unknown_company"
            status, body = await _get(client, P + "fleet", company="abc")
            assert status == 409 and body["error"] == "company_ambiguous"
            status, body = await _get(client, P + "fleet", company="no key")
            assert status == 404 and body["error"] == "not_configured"
    run(go())


# ── fleet ────────────────────────────────────────────────────────────────────

def test_fleet_snapshot(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 200
            assert body["provider"] == "vitality" and body["api_version"] == 1
            snap = body["snapshot"]
            assert snap["source"] == "on_demand" and snap["vehicle_count"] == 4
            assert 0 <= snap["age_seconds"] <= 5
            by_number = {v["number"]: v for v in body["vehicles"]}
            # deactivated trucks are listed, flagged
            assert by_number["0942"]["active"] is False and by_number["4063"]["active"] is True
            v = by_number["4063"]
            assert v["position"] == {"lat": 35.1, "lon": -111.6}
            assert v["online"] is True and v["status"] == "STATIONARY"
            assert v["location"] == "Test place" and v["odometer"] == 1000.0
            assert 25 <= v["report_age_seconds"] <= 40
            # the bulk listing never carries phones
            assert v["driver"] == {"id": "d1", "name": "TEST DRIVER ONE"}
            assert by_number["4063z"]["heading"] == 90.0 and by_number["4063z"]["speed"] == 61.0
            assert by_number["4063z"]["driver"] == {"id": "d2", "name": "Test Driver Two"}
            off = by_number["0942"]
            assert off["online"] is False and off["position"] is None
            assert off["driver"] == {"id": "d3", "name": None}  # not in roster
            assert by_number["7001"]["driver"] is None
            # the fleet listing never asks for duty statuses
            assert fake.count("/v2/latest-driver-status") == 0
    run(go())


def test_fleet_listing_has_no_phones_but_unit_and_driver_do(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            resp = await client.get(P + "fleet", params={"company": "jrd"}, headers=H)
            text = await resp.text()
            assert resp.status == 200 and "phone" not in text and "+1555" not in text
            status, body = await _get(client, P + "unit", company="jrd", unit="4063")
            assert body["matches"][0]["driver"]["phone"] == "+15550100001"
            assert body["matches"][1]["driver"]["phone"] == "+15550100002"
    run(go())


def test_fleet_is_cached_and_shared(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            await asyncio.gather(*(_get(client, P + "fleet", company="jrd") for _ in range(5)))
            await _get(client, P + "unit", company="jrd", unit="7001")
            assert fake.count("/v2/latest-vehicle-status") == 1
            assert fake.count("/v2/drivers") == 1
    run(go())


def test_poller_data_is_reused(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            # What the poller does each cycle.
            await vitalityeld.fetch_units(CARRIER_KEY, PRIMARY, config.VITALITY_BASE_URL)
            fetched = fake.count("/v2/latest-vehicle-status")
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 200 and body["snapshot"]["source"] == "poller"
            assert fake.count("/v2/latest-vehicle-status") == fetched
            # the poller's roster serves names too
            assert fake.count("/v2/drivers") == 1
    run(go())


def test_old_data_is_refetched_per_max_age(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            await _get(client, P + "fleet", company="jrd")
            vitalityeld.latest_raw(CARRIER_KEY, config.VITALITY_BASE_URL).fetched_mono -= 100
            # 100 s old: fine for the default 120 s, too old for max_age=60
            await _get(client, P + "fleet", company="jrd")
            assert fake.count("/v2/latest-vehicle-status") == 1
            status, body = await _get(client, P + "fleet", company="jrd", max_age="60")
            assert status == 200 and body["snapshot"]["age_seconds"] <= 5
            assert fake.count("/v2/latest-vehicle-status") == 2
            # max_age below the floor is clamped: no refetch within MIN_MAX_AGE
            await _get(client, P + "fleet", company="jrd", max_age="0")
            assert fake.count("/v2/latest-vehicle-status") == 2
    run(go())


def test_roster_fetch_feeds_the_pollers_name_cache(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            await _get(client, P + "fleet", company="jrd")
            assert vitalityeld._driver_cache[CARRIER_KEY][1]["d1"] == "TEST DRIVER ONE"
            await vitalityeld.fetch_units(CARRIER_KEY, PRIMARY, config.VITALITY_BASE_URL)
            assert fake.count("/v2/drivers") == 1
    run(go())


# ── upstream failures ────────────────────────────────────────────────────────

def test_upstream_error_is_503_and_remembered(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            fake.mode = "error"
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 503 and body["error"] == "upstream_unavailable"
            assert "oops" not in body["message"]
            calls = fake.count("/v2/latest-vehicle-status")
            fake.mode = "ok"
            status, body = await _get(client, P + "unit", company="jrd", unit="4063")
            assert status == 503 and body["retry_after_seconds"] > 0
            assert fake.count("/v2/latest-vehicle-status") == calls
    run(go())


def test_upstream_refusal_and_empty_fleet(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            fake.mode = "forbidden"
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 503 and body["error"] == "upstream_auth_rejected"
            facts._fetch_errors.clear()
            fake.mode = "empty"
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 503 and body["error"] == "upstream_empty"
    run(go())


def test_upstream_timeout(db, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_UPSTREAM_TIMEOUT", 0.2)

    async def go():
        async with _env(monkeypatch) as (client, fake):
            fake.mode = "slow"
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 503 and body["error"] == "upstream_timeout"
            # the fetch went on in the background; the next request gets it
            await asyncio.sleep(1.2)
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 200 and fake.count("/v2/latest-vehicle-status") == 1
    run(go())


def test_provider_key_missing_is_503(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            monkeypatch.setattr(config, "VITALITY_PROVIDER_KEY", "")
            status, body = await _get(client, P + "fleet", company="jrd")
            assert status == 503 and body["error"] == "provider_not_configured"
    run(go())


# ── unit ─────────────────────────────────────────────────────────────────────

def test_unit_variants_are_ambiguous_with_duty(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            status, body = await _get(client, P + "unit", company="jrd", unit="4063")
            assert status == 200 and body["query"] == "4063" and body["ambiguous"] is True
            first, second = body["matches"]
            assert (first["number"], first["match"]) == ("4063", "exact")
            assert (second["number"], second["match"]) == ("4063z", "variant")
            assert first["duty"]["status"] == "DS_OFF" and first["duty"]["on_duty"] is False
            assert second["duty"]["status"] == "DS_D" and second["duty"]["on_duty"] is True
            assert second["duty"]["username"] == "user_d2" and second["duty_error"] is None
            assert 0 <= second["duty"]["age_seconds"] <= 5
            assert second["driver"]["name"] == "Test Driver Two"
            # duty is cached
            await _get(client, P + "unit", company="jrd", unit="4063")
            assert fake.count("/v2/latest-driver-status") == 2
    run(go())


def test_unit_suffix_and_leading_zeros(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            status, body = await _get(client, P + "unit", company="jrd", unit="4063z")
            assert [m["number"] for m in body["matches"]] == ["4063z", "4063"]
            status, body = await _get(client, P + "unit", company="jrd", unit="942")
            assert status == 200 and body["ambiguous"] is False
            m = body["matches"][0]
            assert m["number"] == "0942" and m["active"] is False
            # a driver missing from the roster is fetched alone
            assert m["driver"]["name"] == "Test Driver d3"
            assert m["duty"]["personal_conveyance"] is True and m["duty"]["on_duty"] is False
            status, body = await _get(client, P + "unit", company="jrd", unit="7001 O/O")
            assert status == 200 and body["matches"][0]["number"] == "7001"
            assert body["matches"][0]["driver"] is None and body["matches"][0]["duty"] is None
    run(go())


def test_unit_without_duty(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            status, body = await _get(client, P + "unit", company="jrd", unit="4063", duty="0")
            assert status == 200 and all(m["duty"] is None for m in body["matches"])
            assert fake.count("/v2/latest-driver-status") == 0
    run(go())


def test_unit_not_found(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, _):
            status, body = await _get(client, P + "unit", company="jrd", unit="5555")
            assert status == 404 and body["error"] == "unit_not_found"
            assert body["snapshot"]["vehicle_count"] == 4
    run(go())


def test_bad_input_is_400(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            cases = [
                (P + "fleet", {}),                                        # no company
                (P + "unit", {"company": "jrd"}),                         # no unit
                (P + "unit", {"company": "jrd", "unit": "4063", "max_age": "soon"}),
                (P + "unit", {"company": "jrd", "unit": "4063", "duty": "maybe"}),
                (P + "fleet", {"company": "jrd", "provider": "other"}),
                (P + "unit", {"company": "jrd", "unit": "9" * 65}),
                (P + "driver", {"company": "jrd", "driver_id": "../v2/drivers"}),
                (P + "driver", {"company": "jrd"}),
            ]
            for path, params in cases:
                status, body = await _get(client, path, **params)
                assert status == 400 and body["error"] == "bad_request", (path, params)
            assert fake.calls == []
    run(go())


# ── driver ───────────────────────────────────────────────────────────────────

def test_driver_endpoint(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            status, body = await _get(client, P + "driver", company="jrd", driver_id="d1")
            assert status == 200
            assert body["driver"] == {"id": "d1", "name": "TEST DRIVER ONE",
                                      "phone": "+15550100001"}
            assert body["duty"]["status"] == "DS_OFF"
            # not in the roster: fetched alone, once
            status, body = await _get(client, P + "driver", company="jrd", driver_id="d9")
            assert status == 200 and body["driver"]["phone"] == "+15550100009"
            assert body["duty"]["on_duty"] is True
            await _get(client, P + "driver", company="jrd", driver_id="d9", duty="0")
            assert fake.count("/v2/driver/d9") == 1
            status, body = await _get(client, P + "driver", company="jrd", driver_id="nobody")
            assert status == 404 and body["error"] == "driver_not_found"
    run(go())


def test_duty_failure_is_reported_not_fatal(db, monkeypatch):
    async def go():
        async with _env(monkeypatch) as (client, fake):
            await _get(client, P + "fleet", company="jrd")

            async def broken(request):
                return web.Response(status=502)
            # Point duty requests at a dead upstream after the fleet is cached.
            dead = web.Application()
            dead.router.add_get("/v2/latest-driver-status", broken)
            dead_server = TestServer(dead)
            await dead_server.start_server()
            entry = vitalityeld.latest_raw(CARRIER_KEY, config.VITALITY_BASE_URL)
            monkeypatch.setattr(config, "VITALITY_BASE_URL",
                                str(dead_server.make_url("")).rstrip("/"))
            vitalityeld._latest_raw[(config.VITALITY_BASE_URL, CARRIER_KEY)] = entry
            try:
                status, body = await _get(client, P + "unit", company="jrd", unit="4063",
                                          duty="1")
            finally:
                await dead_server.close()
            assert status == 200
            assert body["matches"][0]["duty"] is None
            assert body["matches"][0]["duty_error"] == "upstream_unavailable"
    run(go())


# ── Vitality Plus and Zero ───────────────────────────────────────────────────

def test_vitality_plus_carrier(db, monkeypatch):
    monkeypatch.setattr(config, "VITALITY_PLUS_PROVIDER_KEY", PLUS)

    async def go():
        async with _env(monkeypatch) as (client, fake):
            fake.mode = "plus_only"
            status, body = await _get(client, P + "unit", company="jrd", unit="4063z")
            assert status == 200 and body["provider"] == "vitality_plus"
            assert body["matches"][0]["duty"]["status"] == "DS_D"
            # once Plus is known, duty / roster requests go straight to it
            assert all(k == PLUS for p, k in fake.calls
                       if p in ("/v2/latest-driver-status", "/v2/drivers"))
    run(go())


def test_zero_provider(db, monkeypatch):
    async def go():
        async with _env(monkeypatch, companies=(("Zero Co", None, ZERO_KEY),)) as (client, _):
            status, body = await _get(client, P + "unit", company="zero co", unit="4063")
            assert status == 200 and body["provider"] == "zero"
            status, body = await _get(client, P + "fleet", company="zero co",
                                      provider="vitality")
            assert status == 404 and body["error"] == "not_configured"
    run(go())


# ── unit matching rules ──────────────────────────────────────────────────────

def test_unit_match_rules():
    assert unit_match.normalize_unit(" t-120 ") == "T120"
    assert unit_match.unit_candidates("4063   O/O") == ["4063OO", "4063"]
    assert unit_match.unit_candidates("O/O 4063") == ["OO4063"]
    assert unit_match.unit_base("4063z") == "4063"
    assert unit_match.unit_base("0942 SUB") == "942"
    assert unit_match.unit_base("T-120") == "T120"
    rows = [{"number": "4063"}, {"number": "40630"}, {"number": "4063 O/O"}, {"number": ""}]
    got = [(r["number"], how) for r, how in unit_match.match_rows(rows, "4063")]
    assert got == [("4063", "exact"), ("4063 O/O", "variant")]
    assert unit_match.match_rows(rows, "999") == []
