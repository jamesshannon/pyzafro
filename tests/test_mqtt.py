"""Transport concerns that do not need a broker."""

from __future__ import annotations

import asyncio
import logging
import ssl
import threading
from typing import Any

import pytest

from pyzafro import mqtt as mqtt_module
from pyzafro.exceptions import ZafroConnectionError
from pyzafro.mqtt import ZafroMqtt


def _mqtt(**kwargs: Any) -> ZafroMqtt:
    """Build a transport with collaborators it will not reach in these tests."""
    return ZafroMqtt(None, None, "ha-test", **kwargs)  # type: ignore[arg-type]


async def test_default_tls_context_is_built_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading the trust store must not block the caller's event loop.

    Home Assistant watches for exactly this and tells the user to file a bug, so the
    assertion is on where the work happened, not just on what came back.
    """
    real = ssl.create_default_context
    built_on: list[str] = []

    def _record() -> ssl.SSLContext:
        built_on.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(ssl, "create_default_context", _record)

    context = await _mqtt()._async_tls_context()

    assert isinstance(context, ssl.SSLContext)
    assert built_on
    assert threading.main_thread().name not in built_on


async def test_default_tls_context_is_built_once() -> None:
    """Every reconnect reuses the first context rather than re-reading from disk."""
    transport = _mqtt()
    first = await transport._async_tls_context()
    second = await transport._async_tls_context()
    assert first is second


async def test_supplied_tls_context_is_used_as_is() -> None:
    """An application with a pre-warmed context passes it in and nothing is built."""
    supplied = ssl.create_default_context()
    transport = _mqtt(tls_context=supplied)
    assert await transport._async_tls_context() is supplied


class FakeSink:
    """Records presence changes instead of being a device."""

    vendor = "I4SEASON"
    sn = "SN-A"

    def __init__(self) -> None:
        self.presence: list[bool] = []
        self.reasons: list[str] = []
        self.probes = 0
        #: Raised by every probe, to stand in for a device that cannot be reached.
        self.probe_error: Exception | None = None

    def handle_frame(self, cmd: int, result: dict[str, Any]) -> None: ...

    def handle_presence(self, *, online: bool, reason: str) -> None:
        self.presence.append(online)
        self.reasons.append(reason)

    def handle_reconnect(self) -> None: ...

    async def async_probe(self) -> None:
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error


def _connected_transport() -> tuple[ZafroMqtt, FakeSink]:
    """Build a transport that believes it is connected, with one device routed."""
    transport = _mqtt()
    sink = FakeSink()
    transport._sinks["dev/reply"] = sink
    transport._connected.set()
    return transport, sink


async def test_a_retryable_drop_does_not_report_devices_offline_at_once() -> None:
    """The socket dying is not news until the reconnect has had its chance."""
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=True)

    assert sink.presence == []


async def test_a_reconnect_inside_the_grace_is_never_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a blip the consumer never has to know about.

    The grace is shortened so the test can outlast it, which makes the assertion
    meaningful — the timer had time to fire and did not, because reconnecting
    cancelled it.
    """
    monkeypatch.setattr(mqtt_module, "OFFLINE_GRACE", 0.01)
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=True)
    transport._cancel_offline()
    await asyncio.sleep(0.05)

    assert sink.presence == []


async def test_a_drop_that_outlasts_the_grace_reports_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect that never comes has to surface eventually."""
    monkeypatch.setattr(mqtt_module, "OFFLINE_GRACE", 0.01)
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=True)
    await asyncio.sleep(0.05)

    assert sink.presence == [False]


async def test_a_fatal_drop_reports_offline_immediately() -> None:
    """Bad credentials are not retried, so there is nothing to wait out."""
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=False)

    assert sink.presence == [False]


class _StopError(Exception):
    """Breaks out of listen()'s infinite loop from inside the backoff sleep."""


