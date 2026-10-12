"""Polling coordinator: reads the Deco, quarantines new devices, enforces blocks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DecoAuthError, DecoClient, DecoError, normalize_mac
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_NEW_DEVICE,
    PANEL_URL_PATH,
    SIGNAL_NEW_APPROVED,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Client fields kept for display (the block list only returns MACs, so names come from here).
_KEEP = ("name", "ip", "client_type", "wire_type", "connection_type", "interface")


@dataclass
class DecoData:
    """Snapshot of one poll."""

    online: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)


class DecoGuardCoordinator(DataUpdateCoordinator[DecoData]):
    """Owns the Deco client and the device review store."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DecoClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # {"seeded": bool, "auto_block": bool, "devices": {mac: {...}}}
        self.store_data: dict[str, Any] = {}

    # ----- store -----

    async def async_load(self) -> None:
        self.store_data = await self._store.async_load() or {
            "seeded": False,
            "auto_block": True,
            "devices": {},
        }

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.store_data, 1)

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        return self.store_data["devices"]

    @property
    def auto_block(self) -> bool:
        return self.store_data.get("auto_block", True)

    async def async_set_auto_block(self, enabled: bool) -> None:
        self.store_data["auto_block"] = enabled
        self._save()
        self.async_update_listeners()

    def macs_with_status(self, status: str) -> list[str]:
        return [mac for mac, dev in self.devices.items() if dev.get("status") == status]

    # ----- router access -----

    async def _call(self, fn, *args):
        async with self._lock:
            return await self.hass.async_add_executor_job(fn, *args)

    async def _async_update_data(self) -> DecoData:
        try:
            nodes = await self._call(self.client.get_device_list)
            clients = await self._call(self.client.get_client_list)
            blocked = await self._call(self.client.get_blocked_macs)
        except DecoAuthError as err:
            # Stop polling: repeated bad logins lock the admin account on the router.
            raise ConfigEntryAuthFailed(str(err)) from err
        except DecoError as err:
            raise UpdateFailed(str(err)) from err

        data = DecoData(
            online={normalize_mac(c["mac"]): c for c in clients if c.get("mac")},
            blocked=blocked,
            nodes={normalize_mac(n["mac"]): n for n in nodes if n.get("mac")},
        )
        await self._process(data)
        return data

    async def _process(self, data: DecoData) -> None:
        """Remember devices, quarantine new ones and re-apply quarantine blocks."""
        now = dt_util.utcnow().isoformat()
        changed = False

        for mac, client in data.online.items():
            dev = self.devices.get(mac)
            info = {k: client.get(k) for k in _KEEP}
            if dev is not None:
                if any(dev.get(k) != v for k, v in info.items() if v not in (None, "")):
                    dev.update({k: v for k, v in info.items() if v not in (None, "")})
                    changed = True
                dev["last_seen"] = now

        if not self.store_data.get("seeded"):
            # First run: everything already on the network (or already blocked) is trusted,
            # otherwise enabling the integration would block the whole house.
            for mac, client in data.online.items():
                self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
            for mac in data.blocked:
                self.devices.setdefault(mac, self._new_record(mac, {}, STATUS_APPROVED, now))
            self.store_data["seeded"] = True
            self._save()
            _LOGGER.info("Seeded %d known devices as approved", len(self.devices))
            return

        new_approved: list[str] = []
        new_pending: list[str] = []
        for mac, client in data.online.items():
            if mac in self.devices or mac in data.nodes:
                continue
            if mac in data.blocked:
                # Blocked outside HA (Deco app) but still listed: just remember it.
                self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
                new_approved.append(mac)
            elif self.auto_block:
                self.devices[mac] = self._new_record(mac, client, STATUS_PENDING, now)
                new_pending.append(mac)
            else:
                self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
                new_approved.append(mac)
            changed = True

        # Quarantined / denied devices must stay blocked, even if unblocked in the Deco app.
        enforce = [
            mac
            for mac in self.macs_with_status(STATUS_PENDING) + self.macs_with_status(STATUS_DENIED)
            if mac not in data.blocked and mac not in data.nodes
        ]
        if enforce:
            try:
                await self._call(self.client.block, enforce)
                data.blocked.update(enforce)
                for mac in enforce:
                    data.online.pop(mac, None)
                _LOGGER.info("Blocked unapproved devices: %s", ", ".join(enforce))
            except DecoError as err:
                _LOGGER.error("Failed to block unapproved devices %s: %s", enforce, err)

        for mac in new_pending:
            self._announce(mac)
        if changed:
            self._save()
        if new_approved:
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_APPROVED.format(self.config_entry.entry_id), new_approved
            )

    @staticmethod
    def _new_record(mac: str, client: dict[str, Any], status: str, now: str) -> dict[str, Any]:
        rec = {k: client.get(k) for k in _KEEP}
        rec.update(status=status, first_seen=now, last_seen=now if client else None)
        return rec

    def _announce(self, mac: str) -> None:
        dev = self.devices[mac]
        name = dev.get("name") or "Unknown device"
        self.hass.bus.async_fire(
            EVENT_NEW_DEVICE,
            {"mac": mac, "name": name, "ip": dev.get("ip"), "client_type": dev.get("client_type")},
        )
        persistent_notification.async_create(
            self.hass,
            f"**{name}** (`{mac}`, {dev.get('ip') or 'no IP'}) joined the network and has been "
            f"blocked until you approve it.\n\n[Review in Deco Guard](/{PANEL_URL_PATH})",
            title="New device blocked",
            notification_id=f"{DOMAIN}_{mac}",
        )

    # ----- actions (services, switches, panel) -----

    def _require(self, mac: str) -> str:
        try:
            return normalize_mac(mac)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def _router(self, fn, macs: list[str]) -> None:
        try:
            await self._call(fn, macs)
        except DecoError as err:
            raise HomeAssistantError(f"Deco request failed: {err}") from err

    def _ensure(self, mac: str) -> dict[str, Any]:
        now = dt_util.utcnow().isoformat()
        client = self.data.online.get(mac, {}) if self.data else {}
        return self.devices.setdefault(mac, self._new_record(mac, client, STATUS_APPROVED, now))

    async def async_block(self, mac: str) -> None:
        """Block internet/network access (parental block) for an approved device."""
        mac = self._require(mac)
        if self.data and mac in self.data.nodes:
            raise HomeAssistantError("Refusing to block a Deco node")
        await self._router(self.client.block, [mac])
        self._ensure(mac)
        self._after_change(mac, blocked=True)

    async def async_unblock(self, mac: str) -> None:
        """Unblock a device. Unblocking a pending/denied device approves it."""
        mac = self._require(mac)
        await self._router(self.client.unblock, [mac])
        dev = self._ensure(mac)
        if dev.get("status") != STATUS_APPROVED:
            await self._mark(mac, STATUS_APPROVED)
        self._after_change(mac, blocked=False)

    async def async_approve(self, mac: str) -> None:
        mac = self._require(mac)
        if mac not in self.devices:
            raise HomeAssistantError(f"Unknown device {mac}")
        await self._router(self.client.unblock, [mac])
        await self._mark(mac, STATUS_APPROVED)
        self._after_change(mac, blocked=False)

    async def async_deny(self, mac: str) -> None:
        mac = self._require(mac)
        if self.data and mac in self.data.nodes:
            raise HomeAssistantError("Refusing to block a Deco node")
        await self._router(self.client.block, [mac])
        self._ensure(mac)
        await self._mark(mac, STATUS_DENIED)
        self._after_change(mac, blocked=True)

    async def async_forget(self, mac: str) -> None:
        """Unblock and forget a device; if it reconnects it is treated as new."""
        mac = self._require(mac)
        if self.data and mac in self.data.blocked:
            await self._router(self.client.unblock, [mac])
        self.devices.pop(mac, None)
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{mac}")
        self._save()
        if self.data:
            self.data.blocked.discard(mac)
        self.async_update_listeners()

    async def async_rename(self, mac: str, name: str) -> None:
        mac = self._require(mac)
        self._ensure(mac)["label"] = name.strip() or None
        self._save()
        self.async_update_listeners()

    async def _mark(self, mac: str, status: str) -> None:
        dev = self.devices[mac]
        previous = dev.get("status")
        dev["status"] = status
        dev["reviewed"] = dt_util.utcnow().isoformat()
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{mac}")
        self._save()
        if status == STATUS_APPROVED and previous != STATUS_APPROVED:
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_APPROVED.format(self.config_entry.entry_id), [mac]
            )

    def _after_change(self, mac: str, *, blocked: bool) -> None:
        if self.data:
            if blocked:
                self.data.blocked.add(mac)
            else:
                self.data.blocked.discard(mac)
        self._save()
        self.async_update_listeners()

    # ----- views -----

    def display_name(self, mac: str) -> str:
        dev = self.devices.get(mac, {})
        return dev.get("label") or dev.get("name") or mac

    def device_view(self, mac: str) -> dict[str, Any]:
        dev = self.devices.get(mac, {})
        online = self.data.online.get(mac) if self.data else None
        return {
            "mac": mac,
            "name": self.display_name(mac),
            "router_name": dev.get("name"),
            "status": dev.get("status", STATUS_APPROVED),
            "blocked": bool(self.data and mac in self.data.blocked),
            "online": online is not None,
            "ip": (online or {}).get("ip") or dev.get("ip"),
            "client_type": dev.get("client_type"),
            "connection": (online or {}).get("connection_type") or dev.get("connection_type"),
            "network": dev.get("interface"),
            "first_seen": dev.get("first_seen"),
            "last_seen": dev.get("last_seen"),
        }
