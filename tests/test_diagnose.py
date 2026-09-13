"""Redaction and discovery analysis. Both are easy to regress silently."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pyzafro.device import ZafroDevice
from pyzafro.diagnose import Recorder, _parse_assignment

RAW = {
    "sn": "6ISEComboWF020BSJ0000000000",
    "vendor": "I4SEASON",
    "model": "90038EAC0-12K-ZAZ",
    "name": "Jim's Bedroom AC",
    "mac": "001cc2000000",
    "room": "Jim's Bedroom",
    "room_id": 17285,
    "version": "1.0.29",
}

SECRETS = ["6ISEComboWF020BSJ0000000000", "001cc2000000", "Jim", "homewifi"]


class FakeTransport:
    async def publish(self, vendor: str, sn: str, payload: dict[str, Any]) -> None:
        pass

    def register(self, sink: Any) -> None:
        pass


@pytest.fixture
def device() -> ZafroDevice:
    return ZafroDevice(RAW, FakeTransport())  # type: ignore[arg-type]


def test_diagnostics_leaks_nothing_identifying(device):
    device.handle_frame(
        5, {"v": "I4SEASON", "p": "90038EAC0-12K-ZAZ", "ssid": "homewifi", "rssi": 44}
    )
    dump = json.dumps(device.diagnostics())

    for secret in SECRETS:
        assert secret not in dump, f"{secret!r} leaked into the report"

    # Still useful: the model and signal survive.
    assert "90038EAC0-12K-ZAZ" in dump
    assert "44" in dump


def test_anon_id_is_stable_and_not_the_serial(device):
    assert device.anon_id == ZafroDevice(RAW, FakeTransport()).anon_id
    assert device.anon_id not in RAW["sn"]
    assert len(device.anon_id) == 8


def test_recorder_surfaces_unmodelled_wire_keys(device):
    recorder = Recorder(device)
    # A hypothetical new product reporting something we have never seen.
    device.handle_frame(4, {"rh": 88, "pm25": 12, "ionizer": True})
    device.handle_frame(4, {"pm25": 15})

    unknown = recorder.unknown_keys()
    assert set(unknown) == {"pm25", "ionizer"}
    assert unknown["pm25"] == [12, 15]
    assert "rh" not in unknown


def test_recorder_flags_keys_we_know_we_have_not_modelled(device):
    recorder = Recorder(device)
    device.handle_frame(4, {"timeron": {"du": 0, "ts": 182}, "waterlevel": 0})
    assert recorder.summary()["unmodelled_but_expected"] == ["timeron"]


def test_recorder_collects_value_ranges(device):
    recorder = Recorder(device)
    for level in (0, 1, 4, 1):
        device.handle_frame(4, {"windlevel": level})

    ranges = recorder.observed_ranges()
    assert ranges["fan_speed"]["count"] == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("oscset1=true", ("oscset1", True)),
        ("windlevel=0", ("windlevel", 0)),
        ("mode=2", ("mode", 2)),
        ("label=something", ("label", "something")),
    ],
)
def test_assignment_parsing(text, expected):
    assert _parse_assignment(text) == expected
