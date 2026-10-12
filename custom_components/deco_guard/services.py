"""Services: block / unblock / approve / deny / forget a device by MAC."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import DecoGuardCoordinator

ATTR_MAC = "mac"
ATTR_CONFIG_ENTRY = "config_entry_id"

SCHEMA = vol.Schema({vol.Required(ATTR_MAC): cv.string, vol.Optional(ATTR_CONFIG_ENTRY): cv.string})


def get_coordinator(hass: HomeAssistant, entry_id: str | None = None) -> DecoGuardCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise HomeAssistantError("Deco Guard is not set up")
    return entries[0].runtime_data


@callback
def async_register_services(hass: HomeAssistant) -> None:
    actions = {
        "block_device": DecoGuardCoordinator.async_block,
        "unblock_device": DecoGuardCoordinator.async_unblock,
        "approve_device": DecoGuardCoordinator.async_approve,
        "deny_device": DecoGuardCoordinator.async_deny,
        "forget_device": DecoGuardCoordinator.async_forget,
    }

    for name, method in actions.items():

        async def handler(call: ServiceCall, method=method) -> None:
            coordinator = get_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY))
            await method(coordinator, call.data[ATTR_MAC])

        hass.services.async_register(DOMAIN, name, handler, schema=SCHEMA)
