"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.deco_guard.const import DOMAIN

INPUT = {CONF_HOST: "http://192.168.68.1", CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw"}


async def test_user_flow_creates_entry(hass, deco):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    with (
        patch("custom_components.deco_guard.config_flow.DecoClient", return_value=deco),
        patch("custom_components.deco_guard.DecoClient", return_value=deco),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Deco BE85"
    assert result["result"].unique_id == "74-FE-CE-23-F7-8C"


async def test_user_flow_bad_password(hass, deco):
    deco.auth_fail = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.deco_guard.config_flow.DecoClient", return_value=deco):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_bad_email(hass, deco):
    """The luci login takes the password alone; the TMP login also needs the right email."""
    deco.tmp_auth_fail = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.deco_guard.config_flow.DecoClient", return_value=deco):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_tmp_auth"}


async def test_reauth_adds_email_to_old_entry(hass, deco):
    """Entries from before 0.2 have no email: setup asks for it, then loads."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="74-FE-CE-23-F7-8C", data={CONF_HOST: "h", CONF_PASSWORD: "pw"}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.deco_guard.config_flow.DecoClient", return_value=deco),
        patch("custom_components.deco_guard.DecoClient", return_value=deco),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        [flow] = [
            f
            for f in hass.config_entries.flow.async_progress()
            if f["context"]["source"] == "reauth"
        ]
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_EMAIL] == "me@example.com"
    assert entry.state is ConfigEntryState.LOADED
