"""Polling coordinator: reads the Deco, quarantines new devices, enforces blocks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
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
from .trace import Trace

_LOGGER = logging.getLogger(__name__)

# Client fields kept for display (the block list only returns MACs, so names come from here).
_KEEP = ("name", "ip", "client_type", "wire_type", "connection_type", "interface")

# Seconds after a block/unblock at which the router is re-read for the trace.
PROBE_DELAYS = (5, 30, 120)


@dataclass
class DecoData:
    """Snapshot of one poll."""

    online: dict[str, dict[str, Any]] = field(default_factory=dict)
    listed: dict[str, dict[str, Any]] = field(default_factory=dict)  # incl. blocked clients
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
        self.trace = Trace(hass)

    # ----- store -----

    async def async_load(self) -> None:
        self.store_data = await self._store.async_load() or {
            "seeded": False,
            "auto_block": True,
            "devices": {},
        }

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.store_data, 1)

    async def async_flush(self) -> None:
        """Write pending changes now (on unload), so a quick reload can't lose reviews."""
        await self._store.async_save(self.store_data)

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
        step, start = "device_list", time.monotonic()
        try:
            nodes = await self._call(self.client.get_device_list)
            step = "client_list"
            clients = await self._call(self.client.get_client_list)
            step = "black_list"
            blocked = await self._call(self.client.get_blocked_macs)
        except DecoAuthError as err:
            self.trace.record("poll_error", step=step, error=str(err), elapsed_s=_since(start))
            # Stop polling: repeated bad logins lock the admin account on the router.
            raise ConfigEntryAuthFailed(str(err)) from err
        except DecoError as err:
            self.trace.record("poll_error", step=step, error=str(err), elapsed_s=_since(start))
            raise UpdateFailed(str(err)) from err

        listed = {normalize_mac(c["mac"]): c for c in clients if c.get("mac")}
        # Blocked clients stay in client_list (without an IP), so drop them from "online".
        data = DecoData(
            online={mac: c for mac, c in listed.items() if mac not in blocked},
            listed=listed,
            blocked=blocked,
            nodes={normalize_mac(n["mac"]): n for n in nodes if n.get("mac")},
        )
        self._trace_presence(self.data, data)
        await self._process(data)
        return data

    def _trace_presence(self, prev: DecoData | None, data: DecoData) -> None:
        """Trace when a device HA has blocked/unblocked appears, disappears or changes IP."""
        for mac, dev in self.devices.items():
            if not dev.get("traced"):
                continue
            client = data.listed.get(mac)
            old = prev.listed.get(mac) if prev else None
            if (
                prev is None
                or (old is None) != (client is None)
                or (mac in prev.blocked) != (mac in data.blocked)
                or (old or {}).get("ip") != (client or {}).get("ip")
            ):
                self.trace.record(
                    "presence",
                    mac=mac,
                    listed=client is not None,
                    blocked=mac in data.blocked,
                    client=client,
                    clients=len(data.listed),
                )

    async def _process(self, data: DecoData) -> None:
        """Remember devices, quarantine new ones and re-apply quarantine blocks."""
        now = dt_util.utcnow().isoformat()
        changed = False

        for mac, client in data.listed.items():
            dev = self.devices.get(mac)
            info = {k: v for k in _KEEP if (v := client.get(k)) not in (None, "")}
            if dev is not None:
                if any(dev.get(k) != v for k, v in info.items()):
                    dev.update(info)
                    changed = True
                if mac in data.online:
                    dev["last_seen"] = now

        if not self.store_data.get("seeded"):
            # First run: everything already on the network (or already blocked) is trusted,
            # otherwise enabling the integration would block the whole house.
            for mac, client in data.online.items():
                self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
            for mac in data.blocked:
                rec = self.devices.setdefault(
                    mac, self._new_record(mac, data.listed.get(mac, {}), STATUS_APPROVED, now)
                )
                rec["blocked"] = True
            self.store_data["seeded"] = True
            await self.async_flush()
            _LOGGER.info("Seeded %d known devices as approved", len(self.devices))
            return

        new_approved: list[str] = []
        new_pending: list[str] = []
        # A block added outside HA (e.g. in the Deco app) is adopted, so it is enforced too.
        for mac in data.blocked:
            if mac in data.nodes:
                continue
            dev = self.devices.get(mac)
            if dev is None:
                client = data.listed.get(mac, {})
                dev = self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
                new_approved.append(mac)
            if not dev.get("blocked"):
                dev["blocked"] = True
                changed = True
                self.trace.record("adopted_block", mac=mac)
        for mac, client in data.online.items():
            if mac in self.devices or mac in data.nodes:
                continue
            if self.auto_block:
                self.devices[mac] = self._new_record(mac, client, STATUS_PENDING, now)
                new_pending.append(mac)
            else:
                self.devices[mac] = self._new_record(mac, client, STATUS_APPROVED, now)
                new_approved.append(mac)
            changed = True

        # Pending, denied and parental-blocked devices must stay blocked: re-apply the block
        # if it disappears from the router (unblocked in the Deco app, router reset, ...).
        enforce = [
            mac
            for mac, dev in self.devices.items()
            if (dev.get("blocked") or dev.get("status") in (STATUS_PENDING, STATUS_DENIED))
            and mac not in data.blocked
            and mac not in data.nodes
        ]
        if enforce:
            try:
                await self._call(self.client.block, enforce)
                data.blocked.update(enforce)
                for mac in enforce:
                    data.online.pop(mac, None)
                _LOGGER.info("Re-applied blocks: %s", ", ".join(enforce))
                self.trace.record("reapplied_block", macs=enforce)
            except DecoAuthError as err:
                self.trace.record("reapplied_block", macs=enforce, error=str(err))
                raise ConfigEntryAuthFailed(str(err)) from err
            except DecoError as err:
                self.trace.record("reapplied_block", macs=enforce, error=str(err))
                _LOGGER.error("Failed to re-apply blocks %s: %s", enforce, err)

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
        rec.update(
            status=status,
            blocked=status != STATUS_APPROVED,
            first_seen=now,
            last_seen=now if client else None,
        )
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

    def _failed(self, err: DecoError) -> HomeAssistantError:
        """Error for a failed action; a rejected login also asks the user to re-authenticate."""
        if isinstance(err, DecoAuthError):
            self.config_entry.async_start_reauth(self.hass)
        return HomeAssistantError(f"Deco request failed: {err}")

    async def _router(self, fn, macs: list[str]) -> None:
        try:
            await self._call(fn, macs)
        except DecoError as err:
            raise self._failed(err) from err

    async def _traced(self, op: str, fn, mac: str) -> None:
        """Run a block/unblock for one device, tracing the router state before and after."""
        listed = self.data.listed.get(mac) if self.data else None
        before = {
            "listed": listed is not None,
            "blocked": bool(self.data and mac in self.data.blocked),
            "client": listed,
        }
        self._ensure(mac)["traced"] = True
        start = time.monotonic()
        try:
            result = await self._call(self._send_traced, fn, [mac])
        except DecoError as err:
            self.trace.record(
                op,
                mac=mac,
                before=before,
                error=str(err),
                error_code=err.error_code,
                elapsed_s=_since(start),
            )
            raise self._failed(err) from err
        self.trace.record(op, mac=mac, before=before, elapsed_s=_since(start), **result)
        self._schedule_probes(op, mac)

    def _send_traced(self, fn, macs: list[str]) -> dict[str, Any]:
        """Executor side of _traced: the call plus the router's raw answer."""
        relogins = getattr(self.client, "relogins", 0)
        self.client.last_response = None
        start = time.monotonic()
        fn(macs)
        return {
            "request_s": _since(start),
            "relogins": getattr(self.client, "relogins", 0) - relogins,
            "response": self.client.last_response,
        }

    def _schedule_probes(self, op: str, mac: str) -> None:
        for delay in PROBE_DELAYS:

            @callback
            def _fire(_now, delay: int = delay) -> None:
                self.config_entry.async_create_background_task(
                    self.hass, self._async_probe(op, mac, delay), f"{DOMAIN} probe {mac}"
                )

            self.config_entry.async_on_unload(async_call_later(self.hass, delay, _fire))

    async def _async_probe(self, op: str, mac: str, delay: int) -> None:
        """Re-read the client and block lists and trace what the router says about mac."""
        try:
            clients = await self._call(self.client.get_client_list)
            blocked = await self._call(self.client.get_blocked_macs)
        except DecoError as err:
            self.trace.record("probe", op=op, mac=mac, after_s=delay, error=str(err))
            return
        client = next((c for c in clients if _same_mac(c.get("mac"), mac)), None)
        self.trace.record(
            "probe",
            op=op,
            mac=mac,
            after_s=delay,
            listed=client is not None,
            blocked=mac in blocked,
            client=client,
            clients=len(clients),
        )

    async def async_raw(
        self, path: str, operation: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Send any request to the router and return its raw answer (API exploration)."""
        try:
            response = await self._call(self.client.raw, path, operation, params)
        except DecoError as err:
            raise HomeAssistantError(f"Deco request failed: {err}") from err
        self.trace.record("raw", path=path, operation=operation, params=params, response=response)
        return response

    def _ensure(self, mac: str) -> dict[str, Any]:
        now = dt_util.utcnow().isoformat()
        client = self.data.listed.get(mac, {}) if self.data else {}
        return self.devices.setdefault(mac, self._new_record(mac, client, STATUS_APPROVED, now))

    async def async_block(self, mac: str) -> None:
        """Block internet/network access (parental block) for an approved device."""
        mac = self._require(mac)
        if self.data and mac in self.data.nodes:
            raise HomeAssistantError("Refusing to block a Deco node")
        await self._traced("block", self.client.block, mac)
        self._ensure(mac)["blocked"] = True
        self._after_change(mac, blocked=True)

    async def async_unblock(self, mac: str) -> None:
        """Unblock a device. Unblocking a pending/denied device approves it."""
        mac = self._require(mac)
        await self._traced("unblock", self.client.unblock, mac)
        dev = self._ensure(mac)
        dev["blocked"] = False
        if dev.get("status") != STATUS_APPROVED:
            await self._mark(mac, STATUS_APPROVED)
        self._after_change(mac, blocked=False)

    async def async_approve(self, mac: str) -> None:
        mac = self._require(mac)
        if mac not in self.devices:
            raise HomeAssistantError(f"Unknown device {mac}")
        await self._router(self.client.unblock, [mac])
        self.devices[mac]["blocked"] = False
        await self._mark(mac, STATUS_APPROVED)
        self._after_change(mac, blocked=False)

    async def async_deny(self, mac: str) -> None:
        mac = self._require(mac)
        if self.data and mac in self.data.nodes:
            raise HomeAssistantError("Refusing to block a Deco node")
        await self._router(self.client.block, [mac])
        self._ensure(mac)["blocked"] = True
        await self._mark(mac, STATUS_DENIED)
        self._after_change(mac, blocked=True)

    async def async_forget(self, mac: str) -> None:
        """Unblock and forget a device; if it reconnects it is treated as new."""
        mac = self._require(mac)
        if (self.data and mac in self.data.blocked) or self.devices.get(mac, {}).get("blocked"):
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


def _since(start: float) -> float:
    return round(time.monotonic() - start, 3)


def _same_mac(value: Any, mac: str) -> bool:
    try:
        return isinstance(value, str) and normalize_mac(value) == mac
    except ValueError:
        return False
