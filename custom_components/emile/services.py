"""Services for the eMile parcel tracker integration.

`emile.track_parcel` / `emile.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
can start tracking a parcel straight from a dashboard. `emile.update_contents`
sets or clears the text describing what is in a parcel, tracked or not.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import normalize_tracking_code, valid_tracking_code
from .const import CONF_CONTENTS, CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)


def _resolve_entry(hass: HomeAssistant):
    """Return the single eMile hub, or raise when it is not set up."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("eMile is not set up")
    return entries[0]


def _loaded_entry(hass: HomeAssistant):
    """Return the single eMile hub once it is loaded, or raise."""
    entry = _resolve_entry(hass)
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("eMile is not loaded")
    return entry


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the eMile services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid eMile tracking code"
            )
        entry = _loaded_entry(hass) if CONF_CONTENTS in call.data else None
        coordinator = entry.runtime_data.coordinator if entry else None
        entry = entry or _resolve_entry(hass)

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == tracking_code for p in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            ):
                coordinator.async_reapply_contents()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            )
        parcels.append({CONF_TRACKING_CODE: tracking_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        if entry.state is ConfigEntryState.LOADED:
            entry.runtime_data.coordinator.contents_store.async_set(tracking_code, None)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    async def _update_contents(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not tracking_code:
            raise ServiceValidationError("A tracking code is required")
        coordinator = _loaded_entry(hass).runtime_data.coordinator
        if coordinator.contents_store.async_set(
            tracking_code, call.data.get(CONF_CONTENTS)
        ):
            coordinator.async_reapply_contents()

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CONTENTS,
        _update_contents,
        schema=_UPDATE_CONTENTS_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the eMile services (single-entry integration, so on unload)."""
    for service in (
        SERVICE_TRACK_PARCEL,
        SERVICE_UNTRACK_PARCEL,
        SERVICE_UPDATE_CONTENTS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
