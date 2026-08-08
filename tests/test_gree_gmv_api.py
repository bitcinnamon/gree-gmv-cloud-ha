"""Offline tests for the protocol layer; no Home Assistant install required."""

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from custom_components.gree_gmv_cloud.api import (
    GreeCloudApi,
    GreeConnectionError,
    GreeControlError,
    decode_jwt_timing,
)
from custom_components.gree_gmv_cloud.const import (
    FAN_FINAL_READBACK_ADDITIONAL_DELAY,
    HOMEKIT_FAN_MODES,
    WRITE_READBACK_DELAY,
)
from custom_components.gree_gmv_cloud.crypto import (
    CONTROL_FIELDS,
    encrypt_control_payload,
    normalize_control_payload,
)
from custom_components.gree_gmv_cloud.fan_policy import (
    control_target_for_homekit_fan_mode,
    effective_fan_target,
    homekit_fan_mode_from_state,
    reconcile_fixed_fan_target,
    should_send_fan_control,
)
from custom_components.gree_gmv_cloud.models import GreeUnit
from custom_components.gree_gmv_cloud.system_policy import (
    DIRECTION_COOLING,
    DIRECTION_HEATING,
    DIRECTION_UNKNOWN,
    SOURCE_ACTIVE_SLAVE,
    SOURCE_MASTER_MODE,
    SystemModeConflictError,
    allowed_mode_codes,
    system_direction,
    validate_control_change,
)

SYNTHETIC_UNIT = {
    "roomName": "Synthetic room",
    "mac": "synthetic-device",
    "ip": "1",
    "systemId": "synthetic-system",
    "bindType": "dtu",
    "mainIDU": "1",
    "setTemp": "24.5",
    "enviroTemp": "25.0",
    "on_OFF_Status": "1",
    "mode": "1",
    "windSpeed": "7",
    "isLink": 1,
    "error": None,
}

SYNTHETIC_PAYLOAD = {
    "openId": "synthetic-open-id",
    "mac": "synthetic-device",
    "ip": 1,
    "setTemp": 24.5,
    "on_OFF_Status": 1,
    "mode": 1,
    "windSpeed": 2,
    "systemId": "synthetic-system",
    "bindType": "DTU",
    "timestamp": 1786165200000,
}


def jwt(issued_at: int, expires_at: int) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'HS256'})}.{encode({'sub': 'synthetic', 'iat': issued_at, 'exp': expires_at})}.signature"


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


class CryptoTests(unittest.TestCase):
    def test_python_aes_matches_the_captured_node_implementation(self):
        envelope = encrypt_control_payload(
            SYNTHETIC_PAYLOAD, session_key="AbCdEf0123456789"
        )
        self.assertEqual(
            envelope["requestData"],
            "3oaqikPjfMIERh5Ku/bc9pzRpCQ64RY+AopswAcKuhF4lmCEDvTv+D89hTj95Ya"
            "MF8S6eG6q/55ErsAUUjkjd+aTby/rcKO/iLzEa8RHth7t6B92FzpDzSI+JvUed"
            "cukSTZvwMTj4v55BdICHQUT7gDY3iho8GN8q8xU6w/o8cQifSerfvy4n0P/4sU"
            "dnaaRE8+whWywPm+QWUtOtE0/eWzCePHwTTfQnD/MQJGcJ5jpSZacgllA1kZpU"
            "73ELuA+",
        )
        self.assertEqual(len(base64.b64decode(envelope["encrypted"])), 256)

    def test_normalization_preserves_complete_state_order(self):
        normalized = normalize_control_payload(SYNTHETIC_PAYLOAD)
        self.assertEqual(tuple(normalized), CONTROL_FIELDS)
        with self.assertRaisesRegex(ValueError, "windSpeed"):
            normalize_control_payload({**SYNTHETIC_PAYLOAD, "windSpeed": 7})


class ModelTests(unittest.TestCase):
    def test_status_values_are_normalized_without_inventing_fan_target(self):
        unit = GreeUnit.from_api(SYNTHETIC_UNIT)
        self.assertEqual(unit.set_temperature, 24.5)
        self.assertEqual(unit.environment_temperature, 25.0)
        self.assertEqual(unit.reported_wind_speed, 7)
        self.assertTrue(unit.power)
        self.assertTrue(unit.online)
        self.assertTrue(unit.is_master)
        self.assertTrue(unit.control_identity_is_valid())
        self.assertNotIn("room_name", unit.safe_diagnostics())
        self.assertNotIn("mac", unit.safe_diagnostics())


