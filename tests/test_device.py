"""Write path: mode-dependent payloads and optimistic reconciliation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

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

    async def publish(self, vendor: str, sn: str, payload: dict[str, Any]) -> None:
        self.published.append(payload)

    def register(self, sink: Any) -> None:
        pass


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
