"""Shared fixtures: a fake Deco router behind the integration."""

from __future__ import annotations

import sys
from unittest.mock import patch

if sys.platform == "win32":  # pragma: no cover - local runs only; CI is Linux
    # asyncio on Windows needs a loopback socketpair, which pytest-socket would block.
    import pytest_socket

    pytest_socket.disable_socket = lambda *a, **k: None

import pytest
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deco_guard.api import DecoAuthError
from custom_components.deco_guard.const import DOMAIN
from custom_components.deco_guard.tmp import TmpAuthError

NODE = "74-FE-CE-23-F7-8C"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def trace_file(tmp_path):
    """Keep the diagnostic trace out of the shared test config dir."""
    path = tmp_path / "trace.jsonl"
    with patch("custom_components.deco_guard.trace.TRACE_FILE", str(path)):
        yield path


class FakeDeco:
    """In-memory Deco: clients stay listed while blocked, like the real BE85."""

    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.blocked: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []
        self.auth_fail = False
        self.tmp_auth_fail = False  # TP-Link ID login on the TMP channel
        self.host = "http://192.168.68.1"

    def connect(self, mac: str, name: str, ip: str = "192.168.68.99") -> None:
        self.clients[mac] = {
            "mac": mac,
            "name": name,
            "ip": ip,
            "client_type": "pc",
            "online": True,
        }

    def get_device_list(self):
        if self.auth_fail:
            raise DecoAuthError("Login failed (error_code -5002)")
        return [{"mac": NODE, "role": "master", "device_model": "be85"}]

    def get_client_list(self):
        return [
            {**c, "ip": "" if c["mac"] in self.blocked else c["ip"]} for c in self.clients.values()
        ]

    def get_blocked_macs(self):
        return set(self.blocked)

    def verify_tmp(self):
        if self.tmp_auth_fail:
            raise TmpAuthError("TMP login failed")
        return set(self.blocked)

    def block(self, macs):
        if self.tmp_auth_fail:
            raise TmpAuthError("TMP login failed")
        self.calls.append(("block", list(macs)))
        self.blocked.update(macs)
        self.last_response = {"error_code": 0, "result": {}}

    def unblock(self, macs):
        self.calls.append(("unblock", list(macs)))
        self.blocked.difference_update(macs)
        self.last_response = {"error_code": 0, "result": {}}

    def close(self):
        pass


@pytest.fixture
def deco() -> FakeDeco:
    fake = FakeDeco()
    fake.connect("AA-AA-AA-AA-AA-01", "Laptop", "192.168.68.10")
    fake.connect("AA-AA-AA-AA-AA-02", "Phone", "192.168.68.11")
    fake.blocked.add("AA-AA-AA-AA-AA-09")  # blocked before install, not connected
    return fake


@pytest.fixture
async def setup(hass, deco):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NODE,
        data={CONF_HOST: "http://192.168.68.1", CONF_EMAIL: "me@example.com", CONF_PASSWORD: "x"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.deco_guard.DecoClient", return_value=deco):
        assert await hass.config_entries.async_setup(entry.entry_id) or deco.auth_fail
        await hass.async_block_till_done()
        yield entry