class FanPolicyTests(unittest.TestCase):
    def test_unconfigured_or_invalid_target_defaults_to_auto(self):
        self.assertEqual(effective_fan_target(None), "auto")
        self.assertEqual(effective_fan_target("invalid"), "auto")
        self.assertEqual(effective_fan_target({"unexpected": "shape"}), "auto")

    def test_explicit_target_overrides_default(self):
        self.assertEqual(effective_fan_target("medium_high"), "medium_high")

    def test_auto_target_is_never_inferred_from_execution_level(self):
        self.assertEqual(
            reconcile_fixed_fan_target(
                "auto", reported_wind_speed=3, power=True, mode=1
            ),
            "auto",
        )

    def test_external_fixed_level_replaces_stale_fixed_target(self):
        self.assertEqual(
            reconcile_fixed_fan_target(
                "high", reported_wind_speed=3, power=True, mode=1
            ),
            "low",
        )

    def test_non_adjustable_state_does_not_reconcile_fixed_target(self):
        for power, mode in ((False, 1), (True, 2), (True, 5)):
            with self.subTest(power=power, mode=mode):
                self.assertEqual(
                    reconcile_fixed_fan_target(
                        "high",
                        reported_wind_speed=3,
                        power=power,
                        mode=mode,
                    ),
                    "high",
                )

    def test_homekit_four_step_modes_map_to_fixed_levels_1_2_3_5(self):
        self.assertEqual(
            HOMEKIT_FAN_MODES,
            ["off", "auto", "low", "middle", "medium", "high"],
        )
        self.assertEqual(control_target_for_homekit_fan_mode("off"), "auto")
        self.assertEqual(control_target_for_homekit_fan_mode("auto"), "auto")
        self.assertEqual(control_target_for_homekit_fan_mode("low"), "low")
        self.assertEqual(control_target_for_homekit_fan_mode("middle"), "medium_low")
        self.assertEqual(control_target_for_homekit_fan_mode("medium"), "medium")
        self.assertEqual(control_target_for_homekit_fan_mode("high"), "high")

    def test_homekit_state_uses_server_report_for_fixed_targets(self):
        expected = {3: "low", 4: "middle", 5: "medium", 6: "medium", 7: "high"}
        for reported, homekit_mode in expected.items():
            with self.subTest(reported=reported):
                self.assertEqual(
                    homekit_fan_mode_from_state(
                        "high", reported_wind_speed=reported, power=True
                    ),
                    homekit_mode,
                )

    def test_homekit_state_exposes_auto_separately_and_reports_power_off(self):
        self.assertEqual(
            homekit_fan_mode_from_state("auto", reported_wind_speed=7, power=True),
            "auto",
        )
        self.assertEqual(
            homekit_fan_mode_from_state("high", reported_wind_speed=7, power=False),
            "off",
        )

    def test_powered_off_unit_never_sends_fan_control(self):
        self.assertFalse(should_send_fan_control(power=False, mode=1))
        self.assertFalse(should_send_fan_control(power=False, mode=5))
        self.assertTrue(should_send_fan_control(power=True, mode=1))
        self.assertFalse(should_send_fan_control(power=True, mode=2))
        self.assertFalse(should_send_fan_control(power=True, mode=5))

    def test_command_readback_occurs_at_three_and_five_seconds(self):
        self.assertEqual(WRITE_READBACK_DELAY, 3)
        self.assertEqual(FAN_FINAL_READBACK_ADDITIONAL_DELAY, 2)


