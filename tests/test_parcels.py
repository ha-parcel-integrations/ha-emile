"""Tests for the pure parcel-mapping helpers (no Home Assistant needed)."""

from datetime import UTC, datetime, timedelta, timezone
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emile.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.emile.parcels import (
    apply_delivered_filter,
    build_history,
    event_instant,
    map_event_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import ACTIVE_CODE, active_sample, delivered_sample, row, sample

R = ParcelStatus


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("Waybill Generated for REF000000000000", R.REGISTERED),
        ("WAYBILL GENERATED", R.REGISTERED),
        ("PREALERT", R.REGISTERED),
        ("CUSTOMS CLEARANCE FORECAST", R.IN_TRANSIT),
        ("NOTIFICATION OF ARRIVAL", R.IN_TRANSIT),
        ("CBSA HAS PUT THE FOLLOWING SHIPMENTS ON HOLD", R.IN_TRANSIT),
        ("CUSTOMS DETENTION", R.IN_TRANSIT),
        ("CUSTOMS RELEASED", R.IN_TRANSIT),
        ("CUSTOMS CLEARANCE COMPLETED", R.IN_TRANSIT),
        ("HANDED OVER", R.IN_TRANSIT),
        ("PICKED UP", R.IN_TRANSIT),
        ("IN TRANSIT", R.IN_TRANSIT),
        ("ARRIVE TRANSITHUB", R.IN_TRANSIT),
        ("ARRIVE AT THE WAREHOUSE", R.IN_TRANSIT),
        ("ORDER SORTED", R.IN_TRANSIT),
        ("TASK ASSIGNED", R.IN_TRANSIT),
        ("ASSIGN DRIVER", R.IN_TRANSIT),
        ("TRANSFERRED TO DRIVER", R.IN_TRANSIT),
        ("OUTBOUND SCANNED", R.IN_TRANSIT),
        ("OUT FOR DELIVERY", R.OUT_FOR_DELIVERY),
        ("RE-DELIVERY ATTEMPT", R.OUT_FOR_DELIVERY),
        ("DELIVERED", R.DELIVERED),
        ("  delivered ", R.DELIVERED),
        ("RETURNED TO SENDER", R.RETURNING),
        ("LOST CONFIRMED", R.PROBLEM),
        ("ORDER DESTROYED", R.PROBLEM),
        ("DELIVERY EXCEPTION", R.PROBLEM),
        ("EXCEPTION NOTIFIED TO SENDER", R.PROBLEM),
    ],
)
def test_map_event_status_known(wording, expected):
    assert map_event_status(wording) is expected


def test_wording_with_the_waybill_reference_is_matched_by_prefix_only():
    assert map_event_status("WAYBILL GENERATED FOR ANYTHING") is R.REGISTERED


