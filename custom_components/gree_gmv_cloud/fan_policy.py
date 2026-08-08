"""Fan-target policy independent of Home Assistant runtime imports."""

from __future__ import annotations

from typing import Any

from .const import (
    FAN_AUTO,
    FAN_MODE_TO_CONTROL,
    MODE_COOL,
    MODE_FAN,
    MODE_HEAT,
    REPORTED_FAN_LEVELS,
)

FAN_CONTROL_TO_MODE = {
    control_code: fan_mode for fan_mode, control_code in FAN_MODE_TO_CONTROL.items()
}
FAN_ADJUSTABLE_MODES = {MODE_COOL, MODE_FAN, MODE_HEAT}


def effective_fan_target(configured_target: Any) -> str:
    """Return an explicit target or the installation-wide automatic default."""
    if isinstance(configured_target, str) and configured_target in FAN_MODE_TO_CONTROL:
        return configured_target
    return FAN_AUTO


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
    reported_level = REPORTED_FAN_LEVELS.get(reported_wind_speed)
    if reported_level is None:
        return target
    return FAN_CONTROL_TO_MODE[reported_level + 1]
