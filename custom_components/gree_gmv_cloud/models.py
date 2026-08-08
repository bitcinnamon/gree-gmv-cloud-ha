"""Data models for the private Gree GMV cloud API."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    if value in (None, "", "NULL", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class GreeUnit:
    """One indoor unit returned by getUnits."""

    unique_id: str
    room_name: str
    mac: str
    ip: int
    system_id: str
    bind_type: str
    is_master: bool
    set_temperature: float | None
    environment_temperature: float | None
    power: bool
    mode: int
    reported_wind_speed: int
    online: bool
    error_present: bool

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> GreeUnit:
        """Normalize the string-heavy cloud response."""
        mac = str(value.get("mac") or "")
        ip = _as_int(value.get("ip"), -1)
        system_id = str(value.get("systemId") or "")
        identity = f"{system_id}|{mac}|{ip}".encode()
        error = value.get("error")
        return cls(
            unique_id=sha256(identity).hexdigest()[:24],
            room_name=str(value.get("roomName") or "Indoor unit"),
            mac=mac,
            ip=ip,
            system_id=system_id,
            bind_type=str(value.get("bindType") or ""),
            is_master=_as_int(value.get("mainIDU")) == 1,
            set_temperature=_as_float(value.get("setTemp")),
            environment_temperature=_as_float(value.get("enviroTemp")),
            power=_as_int(value.get("on_OFF_Status")) == 1,
            mode=_as_int(value.get("mode")),
            reported_wind_speed=_as_int(value.get("windSpeed")),
            online=_as_int(value.get("isLink")) == 1,
            error_present=error not in (None, "", 0, "0"),
        )

    def control_identity_is_valid(self) -> bool:
        """Return whether all captured DTU control identifiers are present."""
        return bool(
            self.mac
            and self.ip >= 0
            and self.system_id
            and self.bind_type.lower() == "dtu"
        )

    def safe_diagnostics(self) -> dict[str, Any]:
        """Return state without room names or cloud/device identifiers."""
        return {
            "unit_key": self.unique_id,
            "is_master": self.is_master,
            "set_temperature": self.set_temperature,
            "environment_temperature": self.environment_temperature,
            "power": self.power,
            "mode_code": self.mode,
            "reported_wind_speed": self.reported_wind_speed,
            "online": self.online,
            "error_present": self.error_present,
            "dtu_control_fields_present": self.control_identity_is_valid(),
        }
