"""Redacted diagnostics for Gree GMV Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import SENSITIVE_CONFIG_KEYS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without credentials, room names, or device identifiers."""
    coordinator = entry.runtime_data
    return {
        "entry_data": async_redact_data(entry.data, SENSITIVE_CONFIG_KEYS),
        "options": entry.options,
        "last_update_success": coordinator.last_update_success,
        "unit_count": len(coordinator.data),
        "units": [unit.safe_diagnostics() for unit in coordinator.data.values()],
    }
