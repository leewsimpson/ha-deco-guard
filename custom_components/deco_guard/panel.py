"""Sidebar panel ("Deco Guard") for reviewing devices."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, PANEL_COMPONENT, PANEL_STATIC_URL, PANEL_URL_PATH

_FRONTEND = Path(__file__).parent / "frontend"


async def async_register_panel(hass: HomeAssistant) -> None:
    flags = hass.data.setdefault(DOMAIN, {})
    if not flags.get("static"):
        # Static paths cannot be unregistered, so only ever register once per run.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, str(_FRONTEND), False)]
        )
        flags["static"] = True
    if flags.get("panel"):
        return
    # Cache-bust on every HA start so an updated panel file is picked up.
    version = await hass.async_add_executor_job(
        lambda: int((_FRONTEND / "deco-guard-panel.js").stat().st_mtime)
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_COMPONENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{PANEL_STATIC_URL}/deco-guard-panel.js?v={version}",
        sidebar_title="Deco Guard",
        sidebar_icon="mdi:shield-lock-outline",
        require_admin=True,
        config={},
    )
    flags["panel"] = True


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    flags = hass.data.setdefault(DOMAIN, {})
    if flags.pop("panel", False):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
