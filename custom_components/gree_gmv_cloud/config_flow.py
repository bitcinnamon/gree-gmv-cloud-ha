"""UI configuration flow for Gree GMV Cloud."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GreeApiError, GreeAuthError, GreeCloudApi
from .const import CONF_OPEN_ID, CONF_TOKEN, CONF_UID, DOMAIN


def _schema(defaults: dict[str, Any] | None = None, *, token_only: bool = False):
    defaults = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_TOKEN,
            default=defaults.get(CONF_TOKEN, ""),
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
    if not token_only:
        fields.update(
            {
                vol.Required(
                    CONF_OPEN_ID,
                    default=defaults.get(CONF_OPEN_ID, ""),
                ): str,
                vol.Required(
                    CONF_UID,
                    default=defaults.get(CONF_UID, ""),
                ): str,
            }
        )
    return vol.Schema(fields)


async def _validate(hass, data: dict[str, Any]) -> int:
    client = GreeCloudApi(
        async_get_clientsession(hass),
        token=data[CONF_TOKEN],
        open_id=data[CONF_OPEN_ID],
        uid=data[CONF_UID],
    )
    return len(await client.async_get_units())


class GreeGmvCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure credentials captured from the owner's mini-program session."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                unit_count = await _validate(self.hass, user_input)
            except GreeAuthError:
                errors["base"] = "invalid_auth"
            except GreeApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - HA flow must render unknown failures
                errors["base"] = "unknown"
            else:
                account_key = sha256(
                    f"{user_input[CONF_OPEN_ID]}|{user_input[CONF_UID]}".encode()
                ).hexdigest()[:24]
                await self.async_set_unique_id(account_key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Gree GMV Cloud ({unit_count} units)",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updated = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            try:
                await _validate(self.hass, updated)
            except GreeAuthError:
                errors["base"] = "invalid_auth"
            except GreeApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=updated)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema(user_input, token_only=True),
            errors=errors,
        )
