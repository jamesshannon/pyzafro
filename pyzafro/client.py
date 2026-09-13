"""Top-level client: authentication, enumeration, and the transport lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .auth import Authenticator
from .const import BASE_URL, WS_HOST, WS_PATH, WS_PORT
from .device import ZafroDevice
from .mqtt import ZafroMqtt
from .rest import ZafroRest

if TYPE_CHECKING:
    import ssl

    import aiohttp

_LOGGER = logging.getLogger(__name__)


class ZafroClient:
    """A ZAFRO cloud account.

    Typical use::

        client = ZafroClient(session, email, password, client_id="ha-3f9c1a2b")
        devices = await client.async_get_devices()
        listener = asyncio.create_task(client.listen())
        for device in devices:
            await device.async_refresh_base_info()
            await device.async_refresh()

    `client_id` must be stable across restarts and unique per installation. The
    phone app uses ``app_{user_id}``; reusing it makes the broker evict whichever
    client connected first, so the app and this library kick each other in a loop.
    Generate one once, store it, and pass the same value every time.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        client_id: str,
        base_url: str = BASE_URL,
        ws_host: str = WS_HOST,
        ws_port: int = WS_PORT,
        ws_path: str = WS_PATH,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        """Wire up the collaborators. The session is borrowed, never closed here."""
        self._auth = Authenticator(session, email, password, base_url)
        self._rest = ZafroRest(session, self._auth, base_url)
        self._mqtt = ZafroMqtt(
            self._rest,
            self._auth,
            client_id,
            host=ws_host,
            port=ws_port,
            path=ws_path,
            tls_context=tls_context,
        )
        self._devices: dict[str, ZafroDevice] = {}

    @property
    def devices(self) -> list[ZafroDevice]:
        """Every device this client is currently tracking.

        Grows with enumeration and shrinks only on `async_forget`.
        """
        return list(self._devices.values())

    @property
    def connected(self) -> bool:
        """Whether the MQTT connection is currently up."""
        return self._mqtt.connected

    async def async_authenticate(self) -> None:
        """Log in, or raise ZafroAuthError if the credentials are wrong.

        Useful on its own for validating credentials during setup.
        """
        await self._auth.async_get_token()

    async def async_get_devices(self) -> list[ZafroDevice]:
        """Enumerate the account, returning exactly what it reports right now.

        Safe to call again later to pick up devices added in the app; a device already
        known is returned as the same ZafroDevice object, so subscriptions survive.

        A device that has stopped being reported is *not* forgotten — it is simply
        absent from the return value. Deciding that an absence is real, rather than a
        blip in a cloud API, is a policy question this library has no basis to answer;
        the caller makes that call and then says so with `async_forget`.
        """
        current: list[ZafroDevice] = []
        for raw in await self._rest.async_get_devices():
            sn = str(raw.get("sn") or "")
            vendor = str(raw.get("vendor") or "")
            if not sn or not vendor:
                _LOGGER.warning("Skipping device with no sn/vendor: %r", raw)
                continue
            if (device := self._devices.get(sn)) is None:
                device = ZafroDevice(raw, self._mqtt)
                self._devices[sn] = device
                await self._mqtt.async_register(device)
                _LOGGER.debug("Discovered %r", device)
            current.append(device)
        return current

    async def async_forget(self, sn: str) -> None:
        """Drop a device this client should stop tracking.

        Unroutes its topics, cancels its pending work, and removes it from `devices`.
        Unknown serial numbers are ignored, so this is safe to call twice.
        """
        device = self._devices.pop(sn, None)
        if device is None:
            return
        await self._mqtt.async_unregister(device)
        device.close()
        _LOGGER.debug("Forgot %r", device)

    async def async_get_rooms(self) -> list[dict[str, Any]]:
        """Fetch rooms, joinable to devices on room_id."""
        return await self._rest.async_get_rooms()

    async def listen(self) -> None:
        """Run the MQTT connection until cancelled. Start this as a background task."""
        await self._mqtt.listen()

    async def async_wait_connected(self, timeout: float = 30.0) -> None:
        """Block until the broker connection is established."""
        await self._mqtt.wait_connected(timeout)

    def close(self) -> None:
        """Release scheduled work. The listener task is the caller's to cancel.

        The aiohttp session is borrowed, so it is deliberately left open.
        """
        self._mqtt.close()
        for device in self._devices.values():
            device.close()

    def diagnostics(self) -> dict[str, Any]:
        """Redacted dump of every device, for bug reports."""
        return {
            "connected": self.connected,
            "devices": [device.diagnostics() for device in self.devices],
        }
