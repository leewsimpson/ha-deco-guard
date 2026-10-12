"""Deco Guard: TP-Link Deco device blocking and new-device quarantine for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import DecoClient
from .const import DOMAIN, STORAGE_VERSION
from .coordinator import DecoGuardCoordinator
from .panel import async_register_panel, async_unregister_panel
from .services import async_register_services
from .websocket import async_register_websocket

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type DecoGuardConfigEntry = ConfigEntry[DecoGuardCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_register_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DecoGuardConfigEntry) -> bool:
    client = DecoClient(entry.data[CONF_HOST], entry.data[CONF_PASSWORD])
    coordinator = DecoGuardCoordinator(hass, entry, client)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: DecoGuardConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DecoGuardConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.client.close)
        if not [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]:
            async_unregister_panel(hass)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: DecoGuardConfigEntry) -> None:
    """Delete the stored device review list. Router blocks are left as they are."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
