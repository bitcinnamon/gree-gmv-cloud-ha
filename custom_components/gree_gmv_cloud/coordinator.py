"""Update coordinator for Gree GMV Cloud."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GreeApiError, GreeAuthError, GreeCloudApi, GreeControlError
from .const import (
    CONF_FAN_TARGETS,
    DEFAULT_SCAN_INTERVAL,
    FAN_AUTO,
    FAN_MODE_TO_CONTROL,
    FAN_TARGET_RECONCILE_GRACE,
    WRITE_READBACK_DELAY,
)
from .fan_policy import (
    FAN_ADJUSTABLE_MODES,
    FAN_CONTROL_TO_MODE,
    effective_fan_target,
    reconcile_fixed_fan_target,
    reported_fixed_fan_target,
)
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
        self._fan_target_hold_until: dict[str, float] = {}
        self._fan_control_locks: dict[str, asyncio.Lock] = {}
        self._fan_control_generation: dict[str, int] = {}
        self._fan_control_events: dict[str, asyncio.Event] = {}

    async def _async_update_data(self) -> dict[str, GreeUnit]:
        try:
            units = await self.api.async_get_units()
            self._reconcile_fan_targets(units)
            return units
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

    def _reconcile_fan_targets(self, units: dict[str, GreeUnit]) -> None:
        """Reflect wired-controller fixed-level changes in HA target state."""
        configured = self.entry.options.get(CONF_FAN_TARGETS, {})
        targets = dict(configured) if isinstance(configured, dict) else {}
        now = time.monotonic()
        changed = False
        for unit_key, unit in units.items():
            if now < self._fan_target_hold_until.get(unit_key, 0):
                continue
            current = effective_fan_target(targets.get(unit_key))
            reconciled = reconcile_fixed_fan_target(
                current,
                reported_wind_speed=unit.reported_wind_speed,
                power=unit.power,
                mode=unit.mode,
            )
            if reconciled != current:
                targets[unit_key] = reconciled
                changed = True
        if changed:
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
        fan_control_generation: int | None = None,
        superseded_event: asyncio.Event | None = None,
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
            requested_fan_code = FAN_MODE_TO_CONTROL[fan_mode]
            reconcile_reported_fan = (
                explicit_fan_mode is None
                and time.monotonic() >= self._fan_target_hold_until.get(unit_key, 0)
            )
            applied_fan_code = await self.api.async_control_unit(
                unit_key,
                wind_target_code=requested_fan_code,
                changes=changes,
                reconcile_reported_fan=reconcile_reported_fan,
            )
        except GreeControlError:
            # Never retry a write. A prompt poll is safe and helps resolve both
            # definitive direction conflicts and ambiguous transport failures.
            await self.async_request_refresh()
            raise
        if explicit_fan_mode is not None:
            self.save_fan_target(unit_key, explicit_fan_mode)
            self._fan_target_hold_until[unit_key] = (
                time.monotonic() + FAN_TARGET_RECONCILE_GRACE
            )
        elif applied_fan_code != requested_fan_code:
            self.save_fan_target(unit_key, FAN_CONTROL_TO_MODE[applied_fan_code])
        if await self._async_fan_command_was_superseded(superseded_event):
            return
        await self.async_request_refresh()
        if (
            fan_control_generation is not None
            and self._fan_control_generation.get(unit_key) != fan_control_generation
        ):
            return
        if explicit_fan_mode is None:
            return
        if explicit_fan_mode == FAN_AUTO:
            self._fan_target_hold_until.pop(unit_key, None)
            return

        expected_reported_code = FAN_MODE_TO_CONTROL[explicit_fan_mode] + 1
        unit = self.data.get(unit_key)
        if unit is not None and unit.reported_wind_speed == expected_reported_code:
            self._fan_target_hold_until.pop(unit_key, None)
            return

        # A cloud success response does not prove that the DTU has applied the
        # command. Poll once more without resending, then roll HA back to the
        # physical level if the requested fixed target is still unconfirmed.
        if await self._async_fan_command_was_superseded(superseded_event):
            return
        await self.async_request_refresh()
        if (
            fan_control_generation is not None
            and self._fan_control_generation.get(unit_key) != fan_control_generation
        ):
            return
        unit = self.data.get(unit_key)
        if unit is not None and unit.reported_wind_speed == expected_reported_code:
            self._fan_target_hold_until.pop(unit_key, None)
            return
        self._fan_target_hold_until.pop(unit_key, None)
        if unit is not None:
            reported_target = reported_fixed_fan_target(unit.reported_wind_speed)
            if (
                reported_target is not None
                and unit.power
                and unit.mode in FAN_ADJUSTABLE_MODES
            ):
                self.save_fan_target(unit_key, reported_target)
        raise GreeControlError(
            "Gree cloud accepted the fan command, but the indoor unit did not "
            "confirm the requested fixed level"
        )

    async def async_control_fan(
        self,
        unit_key: str,
        fan_mode: str,
    ) -> None:
        """Serialize fan writes per room and discard superseded queued writes."""
        if previous_event := self._fan_control_events.get(unit_key):
            previous_event.set()
        superseded_event = asyncio.Event()
        self._fan_control_events[unit_key] = superseded_event
        generation = self._fan_control_generation.get(unit_key, 0) + 1
        self._fan_control_generation[unit_key] = generation
        lock = self._fan_control_locks.setdefault(unit_key, asyncio.Lock())
        async with lock:
            if self._fan_control_generation.get(unit_key) != generation:
                return
            try:
                await self.async_control(
                    unit_key,
                    {},
                    explicit_fan_mode=fan_mode,
                    fan_control_generation=generation,
                    superseded_event=superseded_event,
                )
            finally:
                if self._fan_control_events.get(unit_key) is superseded_event:
                    self._fan_control_events.pop(unit_key, None)

    @staticmethod
    async def _async_fan_command_was_superseded(
        superseded_event: asyncio.Event | None,
    ) -> bool:
        """Wait for readback time or return early when a newer fan write arrives."""
        if superseded_event is None:
            await asyncio.sleep(WRITE_READBACK_DELAY)
            return False
        try:
            await asyncio.wait_for(
                superseded_event.wait(), timeout=WRITE_READBACK_DELAY
            )
        except TimeoutError:
            return False
        return True
