"""Behaviour tests for quarantine, parental blocks and enforcement."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.deco_guard.const import DOMAIN, EVENT_NEW_DEVICE

LAPTOP = "AA-AA-AA-AA-AA-01"
NEW = "BB-BB-BB-BB-BB-01"
LAPTOP_SWITCH = "switch.laptop_network_access"


async def poll(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_first_run_trusts_existing_devices(hass, setup, deco):
    coordinator = setup.runtime_data
    assert setup.state is ConfigEntryState.LOADED
    assert deco.calls == []
    assert {d["status"] for d in coordinator.devices.values()} == {"approved"}
    assert coordinator.devices["AA-AA-AA-AA-AA-09"]["blocked"] is True
    assert hass.states.get(LAPTOP_SWITCH).state == "on"
    pending = [
        s for s in hass.states.async_all("sensor") if s.entity_id.endswith("pending_devices")
    ]
    assert pending[0].state == "0"


async def test_new_device_is_quarantined(hass, setup, deco):
    events = []
    hass.bus.async_listen(EVENT_NEW_DEVICE, events.append)
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)

    assert ("block", [NEW]) in deco.calls
    assert setup.runtime_data.devices[NEW]["status"] == "pending"
    assert events[0].data["mac"] == NEW
    pending = [
        s for s in hass.states.async_all("sensor") if s.entity_id.endswith("pending_devices")
    ]
    assert pending[0].state == "1"
    # Still listed by the router while blocked, but must not count as online.
    assert NEW not in setup.runtime_data.data.online

    # A second poll does not block or announce again.
    deco.calls.clear()
    await poll(hass, setup)
    assert deco.calls == [] and len(events) == 1


async def test_approve_unblocks_and_adds_switch(hass, setup, deco):
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)
    await hass.services.async_call(
        DOMAIN, "approve_device", {"mac": NEW.lower().replace("-", ":")}, blocking=True
    )
    await hass.async_block_till_done()

    assert NEW not in deco.blocked
    assert setup.runtime_data.devices[NEW]["status"] == "approved"
    assert hass.states.get("switch.stranger_network_access").state == "on"
    await poll(hass, setup)
    assert NEW not in deco.blocked


async def test_parental_block_is_reapplied(hass, setup, deco):
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": LAPTOP_SWITCH}, blocking=True
    )
    assert LAPTOP in deco.blocked
    assert hass.states.get(LAPTOP_SWITCH).state == "off"

    deco.blocked.discard(LAPTOP)  # unblocked in the Deco app / dropped by the router
    await poll(hass, setup)
    assert LAPTOP in deco.blocked
    assert hass.states.get(LAPTOP_SWITCH).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": LAPTOP_SWITCH}, blocking=True)
    assert LAPTOP not in deco.blocked
    await poll(hass, setup)
    assert LAPTOP not in deco.blocked
    assert hass.states.get(LAPTOP_SWITCH).state == "on"


async def test_external_block_is_adopted(hass, setup, deco):
    deco.blocked.add(LAPTOP)  # blocked in the Deco app
    await poll(hass, setup)
    deco.blocked.discard(LAPTOP)
    await poll(hass, setup)
    assert LAPTOP in deco.blocked


async def test_deny_keeps_blocked(hass, setup, deco):
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)
    await hass.services.async_call(DOMAIN, "deny_device", {"mac": NEW}, blocking=True)
    assert setup.runtime_data.devices[NEW]["status"] == "denied"
    deco.blocked.discard(NEW)
    await poll(hass, setup)
    assert NEW in deco.blocked


async def test_forget_unblocks_and_requarantines(hass, setup, deco):
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)
    await hass.services.async_call(DOMAIN, "forget_device", {"mac": NEW}, blocking=True)
    assert NEW not in deco.blocked and NEW not in setup.runtime_data.devices
    await poll(hass, setup)
    assert setup.runtime_data.devices[NEW]["status"] == "pending"
    assert NEW in deco.blocked


async def test_auto_block_off_approves_new_devices(hass, setup, deco):
    switch = [
        s.entity_id
        for s in hass.states.async_all("switch")
        if s.entity_id.endswith("auto_block_new_devices")
    ][0]
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    deco.connect(NEW, "Guest")
    await poll(hass, setup)
    assert NEW not in deco.blocked
    assert setup.runtime_data.devices[NEW]["status"] == "approved"
    assert hass.states.get("switch.guest_network_access").state == "on"


async def test_deco_node_never_blocked(hass, setup, deco):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "block_device", {"mac": "74:fe:ce:23:f7:8c"}, blocking=True
        )
    assert deco.calls == []


async def test_invalid_mac_rejected(hass, setup, deco):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "block_device", {"mac": "nope"}, blocking=True)


async def test_websocket_state_and_action(hass, setup, deco, hass_ws_client):
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "deco_guard/state"})
    msg = await client.receive_json()
    assert msg["success"]
    by_mac = {d["mac"]: d for d in msg["result"]["devices"]}
    assert by_mac[NEW]["status"] == "pending" and by_mac[NEW]["blocked"]
    assert by_mac[LAPTOP]["online"] and not by_mac[LAPTOP]["blocked"]

    await client.send_json({"id": 2, "type": "deco_guard/action", "action": "approve", "mac": NEW})
    msg = await client.receive_json()
    assert msg["success"]
    assert {d["mac"]: d for d in msg["result"]["devices"]}[NEW]["status"] == "approved"


async def test_store_survives_reload(hass, setup, deco):
    deco.connect(NEW, "Stranger")
    await poll(hass, setup)
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    assert (
        hass.config_entries.async_get_entry(setup.entry_id).runtime_data.devices[NEW]["status"]
        == "pending"
    )


@pytest.mark.parametrize("fail", [True])
async def test_auth_failure_starts_reauth(hass, deco, fail):
    deco.auth_fail = fail
    from unittest.mock import patch

    from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "h", CONF_EMAIL: "me@example.com", CONF_PASSWORD: "bad"}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.deco_guard.DecoClient", return_value=deco):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_block_by_device_picker(hass, setup, deco):
    from homeassistant.helpers import entity_registry as er

    device_id = er.async_get(hass).async_get(LAPTOP_SWITCH).device_id
    await hass.services.async_call(
        DOMAIN, "block_device", {"device_id": [device_id]}, blocking=True
    )
    assert LAPTOP in deco.blocked
    await hass.services.async_call(
        DOMAIN, "unblock_device", {"device_id": device_id}, blocking=True
    )
    assert LAPTOP not in deco.blocked


async def test_service_needs_device_or_mac(hass, setup, deco):
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "block_device", {}, blocking=True)


async def test_unblock_is_traced_with_probes(hass, setup, deco, trace_file):
    import json
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": LAPTOP_SWITCH}, blocking=True
    )
    await hass.services.async_call("switch", "turn_on", {"entity_id": LAPTOP_SWITCH}, blocking=True)
    deco.clients.pop(LAPTOP)  # the router forgets the client after the unblock
    for delay in (5, 30, 120):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay + 1))
        await hass.async_block_till_done()
    await poll(hass, setup)
    await hass.async_block_till_done()

    events = [json.loads(line) for line in trace_file.read_text().splitlines()]
    unblock = next(e for e in events if e["event"] == "unblock")
    assert unblock["mac"] == LAPTOP and unblock["response"] == {"error_code": 0, "result": {}}
    assert unblock["before"]["blocked"] is True and unblock["relogins"] == 0
    probes = [e for e in events if e["event"] == "probe" and e["op"] == "unblock"]
    assert [p["after_s"] for p in probes] == [5, 30, 120]
    assert all(not p["listed"] and not p["blocked"] for p in probes)
    presence = [e for e in events if e["event"] == "presence"]
    assert presence[-1]["listed"] is False


async def test_raw_request_returns_router_answer(hass, setup, deco):
    deco.raw = lambda path, operation, params: {"error_code": 0, "result": {"path": path}}
    result = await hass.services.async_call(
        DOMAIN,
        "raw_request",
        {"path": "admin/client?form=black_list", "operation": "list"},
        blocking=True,
        return_response=True,
    )
    assert result == {
        "response": {"error_code": 0, "result": {"path": "admin/client?form=black_list"}}
    }


async def test_rejected_tmp_login_while_enforcing_starts_reauth(hass, setup, deco):
    """A TMP login failure on a re-block stops polling instead of retrying (account lockout)."""
    deco.connect(NEW, "Stranger")
    deco.tmp_auth_fail = True
    await poll(hass, setup)
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_rejected_tmp_login_on_action_starts_reauth(hass, setup, deco):
    from homeassistant.exceptions import HomeAssistantError

    deco.tmp_auth_fail = True
    with pytest.raises(HomeAssistantError):
        await setup.runtime_data.async_block(LAPTOP)
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )
