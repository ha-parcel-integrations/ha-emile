"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.emile.calendar import EmileDeliveriesCalendar
from custom_components.emile.const import ParcelStatus
from custom_components.emile.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)
from custom_components.emile.sensor import EmileNextDeliverySensor

from .payloads import active_sample, delivered_sample, in_transit_sample, row, sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

ZONE = timezone(timedelta(hours=2))

SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "in_transit": in_transit_sample,
    "returning": lambda: sample(
        "EM1", [row("RETURNED TO SENDER", "2026-04-07 10:00:00")]
    ),
    "problem": lambda: sample(
        "EM1", [row("DELIVERY EXCEPTION", "2026-04-07 10:00:00")]
    ),
    "bare": lambda: {"barcode": "EM1"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None
    elif parcel["barcode"]:
        assert parcel["delivered_at"] is not None


def test_planned_is_empty_and_pickup_is_constant(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    assert start == "2099-04-29T13:00:00Z"
    assert datetime.fromisoformat(end) == datetime(2099, 4, 29, 23, 59, 59, tzinfo=ZONE)


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=ZONE)
    assert datetime.fromisoformat(start) == datetime(2099, 4, 29, tzinfo=ZONE)
    assert datetime.fromisoformat(end) == datetime(2099, 4, 29, 23, 59, 59, tzinfo=ZONE)


def test_a_window_keeps_both_ends():
    assert planned_window(
        "2099-04-29T09:00:00+02:00", "2099-04-29T12:00:00+02:00", zone=ZONE
    ) == ("2099-04-29T09:00:00+02:00", "2099-04-29T12:00:00+02:00")


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING],
)
def test_planned_is_emptied_when_done_or_waiting(status):
    assert planned_for_status(status, "a", "b") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def test_pickup_is_sticky_after_collection():
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(True, ParcelStatus.IN_TRANSIT)
    assert not is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.IN_TRANSIT])


def _cal_parcel(barcode, planned_from, planned_to=None):
    return {
        "barcode": barcode,
        "sender": None,
        "status": "in_transit",
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": None,
    }


def _coordinator(data):
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def test_next_delivery_skips_an_ended_window():
    sensor = EmileNextDeliverySensor(
        _coordinator(
            [
                _cal_parcel("OLD", "2000-01-01T10:00:00Z", "2000-01-01T23:59:59Z"),
                _cal_parcel("NEW", "2099-01-01T10:00:00Z"),
            ]
        ),
        MagicMock(entry_id="e1"),
    )
    assert sensor.native_value == datetime(2099, 1, 1, 10, tzinfo=UTC)


def test_calendar_shows_a_whole_day_window_as_all_day():
    calendar = EmileDeliveriesCalendar(
        _coordinator(
            [_cal_parcel("A", "2099-01-01T00:00:00Z", "2099-01-01T23:59:59Z")]
        ),
        MagicMock(entry_id="e1"),
    )
    (event,) = calendar._events()
    assert event.all_day
    assert event.end == event.start + timedelta(days=1)


def test_calendar_keeps_a_stretched_moment_timed():
    calendar = EmileDeliveriesCalendar(
        _coordinator(
            [_cal_parcel("A", "2099-01-01T10:00:00Z", "2099-01-01T23:59:59Z")]
        ),
        MagicMock(entry_id="e1"),
    )
    (event,) = calendar._events()
    assert not event.all_day
