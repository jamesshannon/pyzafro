"""Write path: mode-dependent payloads and optimistic reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest

from pyzafro import capabilities as caps_module
from pyzafro.capabilities import Feature
from pyzafro.device import ZafroDevice
from pyzafro.exceptions import ZafroUnsupportedError
from pyzafro.models import Mode

RAW = {
    "sn": "6ISEComboWF020BSJ0000000000",
    "vendor": "I4SEASON",
    "model": "90038EAC0-12K-ZAZ",
    "name": "Air Conditioner",
    "mac": "001cc2000000",
    "version": "1.0.29",
}


#: A real cmd:5 reply. rssi is the field worth re-reading; ssid is why the dump
#: drops it.
BASE_INFO = {
    "v": "I4SEASON",
    "p": "90038EAC0-12K-ZAZ",
    "ver": "1.0.29",
    "mcu_ver": "1.0.01",
    "mp": "SC95F8613B-3/US",
    "ssid": "Shannon Family 5G",
    "rssi": -52,
}


#: A real cmd:3 reply, trimmed to the fields these tests touch.
FULL_STATE = {
    "poweron": True,
    "mode": 1,
    "templevel": 67,
    "temperature": 77,
    "tempunit": 1,
    "windlevel": 1,
    "timeron": {"du": 0, "ts": 182},
}


class FakeTransport:
    """Records publishes instead of sending them."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.unresponsive: list[str] = []
        #: Set to a device to have it answer its own cmd:3, the way a reachable unit
        #: does. Left None, every request times out.
        self.answers: ZafroDevice | None = None
        #: Requests to swallow before answering, standing in for QoS 0 losses.
        self.drop_next = 0

    async def publish(self, vendor: str, sn: str, payload: dict[str, Any]) -> None:
        self.published.append(payload)
        if self.drop_next > 0:
            self.drop_next -= 1
            return
        if self.answers is None:
            return
        if payload.get("cmd") == 3:
            self.answers.handle_frame(3, dict(FULL_STATE))
        elif payload.get("cmd") == 5:
            self.answers.handle_frame(5, dict(BASE_INFO))

    def sent(self, cmd: int) -> int:
        """How many frames of one command were published."""
        return sum(1 for payload in self.published if payload.get("cmd") == cmd)

    def register(self, sink: Any) -> None:
        pass

    def note_unresponsive(self, sn: str) -> None:
        self.unresponsive.append(sn)


@pytest.fixture
def device() -> tuple[ZafroDevice, FakeTransport]:
    transport = FakeTransport()
    return ZafroDevice(RAW, transport), transport  # type: ignore[arg-type]


async def test_partial_payloads(device):
    dev, transport = device
    await dev.async_set_power(on=False)
    # Every real end_command is a lone poweron:false. Send only what changed.
    assert transport.published[0]["data"]["state"] == {"poweron": False}


async def test_setpoint_must_match_the_mode(device):
    dev, _ = device
    dev.handle_frame(3, {"mode": 2})  # dry uses rhlevel, never templevel
    with pytest.raises(ZafroUnsupportedError):
        await dev.async_set_target_temperature(68)

    dev.handle_frame(3, {"mode": 3})  # fan uses neither
    with pytest.raises(ZafroUnsupportedError):
        await dev.async_set_target_humidity(50)


async def test_commands_are_never_padded(device):
    dev, transport = device
    dev.handle_frame(3, {"mode": 3})
    # sleep in fan mode is unverified but not forbidden; it is sent alone, not padded.
    await dev.async_set_sleep(on=True)
    assert transport.published[0]["data"]["state"] == {"sleep": True}


async def test_cool_mode_keeps_setpoint(device):
    dev, transport = device
    dev.handle_frame(3, {"mode": 1})
    await dev.async_set_target_temperature(68)
    assert transport.published[0]["data"]["state"] == {"templevel": 68}


