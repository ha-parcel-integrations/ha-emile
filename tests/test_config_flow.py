"""Tests for the eMile config and options flow."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emile.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.emile.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code(" em2999-99999991 ca") == "EM299999999991CA"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


@pytest.mark.parametrize(
    "code", ["EM299999999991CA", "EM100000000000CA", "EM000000000000CA"]
)
def test_valid_tracking_code_accepts_the_documented_shape(code):
    assert valid_tracking_code(code)


@pytest.mark.parametrize(
    "code",
    [
        "",
        "ABC",
        "EM29999999991CA",
        "EM2999999999911CA",
        "EM299999999991US",
        "XX299999999991CA",
        "EM29999999999ACA",
        "EM299999999991CA<",
        " EM299999999991CA",
        "EM299999999991CA\n",
    ],
)
def test_valid_tracking_code_rejects_other_shapes(code):
    assert not valid_tracking_code(code)


async def test_user_flow_creates_hub_without_input(hass):
    """No account, no postcode — the entry is created straight away."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "eMile"
    assert result["options"][CONF_PARCELS] == []


async def test_second_hub_rejected(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    # single_config_entry in the manifest aborts before the flow runs.
    assert result["reason"] == "single_instance_allowed"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels},
    )


def _settings_input(
    *,
    history=False,
    filter_type="days",
    amount=7,
) -> dict:
    """Build the settings-form submission."""
    return {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: amount,
        CONF_INCLUDE_HISTORY: history,
    }


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_add_parcel(hass):
    entry = _hub([])
    entry.add_to_hass(hass)

    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["em299999999991ca"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EM299999999991CA"}]


async def test_options_add_code_with_separators(hass):
    """Pasted codes with spaces/dashes are sanitised like the consumer site."""
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["em2999-99999991 ca"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EM299999999991CA"}]


async def test_options_rejects_a_code_of_the_wrong_shape(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["abc"]}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_tracking_code"}
    assert entry.options[CONF_PARCELS] == []


async def test_options_invalid_code_keeps_what_was_typed(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    typed = ["EM299999999996CA", "abc"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": typed}
    )
    field = next(iter(result["data_schema"].schema))
    assert field.description["suggested_value"] == typed


async def test_options_de_duplicates_tracking_codes(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EM299999999996CA"}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["EM299999999996CA", "em299999999996ca"]}
    )
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EM299999999996CA"}]


async def test_options_remove_parcel(hass):
    entry = _hub(
        [
            {CONF_TRACKING_CODE: "EM299999999996CA"},
            {CONF_TRACKING_CODE: "EM299999999994CA"},
        ]
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["EM299999999994CA"]}
    )
    assert result["type"] == "create_entry"
    codes = {p[CONF_TRACKING_CODE] for p in result["data"][CONF_PARCELS]}
    assert codes == {"EM299999999994CA"}


async def test_options_can_clear_the_tracked_code_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EM299999999996CA"}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_options_changes_history_and_delivered(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _settings_input(
            history=True,
            filter_type="parcels",
            amount=5,
        ),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
