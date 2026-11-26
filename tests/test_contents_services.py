"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.emile import async_remove_entry
from custom_components.emile.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN
from custom_components.emile.contents import CONTENTS_STORE_MAX_CODES, ContentsStore
from custom_components.emile.coordinator import EmileCoordinator
from custom_components.emile.parcels import normalize_parcel

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

CLIENT = "custom_components.emile.api.EmileApiClient"
REFRESH = f"{EmileCoordinator.__module__}.EmileCoordinator"
UNTRACKED = "EM299999999995CA"


async def _setup(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: active_sample(code)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> None:
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: active_sample(code)),
    ):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


def test_a_parcel_normalises_with_contents_none():
    assert normalize_parcel(delivered_sample())["contents"] is None


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Winter socks"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "  "}
    )
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: "em2999-99999992 ca", "contents": "Running shoes"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert entry.runtime_data.coordinator.contents_store.get(UNTRACKED) == (
        "Running shoes"
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass, [ACTIVE_CODE])
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: "  "}, blocking=True
        )


async def test_update_contents_needs_a_loaded_entry(hass):
    await _setup(hass, [ACTIVE_CODE])
    unloaded = MagicMock(state=ConfigEntryState.NOT_LOADED)
    with (
        patch("custom_components.emile.services._resolve_entry", return_value=unloaded),
        pytest.raises(ServiceValidationError),
    ):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "x"},
            blocking=True,
        )


async def test_update_contents_never_polls_and_notifies_once_per_change(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: UNTRACKED}, blocking=True
        )
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE},
            blocking=True,
        )
        assert listeners.call_count == 2
        await hass.async_block_till_done()
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_track_parcel_with_contents_stores_it_and_refreshes(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_track_parcel_without_contents_leaves_the_store_alone(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})
    assert _contents(entry, ACTIVE_CODE) is None


async def test_track_parcel_on_a_tracked_code_sets_contents_without_polling(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        assert listeners.call_count == 1
        await hass.async_block_till_done()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_parcel_with_contents_needs_a_loaded_entry(hass):
    await _setup(hass)
    unloaded = MagicMock(state=ConfigEntryState.NOT_LOADED)
    with (
        patch("custom_components.emile.services._resolve_entry", return_value=unloaded),
        pytest.raises(ServiceValidationError),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "x"},
            blocking=True,
        )


async def test_untrack_drops_the_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    assert store.get(ACTIVE_CODE) == "Running shoes"

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert store.get(ACTIVE_CODE) is None


async def test_the_store_keeps_the_newest_two_hundred_codes(hass):
    entry = await _setup(hass)
    store = entry.runtime_data.coordinator.contents_store
    for index in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"CODE{index}", "Shoes")
    assert store.get("CODE0") is None
    assert store.get("CODE1") == "Shoes"
    assert store.get(f"CODE{CONTENTS_STORE_MAX_CODES}") == "Shoes"


async def test_an_entry_without_a_store_file_loads_and_polls(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert _contents(entry, ACTIVE_CODE) is None


async def test_a_stored_value_survives_a_reload(hass, hass_storage):
    hass_storage[f"{DOMAIN}.contents.fixed"] = {
        "version": 1,
        "key": f"{DOMAIN}.contents.fixed",
        "data": {"by_code": {ACTIVE_CODE: "Running shoes"}},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        entry_id="fixed",
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_removing_the_entry_deletes_the_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    store = ContentsStore(hass, entry.entry_id)
    store.async_set(ACTIVE_CODE, "Running shoes")
    await store._store.async_save({"by_code": {ACTIVE_CODE: "Running shoes"}})
    assert f"{DOMAIN}.contents.{entry.entry_id}" in hass_storage

    await async_remove_entry(hass, entry)

    assert f"{DOMAIN}.contents.{entry.entry_id}" not in hass_storage


async def test_reapply_reaches_delivered_parcels(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    delivered = normalize_parcel(delivered_sample())
    coordinator.delivered = [delivered]
    coordinator._parcel_codes[id(delivered)] = DELIVERED_CODE
    coordinator.contents_store.async_set(DELIVERED_CODE, "Winter socks")

    coordinator.async_reapply_contents()

    assert delivered["contents"] == "Winter socks"


@pytest.mark.parametrize("barcode", [None, "EM299999999999CA"])
async def test_contents_follow_the_requested_code_not_the_barcode(hass, barcode):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    real = normalize_parcel

    def fake_normalize(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["barcode"] = barcode
        return parcel

    with (
        patch(
            f"{EmileCoordinator.__module__}.normalize_parcel",
            side_effect=fake_normalize,
        ),
        patch(
            f"{CLIENT}.async_get_parcel",
            new=AsyncMock(side_effect=lambda code: active_sample(code)),
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        coordinator.contents_store.async_set(ACTIVE_CODE, "Running shoes")
        coordinator.async_reapply_contents()
        assert coordinator.data[0]["contents"] == "Running shoes"
        assert coordinator.data[0]["barcode"] == barcode

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data[0]["contents"] == "Running shoes"
