"""Websocket API used by the Deco Guard panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .coordinator import DecoGuardCoordinator
from .services import get_coordinator

_ACTIONS = {
    "block": DecoGuardCoordinator.async_block,
    "unblock": DecoGuardCoordinator.async_unblock,
    "approve": DecoGuardCoordinator.async_approve,
    "deny": DecoGuardCoordinator.async_deny,
    "forget": DecoGuardCoordinator.async_forget,
}


def _state(coordinator: DecoGuardCoordinator) -> dict[str, Any]:
    macs = set(coordinator.devices)
    if coordinator.data:
        macs |= set(coordinator.data.online) | set(coordinator.data.blocked)
        macs -= set(coordinator.data.nodes)
    return {
        "auto_block": coordinator.auto_block,
        "last_update_success": coordinator.last_update_success,
        "devices": [coordinator.device_view(mac) for mac in sorted(macs)],
    }


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_state)
    websocket_api.async_register_command(hass, ws_action)
    websocket_api.async_register_command(hass, ws_settings)


@websocket_api.websocket_command({vol.Required("type"): "deco_guard/state"})
@websocket_api.require_admin
@callback
def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        coordinator = get_coordinator(hass)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return
    connection.send_result(msg["id"], _state(coordinator))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "deco_guard/action",
        vol.Required("action"): vol.In([*_ACTIONS, "rename"]),
        vol.Required("mac"): str,
        vol.Optional("name"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_action(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    try:
        coordinator = get_coordinator(hass)
        if msg["action"] == "rename":
            await coordinator.async_rename(msg["mac"], msg.get("name", ""))
        else:
            await _ACTIONS[msg["action"]](coordinator, msg["mac"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _state(coordinator))


@websocket_api.websocket_command(
    {vol.Required("type"): "deco_guard/settings", vol.Required("auto_block"): bool}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    try:
        coordinator = get_coordinator(hass)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return
    await coordinator.async_set_auto_block(msg["auto_block"])
    connection.send_result(msg["id"], _state(coordinator))
