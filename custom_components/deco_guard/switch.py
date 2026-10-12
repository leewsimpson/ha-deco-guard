"""Switches: per-device network access, and the auto-block toggle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DecoGuardConfigEntry
from .const import DOMAIN, SIGNAL_NEW_APPROVED, STATUS_APPROVED
from .coordinator import DecoGuardCoordinator


def hub_device_info(entry: DecoGuardConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Deco Guard",
        manufacturer="TP-Link",
        model="Deco",
        configuration_url=entry.runtime_data.client.host,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DecoGuardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_devices(macs: list[str]) -> None:
        new = [m for m in macs if m not in added]
        added.update(new)
        async_add_entities(DeviceAccessSwitch(coordinator, mac) for mac in new)

    async_add_entities([AutoBlockSwitch(coordinator, entry)])
    add_devices(coordinator.macs_with_status(STATUS_APPROVED))
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_APPROVED.format(entry.entry_id), add_devices)
    )


class DeviceAccessSwitch(CoordinatorEntity[DecoGuardCoordinator], SwitchEntity):
    """On = the device may use the network, off = blocked at the Deco.

    The Deco block list refuses the device's MAC at the router, so unlike app-level
    parental controls it can't be bypassed with a VPN or alternate DNS.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "network_access"

    def __init__(self, coordinator: DecoGuardCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{mac}_network_access"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac.lower().replace("-", ":"))},
            name=coordinator.display_name(mac),
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    @property
    def available(self) -> bool:
        return super().available and self.mac in self.coordinator.devices

    @property
    def is_on(self) -> bool:
        return self.mac not in self.coordinator.data.blocked

    @property
    def icon(self) -> str:
        return "mdi:lan-connect" if self.is_on else "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        view = self.coordinator.device_view(self.mac)
        return {k: view[k] for k in ("mac", "ip", "online", "status", "client_type", "last_seen")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_unblock(self.mac)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_block(self.mac)


class AutoBlockSwitch(CoordinatorEntity[DecoGuardCoordinator], SwitchEntity):
    """Block every never-seen-before device until it is approved."""

    _attr_has_entity_name = True
    _attr_translation_key = "auto_block"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:shield-account"

    def __init__(self, coordinator: DecoGuardCoordinator, entry: DecoGuardConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_auto_block"
        self._attr_device_info = hub_device_info(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.auto_block

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_block(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_block(False)