async def test_write_is_optimistic_and_does_not_block(device):
    dev, _ = device
    dev.handle_frame(3, {"mode": 1, "templevel": 72})
    await asyncio.wait_for(dev.async_set_target_temperature(68), 0.5)
    # Applied immediately, without waiting for an acknowledgement.
    assert dev.state.target_temperature == 68


async def test_side_effects_are_not_assumed(device):
    dev, _ = device
    dev.handle_frame(3, {"mode": 1, "templevel": 65, "windlevel": 3, "eco": False})
    await dev.async_set_eco(on=True)

    # Only the commanded field is assumed.
    assert dev.state.eco is True
    assert dev.state.target_temperature == 65

    # The device then reports what it actually did, and that wins.
    dev.handle_frame(4, {"templevel": 76, "windlevel": 1, "origin": 0})
    assert dev.state.target_temperature == 76
    assert dev.state.fan_speed == 1


async def test_a_push_clears_the_pending_field(device):
    dev, _ = device
    dev.handle_frame(3, {"mode": 1, "templevel": 72})
    await dev.async_set_target_temperature(68)
    assert dev._pending == {"target_temperature"}

    dev.handle_frame(4, {"templevel": 68, "origin": 1})
    assert dev._pending == set()


async def test_unsupported_feature_raises(device):
    dev, _ = device
    with pytest.raises(ZafroUnsupportedError):
        await dev.async_set_mode(Mode.HEAT)
    with pytest.raises(ZafroUnsupportedError):
        await dev.async_set_target_temperature(200)


async def test_subscribers_see_pushes(device):
    dev, _ = device
    seen: list[int | None] = []
    unsubscribe = dev.subscribe(lambda d: seen.append(d.state.ambient_temperature))

    dev.handle_frame(4, {"temperature": 79})
    assert seen == [79]

    unsubscribe()
    dev.handle_frame(4, {"temperature": 80})
    assert seen == [79]


def test_an_unknown_key_is_logged_once_and_kept(device, caplog):
    """A firmware update adding a field must be visible without flooding the log."""
    dev, _ = device
    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        for _ in range(5):
            dev.handle_frame(4, {"rh": 80, "ionizer": True})

    messages = [r for r in caplog.records if "ionizer" in r.getMessage()]
    assert len(messages) == 1
    assert messages[0].levelno == logging.INFO
    # The unexpected value travels with the bug report.
    assert dev.diagnostics()["anomalies"]["unknown_keys"] == {"ionizer": True}


def test_known_but_unmodelled_keys_stay_quiet(device, caplog):
    """The timeron key is in every frame. Logging it would be pure noise."""
    dev, _ = device
    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        dev.handle_frame(4, {"timeron": {"du": 0, "ts": 182}, "extra": False})

    assert caplog.records == []
    assert dev.diagnostics()["anomalies"]["unknown_keys"] == {}


def test_an_unreadable_value_on_a_known_key_warns(device, caplog):
    """Worse than an unknown key: the field silently keeps a stale value."""
    dev, _ = device
    dev.handle_frame(3, dict(FULL_STATE))
    with caplog.at_level(logging.WARNING, logger="pyzafro.device"):
        for _ in range(3):
            dev.handle_frame(4, {"mode": 99})

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert dev.state.mode is Mode.COOL
    assert dev.diagnostics()["anomalies"]["unreadable_keys"] == {"mode": 99}


def test_a_device_outside_its_capability_table_says_so(device, caplog):
    """The half-supported product case: the guessed table is too narrow."""
    dev, _ = device
    with caplog.at_level(logging.WARNING, logger="pyzafro.device"):
        dev.handle_frame(3, {**FULL_STATE, "windlevel": 7, "templevel": 95})

    warnings = [r.getMessage() for r in caplog.records]
    assert any("fan speed 7" in message for message in warnings)
    assert any("target_temperature=95" in message for message in warnings)
    assert dev.diagnostics()["anomalies"]["outside_capabilities"] == [
        "fan_speed=7",
        "target_temperature=95",
    ]


