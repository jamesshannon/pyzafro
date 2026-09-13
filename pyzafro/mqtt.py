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
    OFFLINE_GRACE,
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


class _UnresponsiveError(Exception):
    """The broker stopped answering, so the socket is treated as already dead.

    Internal to this module. Raised in place of the `aiomqtt.MqttError` the same
    situation would eventually produce on its own, so the reconnect path is shared.
    """


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
        self._offline_handle: asyncio.TimerHandle | None = None
        self._unresponsive = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Whether the broker connection is currently up."""
        return self._connected.is_set()

    async def _async_tls_context(self) -> ssl.SSLContext:
        """Return the TLS context, building a default one off the event loop.

        `ssl.create_default_context()` reads the system trust store from disk, which
        blocks. Calling it from a running loop stalls everything else on that loop;
        Home Assistant detects it and asks the user to file a bug. Built once, in a
        thread, and reused for every reconnect after that.

        A caller that already has a context — anything running inside an application
        with a shared, pre-warmed one — should pass it to the constructor instead.
        """
        if self._tls_context is None:
            self._tls_context = await asyncio.to_thread(ssl.create_default_context)
        return self._tls_context

    def _topics(self, sink: FrameSink) -> tuple[str, str]:
        return (
            TOPIC_REPLY.format(vendor=sink.vendor, sn=sink.sn),
            TOPIC_LWT.format(vendor=sink.vendor, sn=sink.sn),
        )

    async def async_register(self, sink: FrameSink) -> None:
        """Route this device's topics to `sink`, subscribing if already connected.

        Devices found by a later enumeration arrive while the connection is up, so the
        subscription cannot wait for the next connect. A failure here is not fatal:
        every topic in `_sinks` is subscribed again on reconnect.
        """
        topics = self._topics(sink)
        for topic in topics:
            self._sinks[topic] = sink

        client = self._client
        if client is None or not self._connected.is_set():
            return
        try:
            for topic in topics:
                await client.subscribe(topic)
        except aiomqtt.MqttError as err:
            _LOGGER.debug(
                "Deferring subscription for %s to reconnect: %s", sink.sn, err
            )

    async def async_unregister(self, sink: FrameSink) -> None:
        """Stop routing this device's topics. Safe to call when never registered."""
        topics = self._topics(sink)
        for topic in topics:
            self._sinks.pop(topic, None)

        client = self._client
        if client is None or not self._connected.is_set():
            return
        try:
            for topic in topics:
                await client.unsubscribe(topic)
        except aiomqtt.MqttError as err:
            _LOGGER.debug("Could not unsubscribe %s: %s", sink.sn, err)

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
                # Neither is retried, so there is nothing to wait out.
                self._handle_disconnect(may_return=False)
                raise
            except (aiomqtt.MqttError, _UnresponsiveError) as err:
                _LOGGER.debug("MQTT connection lost: %s", err)
                self._handle_disconnect(may_return=True)
            except Exception:
                _LOGGER.exception("Unexpected MQTT failure")
                self._handle_disconnect(may_return=True)

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
            tls_context=await self._async_tls_context(),
            logger=_LOGGER.getChild("aiomqtt"),
        )
        async with client:
            self._client = client
            for topic in self._sinks:
                await client.subscribe(topic)
            self._cancel_offline()
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

            await self._dispatch_until_lost(client)

    async def _dispatch_until_lost(self, client: aiomqtt.Client) -> None:
        """Route inbound frames until the socket drops or is declared dead.

        The iteration itself cannot notice a half-open socket — that is the whole
        problem — so it is raced against `_unresponsive`, which a device sets when a
        request it expected an answer to went unanswered.
        """
        self._unresponsive.clear()
        pump = asyncio.create_task(self._pump(client))
        declared = asyncio.create_task(self._unresponsive.wait())
        try:
            done, _ = await asyncio.wait(
                (pump, declared), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (pump, declared):
                task.cancel()
        if pump in done:
            # Re-raises whatever ended the iteration, usually an MqttError.
            pump.result()
            return
        raise _UnresponsiveError("Broker stopped answering")

    async def _pump(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            self._dispatch(str(message.topic), message.payload)

    def note_unresponsive(self, sn: str) -> None:
        """Report that a device answered nothing, so the connection looks dead.

        A websocket the broker has hung up on stays readable for up to a keepalive
        interval, and a command published into it is simply lost. A request that timed
        out is the earliest evidence available, so it is used to tear the connection
        down rather than waiting for the ping to notice.

        The caller only reports this for a device it still believes is present; a
        device the last-will topic has already declared gone explains its own silence
        and says nothing about the socket.
        """
        if not self._connected.is_set() or self._unresponsive.is_set():
            return
        _LOGGER.debug("%s went unanswered; reconnecting rather than waiting", sn)
        self._unresponsive.set()

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

    def _handle_disconnect(self, *, may_return: bool) -> None:
        """Tear down the dead connection, deciding when devices become unavailable.

        A retryable drop usually resolves in a second or two, so the availability
        change waits `OFFLINE_GRACE` and is cancelled outright if the reconnect beats
        it. Without that, every routine websocket drop flaps the consumer's whole
        device set — visible, alarming, and over before anyone can read it.
        """
        self._client = None
        if not self._connected.is_set():
            # Never got as far as CONNACK, so nothing was reported reachable and
            # any timer from the drop that started this retry loop still stands.
            return
        self._connected.clear()
        if not may_return:
            self._mark_all_offline()
            return
        self._offline_handle = asyncio.get_running_loop().call_later(
            OFFLINE_GRACE, self._mark_all_offline
        )

    def _cancel_offline(self) -> None:
        """Call off a pending availability drop, because the connection came back."""
        if self._offline_handle is not None:
            self._offline_handle.cancel()
            self._offline_handle = None

    def _mark_all_offline(self) -> None:
        self._offline_handle = None
        for sink in set(self._sinks.values()):
            sink.handle_presence(online=False)

    def close(self) -> None:
        """Drop scheduled work. The listener task is the caller's to cancel."""
        self._cancel_offline()

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
