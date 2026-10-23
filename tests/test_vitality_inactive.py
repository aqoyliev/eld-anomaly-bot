"""Trucks deactivated in the Vitality panel stay in /v2/latest-vehicle-status as
OFFLINE but are absent from /v2/vehicles; the client must drop them so a truck
parked at the yard does not raise a "disconnected" alert."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.conftest import run
from utils.eld import vitalityeld

STATUS = [
    {"number": "055", "vehicle_id": "v1", "timestamp": "2026-10-23T10:00:00Z"},
    {"number": "294181", "vehicle_id": "v2", "timestamp": "2026-10-16T05:51:47Z"},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    vitalityeld._active_cache.clear()
    yield
    vitalityeld._active_cache.clear()


def _fetch(vehicles_handler):
    async def status(request):
        return web.json_response({"description": "success", "data": STATUS})

    async def go():
        app = web.Application()
        app.router.add_get("/v2/latest-vehicle-status", status)
        app.router.add_get("/v2/vehicles", vehicles_handler)
        server = TestServer(app)
        await server.start_server()
        try:
            return await vitalityeld.fetch_units(
                "company-key", "provider-key", str(server.make_url(""))
            )
        finally:
            await server.close()
    return run(go())


def test_deactivated_truck_is_dropped():
    async def vehicles(request):
        return web.json_response({"description": "success", "data": [
            {"number": "055", "vehicle_id": "v1", "active": True},
        ]})
    units = _fetch(vehicles)
    assert vitalityeld.lookup(units, "055") is not None
    assert vitalityeld.lookup(units, "294181") is None


def test_vehicle_list_failure_does_not_hide_the_fleet():
    async def vehicles(request):
        return web.Response(status=500)
    units = _fetch(vehicles)
    assert vitalityeld.lookup(units, "055") is not None
    assert vitalityeld.lookup(units, "294181") is not None


def test_empty_vehicle_list_does_not_hide_the_fleet():
    async def vehicles(request):
        return web.json_response({"description": "success", "data": None})
    units = _fetch(vehicles)
    assert vitalityeld.lookup(units, "055") is not None
    assert vitalityeld.lookup(units, "294181") is not None
