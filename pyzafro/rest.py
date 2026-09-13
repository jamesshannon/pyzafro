"""Authenticated REST calls.

Only the endpoints the library actually needs are here. The full decoded endpoint
list is in ZAFRO_API_NOTES.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import (
    OK_CODE,
    PATH_DEVICE_LIST,
    PATH_MQTT_USERINFO,
    PATH_ROOM_LIST,
    UNAUTHORIZED_CODES,
)
from .exceptions import (
    ZafroApiError,
    ZafroAuthError,
    ZafroConnectionError,
    ZafroTimeoutError,
)
from .models import flatten_device_list

if TYPE_CHECKING:
    import aiohttp

    from .auth import Authenticator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MqttCredentials:
    """Broker credentials. Unrelated to the REST session token."""

    username: str
    password: str


class ZafroRest:
    """Thin REST client that unwraps the response envelope."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        authenticator: Authenticator,
        base_url: str,
    ) -> None:
        """Store collaborators. The session is borrowed, never mutated or closed."""
        self._session = session
        self._auth = authenticator
        self._base_url = base_url.rstrip("/")

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return a flat list of devices.

        The server groups them by room; see flatten_device_list. The response carries no
        device state at all, so MQTT is required even for read-only use.
        """
        data = await self._async_request("GET", PATH_DEVICE_LIST)
        return flatten_device_list(data)

    async def async_get_mqtt_credentials(self) -> MqttCredentials:
        """Fetch the broker username and password."""
        data = await self._async_request("GET", PATH_MQTT_USERINFO)
        if not isinstance(data, dict) or "username" not in data:
            raise ZafroApiError(OK_CODE, "mqtt/userinfo returned no credentials")
        return MqttCredentials(
            username=str(data["username"]), password=str(data.get("password", ""))
        )

    async def async_get_rooms(self) -> list[dict[str, Any]]:
        """Fetch the room list, joinable to devices on room_id."""
        data = await self._async_request("GET", PATH_ROOM_LIST)
        return data if isinstance(data, list) else []

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        _retry: bool = True,
    ) -> Any:
        """Perform one authenticated call and unwrap the envelope.

        Headers are built per request. The aiohttp session is shared with other
        consumers (in Home Assistant, with every other integration), so the
        Authorization header must never be set on the session itself.
        """
        token = await self._auth.async_get_token()
        headers = {"Language": "en", "Authorization": f"Bearer {token}"}
        url = f"{self._base_url}{path}"

        try:
            async with self._session.request(
                method, url, json=json, headers=headers
            ) as response:
                body: Any = await response.json(content_type=None)
        except TimeoutError as err:
            raise ZafroTimeoutError(f"Timed out calling {path}") from err
        except Exception as err:  # aiohttp.ClientError and friends
            raise ZafroConnectionError(f"Could not reach {url}: {err}") from err

        if not isinstance(body, dict):
            raise ZafroConnectionError(f"Unexpected response from {path}: {body!r}")

        # The envelope code is authoritative; the HTTP status is not.
        code = body.get("code")
        if code in UNAUTHORIZED_CODES:
            if _retry:
                _LOGGER.debug("Session rejected on %s; re-authenticating", path)
                self._auth.invalidate()
                return await self._async_request(method, path, json=json, _retry=False)
            raise ZafroAuthError(body.get("msg") or "Session rejected after re-login")
        if code != OK_CODE:
            raise ZafroApiError(int(code or -1), str(body.get("msg") or ""))

        return body.get("data")