async def test_backoff_grows_while_flapping_and_clears_once_a_connection_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delay used to only ever grow, across the whole life of the process.

    A client up for hours had reached the 300s ceiling one routine drop at a time,
    so a socket that dropped at 14:47 came back at 14:51 — with the broker healthy
    and answering the whole while.
    """
    transport, _ = _connected_transport()
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 5:
            raise _StopError

    monkeypatch.setattr(mqtt_module.asyncio, "sleep", _record)

    async def _drop() -> None:
        # The fifth connection is the one that lasts; the rest die on arrival.
        age = mqtt_module.RECONNECT_RESET_AFTER + 1 if len(delays) == 4 else 0.0
        transport._connected_at = asyncio.get_running_loop().time() - age
        raise mqtt_module.aiomqtt.MqttError("dropped")

    transport._run_once = _drop  # type: ignore[method-assign]
    try:
        with pytest.raises(_StopError):
            await transport.listen()
    finally:
        transport.close()

    flapping = delays[:4]
    assert flapping == sorted(flapping)
    assert flapping[-1] > flapping[0]
    # Back to the floor, not merely smaller than the ceiling it had climbed to.
    assert delays[4] < flapping[-1]
    assert delays[4] <= mqtt_module.RECONNECT_MIN_DELAY * 1.2


async def test_shutdown_says_nothing_about_availability() -> None:
    """Being cancelled is news about the consumer, not about the devices.

    Home Assistant cancels background tasks on the way down while the recorder is
    still writing, so an outage announced here becomes the last thing its logbook
    has to say about every entity — read after the restart as a unit that went
    unreachable, next to a unit that is plainly fine.
    """
    transport, sink = _connected_transport()

    transport._handle_shutdown()

    assert sink.presence == []
    assert not transport.connected


async def test_cancelling_the_listener_reports_no_outage() -> None:
    """The path that actually runs at shutdown, not just the handler behind it."""
    transport, sink = _connected_transport()

    async def _hold() -> None:
        transport._connected.set()
        await asyncio.Event().wait()

    transport._run_once = _hold  # type: ignore[method-assign]
    listener = asyncio.create_task(transport.listen())
    await asyncio.sleep(0)
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener

    assert sink.presence == []


async def test_shutdown_calls_off_a_drop_armed_moments_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timer from the drop that preceded the shutdown must not outlive it."""
    monkeypatch.setattr(mqtt_module, "OFFLINE_GRACE", 0.01)
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=True)
    transport._handle_shutdown()
    await asyncio.sleep(0.05)

    assert sink.presence == []


async def test_closing_calls_off_a_pending_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer that shuts down must not be called back a minute later."""
    monkeypatch.setattr(mqtt_module, "OFFLINE_GRACE", 0.01)
    transport, sink = _connected_transport()

    transport._handle_disconnect(may_return=True)
    transport.close()
    await asyncio.sleep(0.05)

    assert sink.presence == []


async def test_an_unresponsive_report_ends_the_connection() -> None:
    """The dispatch loop cannot see a half-open socket, so a device tells it."""
    transport, _ = _connected_transport()

    transport.note_unresponsive("SN-A")

    assert transport._unresponsive.is_set()


async def test_an_unresponsive_report_while_disconnected_is_ignored() -> None:
    """A reconnect is already under way; nothing to tear down."""
    transport, _ = _connected_transport()
    transport._connected.clear()

    transport.note_unresponsive("SN-A")

    assert not transport._unresponsive.is_set()


class SilentClient:
    """An aiomqtt client whose socket is open but whose broker has gone away."""

    @property
    def messages(self) -> Any:
        return self

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(3600)
        raise AssertionError  # pragma: no cover


async def test_a_silent_socket_is_abandoned_rather_than_waited_out() -> None:
    """Without this the loop blocks until the keepalive notices, a minute later."""
    transport, _ = _connected_transport()
    dispatch = asyncio.create_task(
        transport._dispatch_until_lost(SilentClient())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    transport.note_unresponsive("SN-A")

    # Bounded so a regression fails here rather than hanging the suite, which is
    # exactly what the old code did to the connection.
    with pytest.raises(mqtt_module._UnresponsiveError):
        await asyncio.wait_for(dispatch, timeout=1)


async def test_quiet_devices_are_probed_for_as_long_as_the_connection_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing else re-reads state, so this loop is the only thing that asks."""
    monkeypatch.setattr(mqtt_module, "PROBE_INTERVAL", 0.02)
    transport, sink = _connected_transport()

    probe = asyncio.create_task(transport._probe_quiet_devices())
    await asyncio.sleep(0.07)
    probe.cancel()

    assert sink.probes >= 2


