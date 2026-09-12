"""Somfy RTS integration for Home Assistant.

Controls Somfy RTS motorized blinds/shutters via the ``radio_frequency`` platform.
"""

from __future__ import annotations

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_COUNTER, CONF_KEY, DOMAIN, STORAGE_VERSION
from .entity import SomfyRTSConfigEntry, SomfyRTSData

PLATFORMS = ["cover"]

_DEFAULT_KEY: Final = 0xA7


async def async_setup_entry(hass: HomeAssistant, entry: SomfyRTSConfigEntry) -> bool:
    """Set up Somfy RTS from a config entry."""
    store = Store[dict[str, int]](hass, STORAGE_VERSION, f"{DOMAIN}/{entry.entry_id}")
    stored = await store.async_load()
    rolling_code = (
        stored["rolling_code"]
        if isinstance(stored, dict) and "rolling_code" in stored
        else entry.data.get(CONF_COUNTER, 0)
    )
    key = (
        stored["key"]
        if isinstance(stored, dict) and "key" in stored
        else entry.data.get(CONF_KEY, _DEFAULT_KEY)
    )
    entry.runtime_data = SomfyRTSData(store=store, rolling_code=rolling_code, key=key)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SomfyRTSConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)