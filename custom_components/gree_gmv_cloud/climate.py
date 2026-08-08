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
    FAN_MODES,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    TEMPERATURE_STEP,
)
from .coordinator import FanTargetUnknownError, GreeCoordinator
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
HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
    HVACMode.AUTO,
]


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
    _attr_hvac_modes = HVAC_MODES
    _attr_fan_modes = FAN_MODES
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
        return super().available and self._unit_key in self.coordinator.data and self.unit.online

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self.unit.power:
            return HVACMode.OFF
        return MODE_TO_HVAC.get(self.unit.mode)

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
        # Reported 3..7 is an execution level and cannot reveal an auto target.
        return self.coordinator.fan_target(self._unit_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reported_fan_speed": self.unit.reported_wind_speed,
            "cloud_mode_code": self.unit.mode,
            "cloud_power": self.unit.power,
            "cloud_online": self.unit.online,
            "cloud_error_present": self.unit.error_present,
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
            raise HomeAssistantError("Turn the indoor unit on before changing temperature")
        if self.unit.mode == MODE_AUTO:
            raise HomeAssistantError("The mini-program disables temperature control in auto mode")
        value = float(temperature)
        if value < MIN_TEMPERATURE or value > MAX_TEMPERATURE or not value.is_integer():
            raise HomeAssistantError("Temperature must be a whole degree from 16 to 30")
        await self._async_control({"setTemp": int(value)})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in FAN_MODES:
            raise HomeAssistantError(f"Unsupported fan mode: {fan_mode}")
        if not self.unit.power:
            raise HomeAssistantError("Turn the indoor unit on before selecting a fan mode")
        if self.unit.mode in (MODE_AUTO, MODE_DRY):
            raise HomeAssistantError(
                "The mini-program disables fan adjustment in auto and dry modes"
            )
        await self._async_control({}, explicit_fan_mode=fan_mode)
