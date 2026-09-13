"""Write path: mode-dependent payloads and optimistic reconciliation."""

from __future__ import annotations

import asyncio
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