def test_capability_drift_is_logged_once_per_value(device, caplog):
    dev, _ = device
    with caplog.at_level(logging.WARNING, logger="pyzafro.device"):
        for _ in range(4):
            dev.handle_frame(3, {**FULL_STATE, "windlevel": 7})

    assert len([r for r in caplog.records if "fan speed 7" in r.getMessage()]) == 1


def test_a_new_base_info_field_is_reported_too(device, caplog):
    """A server-side change can add keys to cmd:5 just as easily as to cmd:3."""
    dev, _ = device
    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        dev.handle_frame(5, {"v": "I4SEASON", "p": "X", "ipaddr": "10.0.0.4"})

    assert [r.getMessage() for r in caplog.records if "ipaddr" in r.getMessage()]
    assert dev.diagnostics()["anomalies"]["unknown_keys"] == {"ipaddr": "10.0.0.4"}


def test_an_unsupported_product_gets_no_controls(device, caplog):
    """A device from a class we have never handled must not become a thermostat."""
    dev, _ = device
    dev.model = "SMARTVAC-3000"
    dev.capabilities = caps_module.resolve(dev.model)
    assert dev.capabilities.is_climate  # the fallback guesses air conditioner

    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        dev.handle_frame(3, {"wrong": 0, "worktime": 4, "suction": 2, "dustbin": True})

    assert not dev.capabilities.is_climate
    assert dev.capabilities.modes == frozenset()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "not a supported product" in message
    # Name what it did report, so the issue writes itself.
    assert "dustbin" in message
    assert "suction" in message


def test_an_uncatalogued_air_conditioner_still_works(device, caplog):
    dev, _ = device
    dev.model = "SOMEAC-9000"
    dev.capabilities = caps_module.resolve(dev.model)

    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        dev.handle_frame(3, {**FULL_STATE, "sleep": False, "oscset2": True})

    assert dev.capabilities.is_climate
    assert dev.capabilities.has(Feature.SLEEP)
    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_capabilities_are_refined_once(device, caplog):
    dev, _ = device
    dev.model = "SOMEAC-9000"
    dev.capabilities = caps_module.resolve(dev.model)
    dev.handle_frame(3, {**FULL_STATE, "sleep": False})

    with caplog.at_level(logging.INFO, logger="pyzafro.device"):
        # A later snapshot arriving while the unit is off reports fewer fields. That
        # must not retract a capability already demonstrated.
        dev.handle_frame(3, {"poweron": False})

    assert dev.capabilities.has(Feature.SLEEP)
    assert caplog.records == []


async def test_an_unanswered_resync_reports_the_connection_as_suspect(device):
    """A present device that says nothing is the first sign of a half-open socket.

    The broker can hang up without the client noticing until its next keepalive, and
    every command published in that window is lost. This is the earliest evidence
    available, so it is passed to the transport instead of being swallowed.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)  # marks it available
    assert dev.available

    monkeypatched = 0.01
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", monkeypatched)
        await dev._safe_refresh()

    assert transport.unresponsive == [dev.sn]


async def test_a_device_already_known_gone_is_not_evidence(device):
    """The last-will topic explained the silence; the socket is not implicated."""
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)
    dev.handle_presence(online=False, reason="test")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        await dev._safe_refresh()

    assert transport.unresponsive == []


async def test_a_quiet_but_live_device_stays_available(device):
    """The case that started this: an air conditioner that is off.

    A 103s capture of one had zero cmd:4 pushes and zero `lwt/` beacons, while still
    answering cmd:3 with a full state reply. Nothing volunteered means nothing to
    infer availability from, so it is asked instead.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)
    transport.answers = dev

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        for _ in range(3):
            await dev.async_probe()

    assert dev.available
    assert transport.unresponsive == []


async def test_a_device_heard_from_recently_is_not_probed(device):
    """Its traffic is already the answer the probe would have gone looking for."""
    dev, transport = device
    dev.handle_frame(5, dict(BASE_INFO))  # fresh, so only liveness is in question
    dev.handle_frame(4, {"temperature": 79})

    await dev.async_probe()

    assert transport.published == []