async def test_an_unreachable_device_does_not_end_the_probe_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that cannot be reached is the normal case here, not an error.

    If one could end the loop, the first outage on a multi-device account would
    quietly disable the mechanism for every other device on it.
    """
    monkeypatch.setattr(mqtt_module, "PROBE_INTERVAL", 0.02)
    transport, sink = _connected_transport()
    sink.probe_error = ZafroConnectionError("not connected to the broker")

    probe = asyncio.create_task(transport._probe_quiet_devices())
    await asyncio.sleep(0.07)
    still_running = not probe.done()
    probe.cancel()

    assert still_running
    assert sink.probes >= 2


async def test_the_probe_loop_stops_with_the_connection() -> None:
    """It has no life of its own: a probe outliving its socket publishes into it."""
    transport, sink = _connected_transport()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mqtt_module, "PROBE_INTERVAL", 0.02)
        dispatch = asyncio.create_task(
            transport._dispatch_until_lost(SilentClient())  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)
        assert sink.probes >= 1

        transport.note_unresponsive("SN-A")
        with pytest.raises(mqtt_module._UnresponsiveError):
            await asyncio.wait_for(dispatch, timeout=1)

        after_teardown = sink.probes
        await asyncio.sleep(0.05)

    assert sink.probes == after_teardown


def test_inbound_frames_are_logged_with_their_contents(caplog) -> None:
    """The aiomqtt logger gives topic and byte count; content was nowhere."""
    transport, _ = _connected_transport()
    transport._sinks["dev/I4SEASON/SN-A/command/reply"] = transport._sinks["dev/reply"]

    with caplog.at_level(logging.DEBUG, logger="pyzafro.mqtt"):
        transport._dispatch(
            "dev/I4SEASON/SN-A/command/reply",
            b'{"cmd": 4, "result": {"temperature": 79}}',
        )

    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("<-")]
    assert len(logged) == 1
    assert "temperature" in logged[0]
    assert "79" in logged[0]


def test_the_wifi_network_never_reaches_the_log(caplog) -> None:
    """diagnostics() drops it deliberately; a raw frame log would undo that."""
    transport, _ = _connected_transport()
    transport._sinks["dev/I4SEASON/SN-A/command/reply"] = transport._sinks["dev/reply"]

    with caplog.at_level(logging.DEBUG, logger="pyzafro.mqtt"):
        transport._dispatch(
            "dev/I4SEASON/SN-A/command/reply",
            b'{"cmd": 5, "result": {"ssid": "Shannon Family 5G", "rssi": -52}}',
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Shannon Family 5G" not in logged
    # Still visibly present, so nobody wonders whether the device reported it.
    assert "ssid" in logged
    assert "-52" in logged


def test_a_frame_for_an_unrouted_topic_is_not_silent(caplog) -> None:
    """Otherwise a routing bug looks exactly like a device that said nothing."""
    transport, _ = _connected_transport()

    with caplog.at_level(logging.DEBUG, logger="pyzafro.mqtt"):
        transport._dispatch("dev/I4SEASON/SN-Z/command/reply", b'{"cmd": 4}')

    assert any("no device routed" in r.getMessage() for r in caplog.records)


async def test_an_outage_from_the_transport_says_so(caplog) -> None:
    """The device cannot know why it went unavailable; the transport has to say."""
    transport, sink = _connected_transport()

    with caplog.at_level(logging.DEBUG, logger="pyzafro.mqtt"):
        transport._handle_disconnect(may_return=False)

    assert sink.reasons == ["credentials rejected"]
