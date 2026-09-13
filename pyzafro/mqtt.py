"""MQTT-over-WebSocket transport.

One connection per account, all devices multiplexed. This module owns the reconnect
loop; callers start `listen()` as a long-running task and never touch the socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
from typing import TYPE_CHECKING, Any, Protocol

import aiomqtt

from .const import (
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    TOPIC_LWT,
    TOPIC_REPLY,
    TOPIC_REQUEST,
    WS_HOST,
    WS_PATH,
    WS_PORT,
)
from .exceptions import ZafroAuthError, ZafroConnectionError

if TYPE_CHECKING:
    from .auth import Authenticator
    from .rest import ZafroRest

_LOGGER = logging.getLogger(__name__)


class FrameSink(Protocol):
    """The slice of ZafroDevice this module needs. Keeps the dependency one-way."""

    vendor: str
    sn: str

    def handle_frame(self, cmd: int, result: dict[str, Any]) -> None:
        """Accept a state, base-info, or control reply frame."""

    def handle_presence(self, *, online: bool) -> None:
        """Accept a presence update from the LWT topic."""

    def handle_reconnect(self) -> None:
        """Re-baseline after a reconnect, since missed deltas are never replayed."""


class ZafroMqtt:
    """Connection manager and message router."""

    def __init__(
        self,
        rest: ZafroRest,
        authenticator: Authenticator,
        client_id: str,
        *,
        host: str = WS_HOST,
        port: int = WS_PORT,
        path: str = WS_PATH,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        """Store connection parameters. Nothing connects until listen() runs."""
        self._rest = rest
        self._auth = authenticator
        self._client_id = client_id
        self._host = host
        self._port = port
        self._path = path
        self._tls_context = tls_context
        self._client: aiomqtt.Client | None = None
        self._sinks: dict[str, FrameSink] = {}
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Whether the broker connection is currently up."""
        return self._connected.is_set()

    def register(self, sink: FrameSink) -> None:
        """Route this device's topics to `sink`."""
        self._sinks[TOPIC_REPLY.format(vendor=sink.vendor, sn=sink.sn)] = sink
        self._sinks[TOPIC_LWT.format(vendor=sink.vendor, sn=sink.sn)] = sink

    async def listen(self) -> None:
        """Connect, subscribe, and dispatch forever, reconnecting as needed.

        Cancellation and ZafroAuthError propagate; every other failure is retried
        with backoff. Bad credentials are not transient, and retrying them forever
        would hide a password change from the consumer instead of letting it
        re-prompt.
        """
        delay = RECONNECT_MIN_DELAY
        while True:
            try:
                await self._run_once()
            except (asyncio.CancelledError, ZafroAuthError):
                raise
            except aiomqtt.MqttError as err:
                _LOGGER.debug("MQTT connection lost: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected MQTT failure")
            finally:
                self._client = None
                if self._connected.is_set():
                    self._connected.clear()
                    self._mark_all_offline()

            jittered = delay * (0.8 + random.random() * 0.4)  # noqa: S311
            _LOGGER.debug("Reconnecting to MQTT in %.1fs", jittered)
            await asyncio.sleep(jittered)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _run_once(self) -> None:
        """One connection lifetime: connect, subscribe, dispatch until it drops."""
        credentials = await self._rest.async_get_mqtt_credentials()
        client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=credentials.username,
            password=credentials.password,
            identifier=self._client_id,
            transport="websockets",
            websocket_path=self._path,
            tls_context=self._tls_context or ssl.create_default_context(),
            logger=_LOGGER.getChild("aiomqtt"),
        )
        async with client:
            self._client = client
            for topic in self._sinks:
                await client.subscribe(topic)
            self._connected.set()
            _LOGGER.debug(
                "MQTT connected as %s, subscribed to %d topics",
                self._client_id,
                len(self._sinks),
            )
            # Deltas missed while disconnected are never replayed, so every device
            # re-baselines itself here.
            for sink in set(self._sinks.values()):
                sink.handle_reconnect()

            async for message in client.messages:
                self._dispatch(str(message.topic), message.payload)

    def _dispatch(self, topic: str, payload: Any) -> None:
        sink = self._sinks.get(topic)
        if sink is None:
            return
        try:
            frame = json.loads(
                payload.decode() if isinstance(payload, bytes) else payload
            )
        except (ValueError, UnicodeDecodeError):
            _LOGGER.warning("Unparseable payload on %s: %r", topic, payload)
            return
        if not isinstance(frame, dict):
            return

        if topic.startswith("lwt/"):
            sink.handle_presence(online=bool(frame.get("status")))
            return

        cmd = frame.get("cmd")
        if isinstance(cmd, int):
            result = frame.get("result")
            sink.handle_frame(cmd, result if isinstance(result, dict) else {})

    def _mark_all_offline(self) -> None:
        for sink in set(self._sinks.values()):
            sink.handle_presence(online=False)

    async def publish(self, vendor: str, sn: str, payload: dict[str, Any]) -> None:
        """Publish a command envelope to a device's request topic."""
        client = self._client
        if client is None or not self._connected.is_set():
            raise ZafroConnectionError("Not connected to the MQTT broker")

        user_id = self._auth.user_id
        envelope = {"user": f"app_{user_id}_server", **payload}
        topic = TOPIC_REQUEST.format(vendor=vendor, sn=sn)
        _LOGGER.debug("-> %s %s", topic, envelope)
        try:
            await client.publish(topic, json.dumps(envelope))
        except aiomqtt.MqttError as err:
            raise ZafroConnectionError(f"Publish to {topic} failed: {err}") from err

    async def wait_connected(self, timeout: float) -> None:
        """Block until the broker connection is up, or raise."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError as err:
            raise ZafroConnectionError("Timed out connecting to the broker") from err