async def test_a_device_that_stops_answering_goes_unavailable(device):
    dev, _ = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        mp.setattr("pyzafro.device.UNANSWERED_GRACE", 0.05)
        await dev.async_probe()
        # Early in the run, and a half-open socket looks exactly like this, so the
        # first miss buys a reconnect rather than an outage.
        assert dev.available

        while dev.available:
            await dev.async_probe()

    assert not dev.available


async def test_silence_shorter_than_the_grace_is_not_an_outage(device):
    """The floor users actually feel: a fault has to last before it is reported.

    An availability change is recorded by the consumer and read by a human later, so
    a device is given UNANSWERED_GRACE of continuous silence before one is written —
    long enough that a bad minute on a cloud connection passes unremarked.
    """
    dev, _ = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        mp.setattr("pyzafro.device.UNANSWERED_GRACE", 30.0)
        for _ in range(20):
            await dev.async_probe()

    assert dev.available
    assert dev._probe_misses == 20


async def test_the_run_is_timed_from_the_request_not_from_noticing(device):
    """Otherwise the grace silently becomes longer than it says it is.

    A miss is only recorded once the request has timed out, so timing the run from
    there would discard REQUEST_TIMEOUT of real silence on every probe.
    """
    dev, _ = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.05)
        mp.setattr("pyzafro.device.UNANSWERED_GRACE", 1000.0)
        before = time.monotonic()
        await dev.async_probe()

    # The run began when the first request went out, not when it gave up on it.
    assert dev._unanswered_since is not None
    assert dev._unanswered_since <= before + 0.01


async def test_only_the_first_miss_blames_the_socket(device):
    """An absent device must not tear the connection down on every probe.

    That loop is how this used to end badly: each reconnect re-baselined, timed out,
    and reconnected again with a longer backoff, until the backoff outgrew
    OFFLINE_GRACE and the entities went unavailable for good.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        for _ in range(5):
            await dev.async_probe()

    assert transport.unresponsive == [dev.sn]


async def test_an_unavailable_device_is_still_probed_and_can_return(device):
    """The regression the whole mechanism exists for.

    Availability only ever came back on an inbound frame, and an unavailable device
    was asked for nothing — so an idle unit that had been marked gone had no way back
    short of reloading the integration, which is exactly what users had to do.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)
    seen: list[bool] = []
    dev.subscribe(lambda d: seen.append(d.available))
    dev.handle_presence(online=False, reason="test")
    assert not dev.available

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        await dev.async_probe()
        assert not dev.available

        transport.answers = dev  # the unit comes back
        await dev.async_probe()

    assert dev.available
    assert seen[-1] is True
    # A device already known gone never implicates the socket on its way back.
    assert transport.unresponsive == []


async def test_a_frame_we_cannot_read_still_counts_as_present(device):
    """Availability is about whether the device is there, not whether we parsed it."""
    dev, _ = device
    dev.handle_presence(online=False, reason="test")

    dev.handle_frame(4, {"ionizer": True})

    assert dev.available


