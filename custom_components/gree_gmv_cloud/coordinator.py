"""Update coordinator for Gree GMV Cloud."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GreeApiError, GreeAuthError, GreeCloudApi, GreeControlError
from .const import (
    CONF_FAN_TARGETS,
    DEFAULT_SCAN_INTERVAL,
    FAN_MODE_TO_CONTROL,
    WRITE_READBACK_DELAY,
)
from .fan_policy import effective_fan_target
from .models import GreeUnit
from .system_policy import DirectionState, allowed_mode_codes, system_direction

_LOGGER = logging.getLogger(__name__)


class FanTargetUnknownError(Exception):
    """A full-state write cannot safely preserve the desired fan target."""


class GreeCoordinator(DataUpdateCoordinator[dict[str, GreeUnit]]):
    """Coordinate one cloud poll for all indoor units and serialize controls."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: GreeCloudApi,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Gree GMV Cloud",
            update_interval=DEFAULT_SCAN_INTERVAL,
            always_update=False,
        )
        self.entry = entry
        self.api = api

    async def _async_update_data(self) -> dict[str, GreeUnit]:
        try:
            return await self.api.async_get_units()
        except GreeAuthError as err:
            raise ConfigEntryAuthFailed(
                "Gree GMV cloud credential was rejected"
            ) from err
        except GreeApiError as err:
            raise UpdateFailed(str(err)) from err

    def fan_target(self, unit_key: str) -> str:
        """Return the explicit target or automatic default; never infer status."""
        targets = self.entry.options.get(CONF_FAN_TARGETS, {})
        target = targets.get(unit_key) if isinstance(targets, dict) else None
        return effective_fan_target(target)

    def system_direction(self) -> DirectionState:
        """Return the system direction visible in the latest cloud snapshot."""
        return system_direction(self.data)

    def allowed_mode_codes(self, unit_key: str) -> tuple[int, ...]:
        """Return modes selectable for this master/slave unit right now."""
        return allowed_mode_codes(self.data[unit_key], self.data)

    def save_fan_target(self, unit_key: str, fan_mode: str) -> None:
        """Persist an explicit HA fan target without inferring cloud state."""
        targets = dict(self.entry.options.get(CONF_FAN_TARGETS, {}))
        targets[unit_key] = fan_mode
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_FAN_TARGETS: targets},
        )

    async def async_control(
        self,
        unit_key: str,
        changes: dict[str, int | float],
        *,
        explicit_fan_mode: str | None = None,
    ) -> None:
        """Perform one guarded write and wait for a fresh cloud readback."""
        fan_mode = explicit_fan_mode or self.fan_target(unit_key)
        if fan_mode not in FAN_MODE_TO_CONTROL:
            raise FanTargetUnknownError(
                "Fan target is unknown. While this room is on in cooling, heating, "
                "or fan-only mode, choose a fan mode in Home Assistant once before "
                "using power, mode, or temperature controls."
            )
        try:
            await self.api.async_control_unit(
                unit_key,
                wind_target_code=FAN_MODE_TO_CONTROL[fan_mode],
                changes=changes,
            )
        except GreeControlError:
            # Never retry a write. A prompt poll is safe and helps resolve both
            # definitive direction conflicts and ambiguous transport failures.
            await self.async_request_refresh()
            raise
        if explicit_fan_mode is not None:
            self.save_fan_target(unit_key, explicit_fan_mode)
        await asyncio.sleep(WRITE_READBACK_DELAY)
        await self.async_request_refresh()
