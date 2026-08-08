"""Master/slave operating-direction policy for a Gree GMV system."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .const import MODE_AUTO, MODE_COOL, MODE_DRY, MODE_FAN, MODE_HEAT
from .models import GreeUnit

DIRECTION_COOLING = "cooling"
DIRECTION_HEATING = "heating"
DIRECTION_UNKNOWN = "unknown"

SOURCE_MASTER_MODE = "master_mode"
SOURCE_ACTIVE_SLAVE = "active_slave"
SOURCE_AMBIGUOUS = "ambiguous"
SOURCE_UNAVAILABLE = "unavailable"


class SystemModeConflictError(ValueError):
    """A requested slave operation conflicts with the GMV system direction."""


@dataclass(frozen=True, slots=True)
class DirectionState:
    """The direction visible or safely inferable from one cloud snapshot."""

    direction: str
    source: str


def master_unit(units: Mapping[str, GreeUnit]) -> GreeUnit | None:
    """Return the single reported master, or None for ambiguous topology."""
    masters = [unit for unit in units.values() if unit.is_master]
    return masters[0] if len(masters) == 1 else None


def _direction_for_mode(mode: int) -> str | None:
    if mode in (MODE_COOL, MODE_DRY):
        return DIRECTION_COOLING
    if mode == MODE_HEAT:
        return DIRECTION_HEATING
    return None


def system_direction(units: Mapping[str, GreeUnit]) -> DirectionState:
    """Resolve the active heat/cool direction without inventing cloud state.

    An explicit master cooling/heating mode is authoritative even while the
    master unit is off. In master auto mode, the captured API has no second
    field for the controller's cooling/heating lamp, so a powered slave in a
    directional mode is the only currently verified cloud-side evidence.
    """
    master = master_unit(units)
    if master is None:
        return DirectionState(DIRECTION_UNKNOWN, SOURCE_UNAVAILABLE)

    explicit = _direction_for_mode(master.mode)
    if explicit is not None:
        return DirectionState(explicit, SOURCE_MASTER_MODE)

    active_directions = {
        direction
        for unit in units.values()
        if not unit.is_master and unit.power
        if (direction := _direction_for_mode(unit.mode)) is not None
    }
    if len(active_directions) == 1:
        return DirectionState(active_directions.pop(), SOURCE_ACTIVE_SLAVE)
    if len(active_directions) > 1:
        return DirectionState(DIRECTION_UNKNOWN, SOURCE_AMBIGUOUS)
    return DirectionState(DIRECTION_UNKNOWN, SOURCE_UNAVAILABLE)


def allowed_mode_codes(
    unit: GreeUnit, units: Mapping[str, GreeUnit]
) -> tuple[int, ...]:
    """Return selectable mode codes for an entity in the current snapshot."""
    master = master_unit(units)
    if master is not None and unit.unique_id == master.unique_id:
        return (MODE_COOL, MODE_HEAT, MODE_DRY, MODE_FAN, MODE_AUTO)

    direction = system_direction(units).direction
    if direction == DIRECTION_COOLING:
        allowed = [MODE_COOL, MODE_DRY, MODE_FAN]
    elif direction == DIRECTION_HEATING:
        allowed = [MODE_HEAT, MODE_FAN]
    elif master is not None and master.mode == MODE_AUTO:
        # The direction exists physically but is absent from this snapshot.
        # Offer both directional choices and let the controller reject the one
        # that conflicts; validate_control_change deliberately permits this.
        allowed = [MODE_COOL, MODE_HEAT, MODE_DRY, MODE_FAN]
    else:
        allowed = [MODE_FAN]

    # Keep a currently running cloud state representable in HA during a
    # direction transition, without making it a generally valid new choice.
    if unit.power and unit.mode != MODE_AUTO and unit.mode not in allowed:
        allowed.insert(0, unit.mode)
    return tuple(allowed)


def validate_control_change(
    units: Mapping[str, GreeUnit],
    unit_key: str,
    changes: Mapping[str, int | float],
) -> None:
    """Reject only new slave operations that contradict the visible direction."""
    try:
        unit = units[unit_key]
    except KeyError:
        raise SystemModeConflictError(
            "The selected indoor unit is no longer present"
        ) from None

    desired_power = bool(changes.get("on_OFF_Status", int(unit.power)))
    desired_mode = int(changes.get("mode", unit.mode))

    if desired_mode == MODE_AUTO and not unit.is_master:
        raise SystemModeConflictError("Auto mode is available only on the master unit")
    if not desired_power:
        return

    # Temperature/fan writes and idempotent power calls do not introduce a new
    # system direction. Validate only a new power-on or an actual mode change.
    introduces_operation = (not unit.power) or desired_mode != unit.mode
    if not introduces_operation:
        return

    master = master_unit(units)
    if master is not None and unit.unique_id == master.unique_id:
        return
    if desired_mode == MODE_FAN:
        return

    direction = system_direction(units)
    required = _direction_for_mode(desired_mode)
    if required is None:
        raise SystemModeConflictError("The requested slave mode is not supported")
    if direction.direction == DIRECTION_UNKNOWN:
        # Auto always has a real cooling/heating direction on the controller,
        # but the captured cloud snapshot does not expose its second lamp. Let
        # the GMV controller arbitrate this one case and surface a definite
        # application rejection without retrying.
        if master is not None and master.mode == MODE_AUTO:
            return
        raise SystemModeConflictError(
            "The active cooling/heating direction is not visible in the current "
            "cloud state. Set the master to an explicit cooling or heating mode, "
            "or first establish the direction with the wired controller."
        )
    if required != direction.direction:
        raise SystemModeConflictError(
            f"The slave requires {required}, but the GMV system is currently "
            f"in the {direction.direction} direction"
        )
