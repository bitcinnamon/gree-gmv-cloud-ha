"""Climate entities for Gree GMV indoor units."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GreeApiError
from .const import (
    DOMAIN,
    HOMEKIT_FAN_MODES,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    REPORTED_FAN_LEVELS,
    TEMPERATURE_STEP,
)
from .coordinator import FanTargetUnknownError, GreeCoordinator
from .fan_policy import (
    control_target_for_homekit_fan_mode,
    homekit_fan_mode_from_state,
    should_send_fan_control,
)
from .models import GreeUnit

PARALLEL_UPDATES = 0

MODE_TO_HVAC = {
    MODE_COOL: HVACMode.COOL,
    MODE_DRY: HVACMode.DRY,
    MODE_FAN: HVACMode.FAN_ONLY,
    MODE_HEAT: HVACMode.HEAT,
    MODE_AUTO: HVACMode.AUTO,
}
HVAC_TO_MODE = {value: key for key, value in MODE_TO_HVAC.items()}


async def async_setup_entry(
    hass,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one climate entity per currently discovered indoor unit."""
    coordinator = entry.runtime_data
    async_add_entities(
        GreeGmvClimate(coordinator, unit_key) for unit_key in coordinator.data
    )


class GreeGmvClimate(CoordinatorEntity[GreeCoordinator], ClimateEntity):
    """A guarded full-state climate entity for one indoor unit."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMPERATURE
    _attr_max_temp = MAX_TEMPERATURE
    _attr_target_temperature_step = TEMPERATURE_STEP
    _attr_fan_modes = HOMEKIT_FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: GreeCoordinator, unit_key: str) -> None:
        super().__init__(coordinator, context=unit_key)
        self._unit_key = unit_key
        self._attr_unique_id = f"{unit_key}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit_key)},
            manufacturer="Gree",
            model="GMV indoor unit via DTU cloud",
            name=self.unit.room_name,
        )

    @property
    def unit(self) -> GreeUnit:
        return self.coordinator.data[self._unit_key]

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._unit_key in self.coordinator.data
            and self.unit.online
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self.unit.power:
            return HVACMode.OFF
        return MODE_TO_HVAC.get(self.unit.mode)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Expose auto only on the master and follow the active GMV direction."""
        modes = [HVACMode.OFF]
        modes.extend(
            MODE_TO_HVAC[mode]
            for mode in self.coordinator.allowed_mode_codes(self._unit_key)
            if mode in MODE_TO_HVAC
        )
        return modes

    @property
    def hvac_action(self) -> HVACAction | None:
        # getUnits exposes configured mode and power, but no compressor/thermal
        # demand bit. Do not pretend that COOL means actively cooling.
        return HVACAction.OFF if not self.unit.power else None

    @property
    def current_temperature(self) -> float | None:
        return self.unit.environment_temperature

    @property
    def target_temperature(self) -> float | None:
        return self.unit.set_temperature

    @property
    def fan_mode(self) -> str | None:
        return homekit_fan_mode_from_state(
            self.coordinator.fan_target(self._unit_key),
            reported_wind_speed=self.unit.reported_wind_speed,
            power=self.unit.power,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        direction = self.coordinator.system_direction()
        return {
            "is_master_unit": self.unit.is_master,
            "system_direction": direction.direction,
            "system_direction_source": direction.source,
            "reported_fan_level": REPORTED_FAN_LEVELS.get(
                self.unit.reported_wind_speed
            ),
            "reported_fan_status_code": self.unit.reported_wind_speed,
            "cloud_mode_code": self.unit.mode,
            "cloud_power": self.unit.power,
            "cloud_online": self.unit.online,
            "cloud_error_present": self.unit.error_present,
            "fan_target_mode": self.coordinator.fan_target(self._unit_key),
            "fan_target_known": self.fan_mode is not None,
        }

    async def _async_control(
        self,
        changes: dict[str, int | float],
        *,
        explicit_fan_mode: str | None = None,
    ) -> None:
        try:
            await self.coordinator.async_control(
                self._unit_key,
                changes,
                explicit_fan_mode=explicit_fan_mode,
            )
        except (FanTargetUnknownError, GreeApiError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self) -> None:
        await self._async_control({"on_OFF_Status": 1})

    async def async_turn_off(self) -> None:
        await self._async_control({"on_OFF_Status": 0})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        mode = HVAC_TO_MODE.get(hvac_mode)
        if mode is None:
            raise HomeAssistantError(f"Unsupported HVAC mode: {hvac_mode}")
        await self._async_control({"on_OFF_Status": 1, "mode": mode})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        if not self.unit.power:
            raise HomeAssistantError(
                "Turn the indoor unit on before changing temperature"
            )
        if self.unit.mode == MODE_AUTO:
            raise HomeAssistantError(
                "The mini-program disables temperature control in auto mode"
            )
        value = float(temperature)
        half_steps = value * 2
        if (
            value < MIN_TEMPERATURE
            or value > MAX_TEMPERATURE
            or abs(half_steps - round(half_steps)) > 1e-6
        ):
            raise HomeAssistantError(
                "Temperature must use 0.5-degree steps from 16 to 30"
            )
        normalized = round(half_steps) / 2
        control_value: int | float = (
            int(normalized) if normalized.is_integer() else normalized
        )
        await self._async_control({"setTemp": control_value})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in HOMEKIT_FAN_MODES:
            raise HomeAssistantError(f"Unsupported fan mode: {fan_mode}")
        target = control_target_for_homekit_fan_mode(fan_mode)
        if not should_send_fan_control(power=self.unit.power, mode=self.unit.mode):
            # The cloud requires a complete target state for future writes, but
            # its status response cannot distinguish auto from a fixed target.
            # When the room is off, or its current mode does not allow fan
            # adjustment, save an explicit HA selection without a cloud write.
            self.coordinator.save_fan_target(self._unit_key, target)
            self.async_write_ha_state()
            return
        try:
            await self.coordinator.async_control_fan(self._unit_key, target)
        except (FanTargetUnknownError, GreeApiError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
