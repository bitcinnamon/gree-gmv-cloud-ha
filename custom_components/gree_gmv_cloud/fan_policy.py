"""Fan-target policy independent of Home Assistant runtime imports."""

from __future__ import annotations

from typing import Any

from .const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_MEDIUM_HIGH,
    FAN_MEDIUM_LOW,
    FAN_MIDDLE,
    FAN_MODE_TO_CONTROL,
    FAN_OFF,
    MODE_COOL,
    MODE_FAN,
    MODE_HEAT,
    REPORTED_FAN_LEVELS,
)

FAN_CONTROL_TO_MODE = {
    control_code: fan_mode for fan_mode, control_code in FAN_MODE_TO_CONTROL.items()
}
FAN_ADJUSTABLE_MODES = {MODE_COOL, MODE_FAN, MODE_HEAT}

HOMEKIT_FAN_MODE_TO_TARGET = {
    # HomeKit reserves 0% for "off". This installation uses that linked-fan
    # event as an automatic-fan command; the climate power switch remains the
    # only way to turn the room off.
    FAN_OFF: FAN_AUTO,
    FAN_LOW: FAN_LOW,
    FAN_MIDDLE: FAN_MEDIUM_LOW,
    FAN_MEDIUM: FAN_MEDIUM,
    FAN_HIGH: FAN_HIGH,
}

TARGET_TO_HOMEKIT_FAN_MODE = {
    # Reporting HomeKit "auto" would leave its rotation slider at the previous
    # fixed value. Report the deliberately repurposed 0%/off step instead.
    FAN_AUTO: FAN_OFF,
    FAN_LOW: FAN_LOW,
    FAN_MEDIUM_LOW: FAN_MIDDLE,
    FAN_MEDIUM: FAN_MEDIUM,
    # Fixed level 4 has no fifth standard HomeKit climate speed. Render it at
    # the nearer 75% step while preserving the exact reported level attribute.
    FAN_MEDIUM_HIGH: FAN_MEDIUM,
    FAN_HIGH: FAN_HIGH,
}

REPORTED_LEVEL_TO_HOMEKIT_FAN_MODE = {
    1: FAN_LOW,
    2: FAN_MIDDLE,
    3: FAN_MEDIUM,
    4: FAN_MEDIUM,
    5: FAN_HIGH,
}


def effective_fan_target(configured_target: Any) -> str:
    """Return an explicit target or the installation-wide automatic default."""
    if isinstance(configured_target, str) and configured_target in FAN_MODE_TO_CONTROL:
        return configured_target
    return FAN_AUTO


def control_target_for_homekit_fan_mode(fan_mode: str) -> str:
    """Translate one exposed HomeKit climate mode to a GMV control target."""
    try:
        return HOMEKIT_FAN_MODE_TO_TARGET[fan_mode]
    except KeyError:
        raise ValueError(f"Unsupported HomeKit fan mode: {fan_mode}") from None


def homekit_fan_mode_from_state(
    configured_target: Any,
    *,
    reported_wind_speed: int,
    power: bool,
) -> str:
    """Render power-off, cached auto intent, or a reported fixed level."""
    if not power:
        return FAN_OFF
    target = effective_fan_target(configured_target)
    if target == FAN_AUTO:
        return FAN_OFF
    reported_level = REPORTED_FAN_LEVELS.get(reported_wind_speed)
    if reported_level is not None:
        return REPORTED_LEVEL_TO_HOMEKIT_FAN_MODE[reported_level]
    return TARGET_TO_HOMEKIT_FAN_MODE[target]


def should_send_fan_control(*, power: bool, mode: int) -> bool:
    """Return whether a fan target can be applied to the running unit now."""
    return power and mode in FAN_ADJUSTABLE_MODES


def reported_fixed_fan_target(reported_wind_speed: int) -> str | None:
    """Translate a status execution code to the corresponding fixed target."""
    reported_level = REPORTED_FAN_LEVELS.get(reported_wind_speed)
    if reported_level is None:
        return None
    return FAN_CONTROL_TO_MODE[reported_level + 1]


def reconcile_fixed_fan_target(
    configured_target: Any,
    *,
    reported_wind_speed: int,
    power: bool,
    mode: int,
) -> str:
    """Adopt an externally changed fixed level without guessing an auto target.

    The status API reports execution codes 3..7 for levels 1..5, while control
    code 1 means automatic and 2..6 mean fixed levels 1..5. Automatic execution
    can move through every reported level, so an automatic target must never be
    inferred from the report. A previously explicit fixed target can, however,
    be reconciled to another fixed level while the unit is in a mode where the
    wired controller permits fan adjustment.
    """
    target = effective_fan_target(configured_target)
    if target == FAN_AUTO or not power or mode not in FAN_ADJUSTABLE_MODES:
        return target
    reported_target = reported_fixed_fan_target(reported_wind_speed)
    if reported_target is None:
        return target
    return reported_target
