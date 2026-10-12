"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.deco_guard.const import DOMAIN

INPUT = {CONF_HOST: "http://192.168.68.1", CONF_PASSWORD: "pw"}


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
