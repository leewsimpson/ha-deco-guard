"""Services: block / unblock / approve / deny / forget devices (picked by device or MAC)."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import DecoGuardCoordinator

ATTR_DEVICE = "device_id"
ATTR_MAC = "mac"
ATTR_CONFIG_ENTRY = "config_entry_id"

SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_DEVICE): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_MAC): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        }
    ),
    cv.has_at_least_one_key(ATTR_DEVICE, ATTR_MAC),
)

RAW_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("operation", default="read"): cv.string,
        vol.Optional("params"): dict,
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
    }
)


def get_coordinator(hass: HomeAssistant, entry_id: str | None = None) -> DecoGuardCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise HomeAssistantError("Deco Guard is not set up")
    return entries[0].runtime_data


def resolve_macs(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    """MACs from the `mac` field plus the network MAC of each selected device."""
    macs = list(call.data.get(ATTR_MAC, []))
    registry = dr.async_get(hass)
    for device_id in call.data.get(ATTR_DEVICE, []):
        device = registry.async_get(device_id)
        if device is None:
            raise HomeAssistantError(f"Unknown device {device_id}")
        mac = next((v for t, v in device.connections if t == dr.CONNECTION_NETWORK_MAC), None)
        if mac is None:
            raise HomeAssistantError(f"{device.name_by_user or device.name} has no MAC address")
        macs.append(mac)
    return macs


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
            for mac in resolve_macs(hass, call):
                await method(coordinator, mac)

        hass.services.async_register(DOMAIN, name, handler, schema=SCHEMA)

    async def raw_request(call: ServiceCall) -> ServiceResponse:
        coordinator = get_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY))
        return {
            "response": await coordinator.async_raw(
                call.data["path"], call.data["operation"], call.data.get("params")
            )
        }

    hass.services.async_register(
        DOMAIN,
        "raw_request",
        raw_request,
        schema=RAW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
