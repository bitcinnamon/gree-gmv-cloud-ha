"""Fan-target policy independent of Home Assistant runtime imports."""

from __future__ import annotations

from typing import Any

from .const import FAN_AUTO, FAN_MODE_TO_CONTROL


def effective_fan_target(configured_target: Any) -> str:
    """Return an explicit target or the installation-wide automatic default."""
    if (
        isinstance(configured_target, str)
        and configured_target in FAN_MODE_TO_CONTROL
    ):
        return configured_target
    return FAN_AUTO