@pytest.mark.parametrize(
    "wording", ["EMAIL REMINDER SENT", "EMAIL REMINDER FAILED", "STORAGE_FEE", ""]
)
def test_statusless_rows_map_to_none_without_a_warning(wording, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_event_status(wording) is None
    assert caplog.records == []


def test_none_wording_is_none():
    assert map_event_status(None) is None


def test_unmapped_wording_warns_once_with_issue_link(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_event_status("SOMETHING NEW") is None
        assert map_event_status("SOMETHING NEW") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SOMETHING NEW" in warnings[0].getMessage()
    assert "template=unrecognised_status.yml" in warnings[0].getMessage()


def test_waiting_for_pickup_is_the_warehouse_stage(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_event_status("WAITING FOR PICKUP") is R.IN_TRANSIT
    assert not caplog.records


@pytest.mark.parametrize(
    "wording",
    [
        "PACKAGE ON THE WAY TO PARTNER SORTING FACILITY",
        "IN TRANSIT TO EMIL. TRACKING WILL UPDATE ON ARRIVAL.",
        "WITH PARTNER CARRIER, TRANSFERRING TO EMIL SOON.",
        "PRE_INBOUND",
        "PRE ASSIGNED",
        "DELIVERY MAY BE DELAYED DUE TO",
        "ORDER EXCEPTION RELEASED",
    ],
)
def test_network_wordings_are_in_transit(wording):
    assert map_event_status(wording) is R.IN_TRANSIT


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-05-01T10:00:00Z") == datetime(2026, 5, 1, 10, tzinfo=UTC)
    assert parse_iso("2026-05-01T10:00:00") == datetime(2026, 5, 1, 10, tzinfo=UTC)
    assert parse_iso("nonsense") is None
    assert parse_iso(None) is None


# --- event clock ------------------------------------------------------------


def test_event_instant_applies_the_gmt_offset():
    instant = event_instant(row("X", "2026-04-04 16:34:04", "GMT-04:00"))
    assert instant == datetime(
        2026, 4, 4, 16, 34, 4, tzinfo=timezone(timedelta(hours=-4))
    )
    assert instant.astimezone(UTC).hour == 20


def test_event_instant_positive_and_half_hour_offsets():
    instant = event_instant(row("X", "2026-04-04 16:34:04", "GMT+05:30"))
    assert instant.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    ("date", "zone"),
    [
        ("2026-04-04 16:34:04", "EST"),
        ("2026-04-04 16:34:04", ""),
        ("not a date", "GMT+00:00"),
        ("2026-13-45 16:34:04", "GMT+00:00"),
        ("2026-04-04 16:34:04", "GMT+24:00"),
    ],
)
def test_event_instant_unreadable_is_none(date, zone):
    assert event_instant(row("X", date, zone)) is None


# --- history ----------------------------------------------------------------


def test_history_is_oldest_to_newest_and_collapses_the_waybill_reference():
    history = build_history(delivered_sample()["events"])
    assert history[0] == {
        "timestamp": "2026-03-30T16:18:23+00:00",
        "status": R.REGISTERED,
        "raw_status": "Waybill Generated",
    }
    assert history[-1]["status"] is R.DELIVERED
    assert "REF000000000000" not in str(history)


def test_history_orders_by_instant_across_offsets():
    """A UTC row can be written later than a local row that is older."""
    events = [
        row("ORDER SORTED", "2026-04-07 08:00:00", "GMT-04:00"),
        row("TASK ASSIGNED", "2026-04-07 11:00:00", "GMT+00:00"),
    ]
    assert [h["raw_status"] for h in build_history(events)] == [
        "TASK ASSIGNED",
        "ORDER SORTED",
    ]


def test_history_keeps_wire_order_for_equal_clocks():
    events = [
        row("TASK ASSIGNED", "2026-04-07 08:00:00", number=1),
        row("ORDER SORTED", "2026-04-07 08:00:00", number=2),
        row("OUTBOUND SCANNED", "2026-04-07 08:00:00", number=3),
    ]
    assert [h["raw_status"] for h in build_history(events)] == [
        "TASK ASSIGNED",
        "ORDER SORTED",
        "OUTBOUND SCANNED",
    ]


def test_repeated_task_assigned_rows_are_kept():
    events = [
        row("TASK ASSIGNED", "2026-04-06 08:00:00"),
        row("ORDER SORTED", "2026-04-07 08:00:00"),
        row("TASK ASSIGNED", "2026-04-08 08:00:00"),
    ]
    assert [h["raw_status"] for h in build_history(events)] == [
        "TASK ASSIGNED",
        "ORDER SORTED",
        "TASK ASSIGNED",
    ]


def test_history_caps_to_the_most_recent_events():
    events = [row("IN TRANSIT", f"2026-04-{day:02d} 08:00:00") for day in range(1, 26)]
    history = build_history(events, max_events=20)
    assert len(history) == 20
    assert history[-1]["timestamp"].startswith("2026-04-25")


def test_unreadable_clock_keeps_its_place_and_warns_once(caplog):
    events = [
        row("OUT FOR DELIVERY", "2026-04-07 08:00:00"),
        row("DELIVERED", "2026-04-07 10:00:00", "GMT-4"),
        row("EMAIL REMINDER SENT", "2026-04-07 11:00:00", "GMT-4"),
    ]
    with caplog.at_level(logging.WARNING):
        history = build_history(events)
    assert [h["raw_status"] for h in history] == [
        "OUT FOR DELIVERY",
        "DELIVERED",
        "EMAIL REMINDER SENT",
    ]
    assert history[1]["timestamp"] == history[0]["timestamp"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "time_zone=GMT-4" in warnings[0].getMessage()


def test_final_delivery_with_an_unreadable_clock_still_delivers():
    raw = sample(
        ACTIVE_CODE,
        [
            row("OUT FOR DELIVERY", "2026-04-07 08:00:00"),
            row("DELIVERED", "2026-04-07 10:00:00", "GMT-4"),
        ],
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] is R.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is not None


def test_leading_row_without_any_instant_is_left_out_of_history():
    events = [
        row("WAYBILL GENERATED", "not a date"),
        row("ORDER SORTED", "2026-04-07 08:00:00"),
    ]
    assert [h["raw_status"] for h in build_history(events)] == ["ORDER SORTED"]


@pytest.mark.parametrize(
    "later",
    ["ORDER SORTED", "DELIVERY MAY BE DELAYED DUE TO", "EMAIL REMINDER SENT"],
)
def test_later_noise_does_not_undo_a_delivery(later):
    raw = delivered_sample()
    raw["events"].append(row(later, "2030-01-01 12:00:00"))
    parcel = normalize_parcel(raw)
    assert parcel["status"] is R.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "DELIVERED"


@pytest.mark.parametrize(
    ("later", "status"),
    [
        ("EXCEPTION NOTIFIED TO SENDER", R.PROBLEM),
        ("RETURNED TO SENDER", R.RETURNING),
        ("RE-DELIVERY ATTEMPT", R.OUT_FOR_DELIVERY),
    ],
)
def test_a_real_correction_overturns_a_delivery(later, status):
    raw = delivered_sample()
    raw["events"].append(row(later, "2030-01-01 12:00:00"))
    parcel = normalize_parcel(raw)
    assert parcel["status"] is status
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_older_unmapped_row_still_warns(caplog):
    raw = active_sample()
    raw["events"].insert(0, row("BRAND NEW WORDING", "2020-01-01 12:00:00"))
    with caplog.at_level(logging.WARNING):
        normalize_parcel(raw)
    assert "BRAND NEW WORDING" in caplog.text


def test_unmapped_warning_masks_digits_and_caps_length(caplog):
    with caplog.at_level(logging.WARNING):
        map_event_status("HOLD FOR SHIPMENT 12345 " + "X" * 100)
        map_event_status("HOLD FOR SHIPMENT 67890 " + "X" * 100)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "12345" not in message
    assert "HOLD FOR SHIPMENT #" in message
    assert "X" * 60 not in message


def test_history_tolerates_missing_and_malformed_events():
    assert build_history(None) == []
    assert build_history(["junk", 3]) == []


def test_history_keeps_no_mapping_as_none_status():
    events = [row("EMAIL REMINDER SENT", "2026-04-08 08:00:00")]
    assert build_history(events)[0]["status"] is None


# --- status resolution ------------------------------------------------------


CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["status"] is R.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "DELIVERED"
    # 14:30 at GMT-04:00
    assert parcel["delivered_at"] == "2026-04-07T14:30:00-04:00"
    assert parcel["history"] is None


def test_normalize_out_for_delivery_has_no_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] is R.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["barcode"] == ACTIVE_CODE


def test_fields_the_payload_lacks_are_literal_none():
    parcel = normalize_parcel(delivered_sample())
    for key in ("sender", "receiver", "pickup_point", "weight", "dimensions"):
        assert parcel[key] is None
    assert parcel["pickup"] is False


def test_url_is_the_tracking_deep_link():
    raw = delivered_sample()
    assert normalize_parcel(raw)["url"] == (
        f"https://www.emileps.com/tracking?tracking={raw['barcode']}"
    )
    assert tracking_url(None) is None


def test_normalize_history_is_opt_in():
    assert (
        len(normalize_parcel(delivered_sample(), include_history=True)["history"]) == 7
    )


def test_normalize_keeps_the_whole_parsed_reply_in_raw():
    raw = delivered_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_a_trailing_reminder_row_keeps_the_current_status():
    raw = active_sample()
    raw["events"].append(row("EMAIL REMINDER SENT", "2026-04-07 12:00:00"))
    raw["events"].append(row("STORAGE_FEE", "2026-04-07 13:00:00"))
    assert normalize_parcel(raw)["status"] is R.OUT_FOR_DELIVERY


def test_unmapped_newest_row_falls_through_to_the_newest_mappable_one(caplog):
    """A new wording after OUT FOR DELIVERY must not win or hide it."""
    raw = active_sample()
    raw["events"].append(row("BRAND NEW WORDING", "2026-04-07 12:00:00"))
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel(raw)
    assert parcel["status"] is R.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "OUT FOR DELIVERY"
    assert "BRAND NEW WORDING" in caplog.text


def test_only_unmapped_rows_give_unknown_with_a_warning(caplog):
    raw = sample(ACTIVE_CODE, [row("BRAND NEW WORDING", "2026-04-07 12:00:00")])
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel(raw)
    assert parcel["status"] is R.UNKNOWN
    assert parcel["raw_status"] == "BRAND NEW WORDING"
    assert "BRAND NEW WORDING" in caplog.text


def test_returning_and_problem_statuses():
    returned = sample(ACTIVE_CODE, [row("RETURNED TO SENDER", "2026-04-07 12:00:00")])
    lost = sample(ACTIVE_CODE, [row("LOST CONFIRMED", "2026-04-07 12:00:00")])
    assert normalize_parcel(returned)["status"] is R.RETURNING
    assert normalize_parcel(lost)["status"] is R.PROBLEM


def test_pending_placeholder_is_unknown_and_silent(caplog):
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel({"barcode": ACTIVE_CODE})
    assert parcel["status"] is R.UNKNOWN
    assert parcel["raw_status"] is None
    assert parcel["barcode"] == ACTIVE_CODE
    assert caplog.records == []


def test_waybill_reference_never_reaches_raw_status():
    raw = sample(
        ACTIVE_CODE,
        [
            row(
                "Waybill Generated for REF000000000000",
                "2026-03-30 16:18:23",
                "GMT+00:00",
            )
        ],
    )
    parcel = normalize_parcel(raw)
    assert parcel["raw_status"] == "Waybill Generated"
    assert parcel["status"] is R.REGISTERED


# --- capabilities -----------------------------------------------------------


def test_capabilities_are_known_values():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)
    absent = KNOWN_CAPABILITIES - CAPABILITIES - PENDING_CAPABILITIES
    assert "history" in CAPABILITIES
    assert with_history["history"]
    for field in ("weight", "dimensions", "pickup_point", "url"):
        if field in absent:
            assert delivered[field] is None
    if "delivery_window" in absent:
        assert delivered["planned_from"] is None
        assert delivered["planned_to"] is None


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