async def test_one_lost_request_costs_nothing(device):
    """Publishes are QoS 0, so a request the device never sees is routine.

    Treating a single one as evidence would reconnect the whole account's socket on
    an ordinary event, and two in a row would flap the entities — which a consumer
    records, making it more expensive for a user than a reading a minute stale.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)
    dev.handle_frame(5, dict(BASE_INFO))
    transport.answers = dev
    transport.drop_next = 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        await dev.async_probe()

    assert dev.available
    assert transport.unresponsive == []
    assert dev._probe_misses == 0
    # It asked again rather than concluding anything from the first silence.
    assert transport.sent(3) == 2


def test_every_availability_change_names_its_cause(device, caplog):
    """A debug log covering the failure has to say what happened.

    The event users report is "the entities went unavailable", and that was the one
    event this library wrote nothing about — so the logs you would ask for could
    cover the whole outage and still not answer the question.
    """
    dev, _ = device
    with caplog.at_level(logging.DEBUG, logger="pyzafro.device"):
        dev.handle_frame(3, FULL_STATE)
        dev.handle_presence(online=False, reason="last-will topic")
        dev.handle_frame(4, {"temperature": 79})

    changes = [r.getMessage() for r in caplog.records if " is now " in r.getMessage()]
    assert len(changes) == 3
    assert "available (cmd:3)" in changes[0]
    assert "unavailable (last-will topic)" in changes[1]
    assert "available (cmd:4)" in changes[2]


def test_an_unchanged_availability_is_not_logged(device, caplog):
    """Beacons arrive repeatedly; only transitions are worth a line."""
    dev, _ = device
    dev.handle_presence(online=True, reason="last-will topic")

    with caplog.at_level(logging.DEBUG, logger="pyzafro.device"):
        for _ in range(5):
            dev.handle_presence(online=True, reason="last-will topic")

    assert [r for r in caplog.records if " is now " in r.getMessage()] == []


async def test_the_outage_reason_carries_how_long_it_lasted(device, caplog):
    """An outage and an outage that lasted 90s are different reports."""
    dev, _ = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        mp.setattr("pyzafro.device.UNANSWERED_GRACE", 0.0)
        with caplog.at_level(logging.DEBUG, logger="pyzafro.device"):
            await dev.async_probe()

    assert any("no answer for" in r.getMessage() for r in caplog.records)


async def test_signal_strength_is_re_read_while_the_device_is_reachable(device):
    """The rssi field was read once at setup and never again.

    A dump from a unit that had been up for weeks reported the signal it had when
    the integration last loaded — which is the first number you would look at for a
    device that keeps dropping off its network, and it was silently ancient.
    """
    dev, transport = device
    transport.answers = dev
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.BASE_INFO_INTERVAL", 0.0)
        await dev.async_probe()
        await dev.async_probe()

    assert transport.sent(5) == 2
    assert dev.base_info is not None
    assert dev.base_info.rssi == -52


async def test_base_info_is_left_alone_between_refreshes(device):
    """It is on a much longer clock than the liveness probe, not every round."""
    dev, transport = device
    transport.answers = dev
    dev.handle_frame(3, FULL_STATE)
    dev.handle_frame(5, dict(BASE_INFO))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.BASE_INFO_INTERVAL", 1000.0)
        for _ in range(5):
            await dev.async_probe()

    assert transport.sent(5) == 0
    assert transport.sent(3) == 5


async def test_a_device_that_is_not_answering_is_not_asked_twice(device):
    """A second request it cannot answer costs a REQUEST_TIMEOUT and tells us nothing.

    The cmd:3 probe already establishes whether the device is there; base info is
    housekeeping and waits until it is.
    """
    dev, transport = device
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.BASE_INFO_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        await dev.async_probe()
        await dev.async_probe()

    assert transport.sent(5) == 0


async def test_a_lost_base_info_reply_is_not_held_against_the_device(device):
    """Housekeeping must not be able to mark a working device unavailable."""
    dev, transport = device
    transport.answers = dev
    dev.handle_frame(3, FULL_STATE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pyzafro.device.PROBE_INTERVAL", 1000.0)
        mp.setattr("pyzafro.device.BASE_INFO_INTERVAL", 0.0)
        mp.setattr("pyzafro.device.REQUEST_TIMEOUT", 0.01)
        mp.setattr("pyzafro.device.UNANSWERED_GRACE", 0.0)
        transport.drop_next = 10
        await dev.async_probe()

    assert dev.available
    assert dev._probe_misses == 0
    assert transport.unresponsive == []


def test_the_dump_says_how_old_the_signal_reading_is(device):
    dev, _ = device
    assert dev.diagnostics()["base_info_age"] is None

    dev.handle_frame(5, dict(BASE_INFO))

    assert dev.diagnostics()["base_info_age"] == pytest.approx(0.0, abs=1.0)
    # And still never carries the network name.
    assert "Shannon Family 5G" not in json.dumps(dev.diagnostics())