class SystemPolicyTests(unittest.TestCase):
    @staticmethod
    def units(*values):
        normalized = [GreeUnit.from_api(value) for value in values]
        return {unit.unique_id: unit for unit in normalized}

    @staticmethod
    def slave(**updates):
        return {
            **SYNTHETIC_UNIT,
            "roomName": "Synthetic slave",
            "mac": "synthetic-slave",
            "ip": "2",
            "mainIDU": "0",
            **updates,
        }

    def test_explicit_master_mode_sets_direction_even_while_off(self):
        master = {**SYNTHETIC_UNIT, "on_OFF_Status": "0", "mode": "1"}
        state = system_direction(self.units(master, self.slave()))
        self.assertEqual(state.direction, DIRECTION_COOLING)
        self.assertEqual(state.source, SOURCE_MASTER_MODE)

    def test_master_auto_direction_is_inferred_from_powered_slave(self):
        master = {**SYNTHETIC_UNIT, "mode": "5"}
        units = self.units(master, self.slave(mode="4", on_OFF_Status="1"))
        state = system_direction(units)
        self.assertEqual(state.direction, DIRECTION_HEATING)
        self.assertEqual(state.source, SOURCE_ACTIVE_SLAVE)

    def test_master_auto_without_directional_slave_remains_unknown(self):
        master = {**SYNTHETIC_UNIT, "mode": "5"}
        units = self.units(master, self.slave(mode="3", on_OFF_Status="1"))
        self.assertEqual(system_direction(units).direction, DIRECTION_UNKNOWN)

    def test_slave_modes_follow_resolved_direction_and_never_include_auto(self):
        master = {**SYNTHETIC_UNIT, "mode": "4"}
        units = self.units(master, self.slave(mode="3"))
        slave = next(unit for unit in units.values() if not unit.is_master)
        self.assertEqual(allowed_mode_codes(slave, units), (4, 3))
        self.assertNotIn(5, allowed_mode_codes(slave, units))

    def test_known_opposite_slave_direction_is_rejected_locally(self):
        units = self.units(SYNTHETIC_UNIT, self.slave(mode="3"))
        slave = next(unit for unit in units.values() if not unit.is_master)
        with self.assertRaisesRegex(SystemModeConflictError, "requires heating"):
            validate_control_change(
                units,
                slave.unique_id,
                {"on_OFF_Status": 1, "mode": 4},
            )

    def test_unknown_master_auto_direction_is_left_to_controller(self):
        master = {**SYNTHETIC_UNIT, "mode": "5"}
        units = self.units(master, self.slave(mode="3", on_OFF_Status="0"))
        slave = next(unit for unit in units.values() if not unit.is_master)
        validate_control_change(
            units,
            slave.unique_id,
            {"on_OFF_Status": 1, "mode": 4},
        )
        self.assertEqual(
            allowed_mode_codes(slave, units),
            (1, 4, 2, 3),
        )

    def test_slave_auto_is_always_rejected(self):
        units = self.units(SYNTHETIC_UNIT, self.slave())
        slave = next(unit for unit in units.values() if not unit.is_master)
        with self.assertRaisesRegex(SystemModeConflictError, "only on the master"):
            validate_control_change(
                units,
                slave.unique_id,
                {"on_OFF_Status": 1, "mode": 5},
            )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_units_uses_captured_form_and_normalizes_strings(self):
        session = FakeSession(
            [{"success": True, "code": 0, "data": {"units": [SYNTHETIC_UNIT]}}]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        units = await client.async_get_units()
        self.assertEqual(len(units), 1)
        request = session.requests[0]
        self.assertTrue(request[1].endswith("/gree2/app/v2.0/control/getUnits"))
        self.assertEqual(
            request[2]["headers"]["Authorization"], "Bearer synthetic-token"
        )
        self.assertEqual(request[2]["data"]["tyFlag"], "false")

    async def test_refresh_updates_token_and_calls_secure_persistence_callback(self):
        new_token = jwt(100, 300)
        session = FakeSession(
            [
                {
                    "success": True,
                    "code": 0,
                    "data": {
                        "token_type": "Bearer",
                        "access_token": new_token,
                        "expires_in": 200_000,
                    },
                }
            ]
        )
        saved = []
        client = GreeCloudApi(
            session,
            token="old-synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
            token_callback=saved.append,
        )
        result = await client.async_refresh_token()
        self.assertEqual(saved, [f"Bearer {new_token}"])
        self.assertEqual(result["expires_in_ms"], 200_000)
        self.assertEqual(result["timing"], (100, 300))
        params = session.requests[0][2]["params"]
        self.assertEqual(params, {"oldToken": "old-synthetic-token"})

    async def test_control_uses_explicit_target_not_reported_fan_speed(self):
        session = FakeSession(
            [
                {"success": True, "code": 0, "data": {"units": [SYNTHETIC_UNIT]}},
                {"success": True, "code": 0},
            ]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        unit_key = GreeUnit.from_api(SYNTHETIC_UNIT).unique_id
        captured = []
        with patch(
            "custom_components.gree_gmv_cloud.api.encrypt_control_payload",
            side_effect=lambda payload: (
                captured.append(payload) or {"requestData": "x", "encrypted": "y"}
            ),
        ):
            await client.async_control_unit(
                unit_key,
                wind_target_code=1,
                changes={"setTemp": 23},
            )
        self.assertEqual(captured[0]["windSpeed"], 1)
        self.assertNotEqual(captured[0]["windSpeed"], SYNTHETIC_UNIT["windSpeed"])
        self.assertEqual(captured[0]["setTemp"], 23)
        self.assertEqual(
            len([r for r in session.requests if r[1].endswith("controlProduct")]), 1
        )

    async def test_full_state_write_reconciles_external_fixed_fan_change(self):
        externally_lowered = {**SYNTHETIC_UNIT, "windSpeed": "3"}
        session = FakeSession(
            [
                {
                    "success": True,
                    "code": 0,
                    "data": {"units": [externally_lowered]},
                },
                {"success": True, "code": 0},
            ]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        unit_key = GreeUnit.from_api(externally_lowered).unique_id
        captured = []
        with patch(
            "custom_components.gree_gmv_cloud.api.encrypt_control_payload",
            side_effect=lambda payload: (
                captured.append(payload) or {"requestData": "x", "encrypted": "y"}
            ),
        ):
            applied_code = await client.async_control_unit(
                unit_key,
                wind_target_code=6,
                changes={"setTemp": 26},
                reconcile_reported_fan=True,
            )
        self.assertEqual(applied_code, 2)
        self.assertEqual(captured[0]["windSpeed"], 2)
        self.assertEqual(captured[0]["setTemp"], 26)

    async def test_off_to_on_transition_always_uses_automatic_fan(self):
        powered_off = {
            **SYNTHETIC_UNIT,
            "on_OFF_Status": "0",
            "windSpeed": "1",
        }
        session = FakeSession(
            [
                {"success": True, "code": 0, "data": {"units": [powered_off]}},
                {"success": True, "code": 0},
            ]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        unit_key = GreeUnit.from_api(powered_off).unique_id
        captured = []
        with patch(
            "custom_components.gree_gmv_cloud.api.encrypt_control_payload",
            side_effect=lambda payload: (
                captured.append(payload) or {"requestData": "x", "encrypted": "y"}
            ),
        ):
            applied_code = await client.async_control_unit(
                unit_key,
                wind_target_code=6,
                changes={"on_OFF_Status": 1},
                reconcile_reported_fan=True,
            )
        self.assertEqual(applied_code, 1)
        self.assertEqual(captured[0]["on_OFF_Status"], 1)
        self.assertEqual(captured[0]["windSpeed"], 1)

    async def test_idempotent_power_on_does_not_reset_running_fan(self):
        session = FakeSession(
            [
                {"success": True, "code": 0, "data": {"units": [SYNTHETIC_UNIT]}},
                {"success": True, "code": 0},
            ]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        unit_key = GreeUnit.from_api(SYNTHETIC_UNIT).unique_id
        captured = []
        with patch(
            "custom_components.gree_gmv_cloud.api.encrypt_control_payload",
            side_effect=lambda payload: (
                captured.append(payload) or {"requestData": "x", "encrypted": "y"}
            ),
        ):
            applied_code = await client.async_control_unit(
                unit_key,
                wind_target_code=6,
                changes={"on_OFF_Status": 1},
                reconcile_reported_fan=True,
            )
        self.assertEqual(applied_code, 6)
        self.assertEqual(captured[0]["windSpeed"], 6)

    async def test_transport_exception_is_sanitized(self):
        secret = "sensitive-old-token"
        session = FakeSession([RuntimeError(f"request contained {secret}")])
        client = GreeCloudApi(
            session,
            token=secret,
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        with self.assertRaises(GreeConnectionError) as raised:
            await client.async_get_units()
        self.assertNotIn(secret, str(raised.exception))

    async def test_ambiguous_control_failure_is_not_retried(self):
        session = FakeSession(
            [
                {"success": True, "code": 0, "data": {"units": [SYNTHETIC_UNIT]}},
                RuntimeError("synthetic connection loss"),
            ]
        )
        client = GreeCloudApi(
            session,
            token="synthetic-token",
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        unit_key = GreeUnit.from_api(SYNTHETIC_UNIT).unique_id
        with (
            patch(
                "custom_components.gree_gmv_cloud.api.encrypt_control_payload",
                return_value={"requestData": "x", "encrypted": "y"},
            ),
            self.assertRaises(GreeControlError) as raised,
        ):
            await client.async_control_unit(
                unit_key,
                wind_target_code=1,
                changes={"on_OFF_Status": 0},
            )
        self.assertTrue(raised.exception.ambiguous_write)
        self.assertEqual(len(session.requests), 2)

    async def test_refresh_transport_error_does_not_expose_query_token(self):
        secret = "sensitive-refresh-token"
        session = FakeSession([RuntimeError(f"URL included {secret}")])
        client = GreeCloudApi(
            session,
            token=secret,
            open_id="synthetic-open-id",
            uid="synthetic-uid",
        )
        with self.assertRaises(GreeConnectionError) as raised:
            await client.async_refresh_token()
        self.assertNotIn(secret, str(raised.exception))

    def test_jwt_timing_is_metadata_only(self):
        self.assertEqual(decode_jwt_timing(jwt(100, 300)), (100, 300))
        self.assertIsNone(decode_jwt_timing("not-a-jwt"))


if __name__ == "__main__":
    unittest.main()
