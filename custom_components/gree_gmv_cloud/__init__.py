"""Gree GMV Cloud custom integration."""

from __future__ import annotations

async def async_setup_entry(hass, entry) -> bool:
    """Set up Gree GMV Cloud from a UI config entry."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .api import GreeCloudApi
    from .const import CONF_OPEN_ID, CONF_TOKEN, CONF_UID, PLATFORMS
    from .coordinator import GreeCoordinator

    def persist_refreshed_token(token: str) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TOKEN: token},
        )

    api = GreeCloudApi(
        async_get_clientsession(hass),
        token=entry.data[CONF_TOKEN],
        open_id=entry.data[CONF_OPEN_ID],
        uid=entry.data[CONF_UID],
        token_callback=persist_refreshed_token,
    )
    coordinator = GreeCoordinator(hass, entry, api)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload the config entry."""
    from .const import PLATFORMS

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
