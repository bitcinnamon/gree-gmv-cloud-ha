"""Asynchronous client for the captured private Gree GMV cloud API."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Protocol

from .const import DEFAULT_BASE_URL, TOKEN_REFRESH_MARGIN
from .crypto import encrypt_control_payload
from .models import GreeUnit


class GreeApiError(Exception):
    """Base error that deliberately contains no request URL or credential."""


class GreeAuthError(GreeApiError):
    """Authentication failed."""


class GreeConnectionError(GreeApiError):
    """The cloud request failed before a definite application response."""


class GreeProtocolError(GreeApiError):
    """The cloud returned an unexpected response."""


class GreeControlError(GreeApiError):
    """A control command failed or has an ambiguous outcome."""

    def __init__(self, message: str, *, ambiguous_write: bool = False) -> None:
        super().__init__(message)
        self.ambiguous_write = ambiguous_write


class HttpSession(Protocol):
    """Small subset of aiohttp.ClientSession used by this client."""

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


TokenCallback = Callable[[str], Awaitable[None] | None]


def normalize_bearer(token: str) -> str:
    """Return exactly one canonical Bearer prefix."""
    value = token.strip()
    if not value:
        raise ValueError("token is required")
    if value.lower().startswith("bearer "):
        return f"Bearer {value.split(None, 1)[1]}"
    return f"Bearer {value}"


def decode_jwt_timing(token: str) -> tuple[int, int] | None:
    """Decode unverified iat/exp only for refresh scheduling."""
    raw = normalize_bearer(token).split(None, 1)[1]
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return None
    return issued_at, expires_at


class GreeCloudApi:
    """Credential-owning client with serialized writes and token refresh."""

    def __init__(
        self,
        session: HttpSession,
        *,
        token: str,
        open_id: str,
        uid: str,
        base_url: str = DEFAULT_BASE_URL,
        token_callback: TokenCallback | None = None,
    ) -> None:
        self._session = session
        self._token = normalize_bearer(token)
        self._open_id = open_id
        self._uid = uid
        self._base_url = base_url.rstrip("/")
        self._token_callback = token_callback
        self._refresh_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def token(self) -> str:
        """Return the current token for immediate secure persistence only."""
        return self._token

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
        content_type: str,
        is_write: bool = False,
    ) -> dict[str, Any]:
        headers = {"Authorization": self._token, "Content-Type": content_type}
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    data=data,
                    json=json_body,
                    headers=headers,
                ) as response:
                    if response.status in (401, 403):
                        raise GreeAuthError("Gree cloud rejected the current credential")
                    if response.status < 200 or response.status >= 300:
                        error_type = GreeControlError if is_write else GreeConnectionError
                        if is_write:
                            raise error_type(
                                f"Gree cloud returned HTTP {response.status}",
                                ambiguous_write=False,
                            )
                        raise error_type(f"Gree cloud returned HTTP {response.status}")
                    result = await response.json(content_type=None)
        except GreeApiError:
            raise
        except TimeoutError:
            if is_write:
                raise GreeControlError(
                    "Gree cloud control timed out; device state is unknown",
                    ambiguous_write=True,
                ) from None
            raise GreeConnectionError("Gree cloud request timed out") from None
        except Exception:
            if is_write:
                raise GreeControlError(
                    "Gree cloud control transport failed; device state is unknown",
                    ambiguous_write=True,
                ) from None
            raise GreeConnectionError("Gree cloud transport failed") from None
        if not isinstance(result, dict):
            raise GreeProtocolError("Gree cloud returned non-object JSON")
        return result

    async def async_ensure_fresh_token(self) -> None:
        """Refresh once when the JWT is within six hours of expiry."""
        timing = decode_jwt_timing(self._token)
        if timing is None or timing[1] - time.time() > TOKEN_REFRESH_MARGIN.total_seconds():
            return
        async with self._refresh_lock:
            timing = decode_jwt_timing(self._token)
            if timing is None or timing[1] - time.time() > TOKEN_REFRESH_MARGIN.total_seconds():
                return
            await self.async_refresh_token()

    async def async_refresh_token(self) -> dict[str, Any]:
        """Exchange the current token and persist the replacement via callback."""
        raw_token = self._token.split(None, 1)[1]
        response = await self._request_json(
            "POST",
            "/gree2/app/v3.0/authExternal/refreshToken",
            params={"oldToken": raw_token},
            content_type="application/json",
        )
        data = response.get("data")
        if not isinstance(data, dict) or not data.get("access_token") or not data.get("token_type"):
            raise GreeAuthError("Gree cloud returned an invalid refresh response")
        new_token = normalize_bearer(f"{data['token_type']} {data['access_token']}")
        self._token = new_token
        if self._token_callback is not None:
            callback_result = self._token_callback(new_token)
            if inspect.isawaitable(callback_result):
                await callback_result
        return {
            "expires_in_ms": data.get("expires_in"),
            "timing": decode_jwt_timing(new_token),
        }

    async def async_get_units(self) -> dict[str, GreeUnit]:
        """Fetch and normalize all indoor units with one coordinated request."""
        await self.async_ensure_fresh_token()
        response = await self._request_json(
            "POST",
            "/gree2/app/v2.0/control/getUnits",
            data={
                "openId": self._open_id,
                "uid": self._uid,
                "roomName": "11",
                "tyFlag": "false",
                "flag": "true",
            },
            content_type="application/x-www-form-urlencoded",
        )
        response_data = response.get("data")
        units = (
            response_data.get("units")
            if response.get("success") and isinstance(response_data, dict)
            else None
        )
        if response.get("code") != 0 or not isinstance(units, list):
            raise GreeProtocolError("Gree cloud returned an invalid unit-state response")
        normalized = [GreeUnit.from_api(unit) for unit in units if isinstance(unit, dict)]
        return {unit.unique_id: unit for unit in normalized}

    async def async_control_unit(
        self,
        unit_key: str,
        *,
        wind_target_code: int,
        changes: dict[str, int | float],
    ) -> None:
        """Read fresh state, merge explicit safe fan target, and write exactly once."""
        if wind_target_code not in (1, 2, 3, 4, 5, 6):
            raise ValueError("wind_target_code must be 1 through 6")
        allowed = {"setTemp", "on_OFF_Status", "mode"}
        if unsupported := set(changes) - allowed:
            raise ValueError(f"Unsupported control fields: {', '.join(sorted(unsupported))}")
        async with self._write_lock:
            units = await self.async_get_units()
            try:
                unit = units[unit_key]
            except KeyError:
                raise GreeProtocolError("The selected indoor unit is no longer present") from None
            if not unit.online:
                raise GreeControlError("The selected indoor unit is offline")
            if not unit.control_identity_is_valid():
                raise GreeProtocolError("The indoor unit lacks required DTU control fields")
            if unit.set_temperature is None:
                raise GreeProtocolError("The indoor unit has no current set temperature")
            payload: dict[str, Any] = {
                "openId": self._open_id,
                "mac": unit.mac,
                "ip": unit.ip,
                "setTemp": unit.set_temperature,
                "on_OFF_Status": int(unit.power),
                "mode": unit.mode,
                "windSpeed": wind_target_code,
                "systemId": unit.system_id,
                "bindType": "DTU",
                "timestamp": int(time.time() * 1000),
            }
            payload.update(changes)
            response = await self._request_json(
                "POST",
                "/gree2/app/v2.0/control/controlProduct",
                json_body=encrypt_control_payload(payload),
                content_type="application/json",
                is_write=True,
            )
            if response.get("success") is not True or response.get("code") != 0:
                raise GreeControlError("Gree cloud rejected the control command")
