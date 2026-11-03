"""A stale ELD record that still sits on the truck's position is not a
disconnection; one the truck has driven away from is."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from utils.eld.detector import find_anomalies

NOW = datetime(2026, 11, 4, 12, 0, 0)
STALE = NOW - timedelta(hours=1)


def _truck(lat, lon):
    return SimpleNamespace(unit_number="4008", vin=None, latitude=lat, longitude=lon)


def _eld(lat, lon, reported=STALE):
    return SimpleNamespace(
        vin=None, last_report_time=reported, latitude=lat, longitude=lon
    )


def _find(truck, eld, tolerance=1.0):
    return find_anomalies(
        [truck], {}, vitality_lookup={"4008": eld},
        threshold_seconds=600, location_tolerance_miles=tolerance, now=NOW,
    )


def test_stale_record_far_from_the_truck_is_flagged():
    assert len(_find(_truck(39.75, -94.17), _eld(37.10, -94.31))) == 1


def test_stale_record_on_the_truck_is_not_flagged():
    assert _find(_truck(39.7500, -94.1700), _eld(39.7510, -94.1710)) == []


def test_missing_coordinates_fall_back_to_staleness():
    assert len(_find(_truck(39.75, -94.17), _eld(None, None))) == 1
    assert len(_find(_truck(None, None), _eld(39.75, -94.17))) == 1


def test_zero_tolerance_turns_the_check_off():
    assert len(_find(_truck(39.7500, -94.1700), _eld(39.7500, -94.1700), 0)) == 1
