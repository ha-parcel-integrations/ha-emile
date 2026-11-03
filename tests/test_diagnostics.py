"""Tests for eMile diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.components.diagnostics import async_redact_data

from custom_components.emile.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EM299999999991CA"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EM299999999991CA",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "barcode": "EM299999999991CA",
                "trande_no": "REF000000000000",
                "events": [
                    {
                        "status_desc": "DELIVERED",
                        "remark": "left at the side door",
                        "phone_number": "555-0100",
                        "postcode": "A1A1A1",
                        "event_info": "info",
                        "proof_images": {"image": "a"},
                    }
                ],
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


def test_every_free_text_and_reference_column_is_on_the_redaction_list():
    assert {
        "raw",
        "barcode",
        "trande_no",
        "next_carrier_no",
        "remark",
        "event_info",
        "postcode",
        "phone_number",
        "proof_images",
    } <= TO_REDACT


def test_nested_columns_are_redacted_when_raw_is_inspected_directly():
    row = {"remark": "x", "phone_number": "y", "status_desc": "DELIVERED"}
    assert async_redact_data({"events": [row]}, TO_REDACT) == {
        "events": [
            {
                "remark": "**REDACTED**",
                "phone_number": "**REDACTED**",
                "status_desc": "DELIVERED",
            }
        ]
    }
