"""Summary sensors: pending (quarantined), blocked and online device counts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DecoGuardConfigEntry
from .const import STATUS_PENDING
from .coordinator import DecoGuardCoordinator
from .switch import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DecoGuardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DecoGuardSensor(coordinator, entry, key, icon)
        for key, icon in (
            ("pending_devices", "mdi:account-question"),
            ("blocked_devices", "mdi:cancel"),
            ("online_devices", "mdi:devices"),
        )
    )


class DecoGuardSensor(CoordinatorEntity[DecoGuardCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(
        self,
        coordinator: DecoGuardCoordinator,
        entry: DecoGuardConfigEntry,
        key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = hub_device_info(entry)

    def _macs(self) -> list[str]:
        data = self.coordinator.data
        if self._key == "pending_devices":
            return self.coordinator.macs_with_status(STATUS_PENDING)
        if self._key == "blocked_devices":
            return sorted(data.blocked)
        return sorted(set(data.online) - set(data.nodes))

    @property
    def native_value(self) -> int:
        return len(self._macs())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._key == "online_devices":
            return {}
        return {
            "devices": [
                {
                    k: v
                    for k, v in self.coordinator.device_view(m).items()
                    if k in ("mac", "name", "ip", "status", "first_seen")
                }
                for m in self._macs()
            ]
        }
