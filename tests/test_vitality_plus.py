"""Carriers that moved to Vitality Plus keep their company key but need the Plus
provider key; the old provider key answers 403 (or an empty fleet)."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from data import config
from tests.conftest import run
from utils.eld import vitalityeld
from utils.eld.vitalityeld import DriveHOSAuthError

OLD, PLUS = "old-provider", "plus-provider"
UNITS = [{"number": "055", "vehicle_id": "v1", "timestamp": "2026-10-23T10:00:00Z"}]


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "VITALITY_PLUS_PROVIDER_KEY", PLUS)
    vitalityeld._provider_used.clear()
    vitalityeld._active_cache.clear()
    yield
    vitalityeld._provider_used.clear()
    vitalityeld._active_cache.clear()


def _fetch(old_answer, brand="Vitality"):
    """old_answer: 'forbidden' | 'empty' — how the old provider key is treated."""
    calls = []

    async def status(request):
        key = request.headers["X-API-Provider-Key"]
        calls.append(key)
        if key == PLUS:
            return web.json_response({"description": "success", "data": UNITS})
        if old_answer == "forbidden":
            return web.json_response(
                {"status_code": 403, "description": "Unauthorized", "data": None},
                status=403,
            )
        return web.json_response({"description": "success", "data": None})

    async def go():
        app = web.Application()
        app.router.add_get("/v2/latest-vehicle-status", status)
        server = TestServer(app)
        await server.start_server()
        try:
            units = await vitalityeld.fetch_units(
                "company-key", OLD, str(server.make_url("")), brand=brand
            )
            return units, calls
        finally:
            await server.close()
    return run(go())


def test_plus_key_used_when_old_key_is_forbidden():
    units, calls = _fetch("forbidden")
    assert vitalityeld.lookup(units, "055") is not None
    assert calls == [OLD, PLUS]


def test_plus_key_used_when_old_key_sees_an_empty_fleet():
    units, _ = _fetch("empty")
    assert vitalityeld.lookup(units, "055") is not None


def test_working_key_is_remembered():
    _fetch("forbidden")
    _, calls = _fetch("forbidden")
    assert calls == [PLUS]  # no wasted request with the old key


def test_zero_never_falls_back():
    with pytest.raises(DriveHOSAuthError):
        _fetch("forbidden", brand="Zero")


def test_both_keys_forbidden_still_raises(monkeypatch):
    monkeypatch.setattr(config, "VITALITY_PLUS_PROVIDER_KEY", "other-plus")
    with pytest.raises(DriveHOSAuthError):
        _fetch("forbidden")
