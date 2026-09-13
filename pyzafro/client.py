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
        """Devices discovered by the most recent enumeration."""
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
        """Enumerate devices and register them with the transport.

        Safe to call again later to pick up devices added in the app; existing
        ZafroDevice objects are preserved so subscriptions survive.
        """
        for raw in await self._rest.async_get_devices():
            sn = str(raw.get("sn") or "")
            vendor = str(raw.get("vendor") or "")
            if not sn or not vendor:
                _LOGGER.warning("Skipping device with no sn/vendor: %r", raw)
                continue
            if sn in self._devices:
                continue
            device = ZafroDevice(raw, self._mqtt)
            self._devices[sn] = device
            self._mqtt.register(device)
            _LOGGER.debug("Discovered %r", device)
        return self.devices

    async def async_get_rooms(self) -> list[dict[str, Any]]:
        """Fetch rooms, joinable to devices on room_id."""
        return await self._rest.async_get_rooms()

    async def listen(self) -> None:
        """Run the MQTT connection until cancelled. Start this as a background task."""
        await self._mqtt.listen()

    async def async_wait_connected(self, timeout: float = 30.0) -> None:
        """Block until the broker connection is established."""
        await self._mqtt.wait_connected(timeout)

    def diagnostics(self) -> dict[str, Any]:
        """Redacted dump of every device, for bug reports."""
        return {
            "connected": self.connected,
            "devices": [device.diagnostics() for device in self.devices],
        }
