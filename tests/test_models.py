"""Wire translation tests, built from real captured frames."""

from __future__ import annotations

from pyzafro.models import (
    DeviceState,
    Mode,
    Origin,
    TemperatureUnit,
    build_command,
    flatten_device_list,
    parse_base_info,
    parse_state,
)

# A real cmd:3 reply from a 90038EAC0-12K-ZAZ window unit.
FULL_STATE = {
    "poweron": True,
    "mode": 1,
    "templevel": 67,
    "temperature": 77,
    "tempunit": 1,
    "rh": 90,
    "rhlevel": 50,
    "windlevel": 1,
    "worktime": 2,
    "filterthr": 600,
    "waterlevel": 0,
    "reachtarget": 0,
    "wrong": 0,
    "origin": 0,
    "childlockon": False,
    "eco": False,
    "extra": False,
    "lighton": True,
    "muteon": False,
    "oscset1": False,
    "oscset2": False,
    "sleep": False,
    "timeron": {"du": 0, "ts": 182},
    "timeroff": {"du": 0, "ts": 182},
}


def test_parses_a_full_state_frame():
    state = DeviceState().merged(parse_state(FULL_STATE).updates)

    assert state.power is True
    assert state.mode is Mode.COOL
    # templevel is the target and temperature is ambient; easy to transpose.
    assert state.target_temperature == 67
    assert state.ambient_temperature == 77
    assert state.target_humidity == 50
    assert state.ambient_humidity == 90
    assert state.temperature_unit is TemperatureUnit.FAHRENHEIT
    assert state.origin is Origin.DEVICE
    assert state.reached_target is False


def test_cmd4_deltas_merge_rather_than_replace():
    state = DeviceState().merged(parse_state(FULL_STATE).updates)
    # A real ambient-only push. It must not blank the setpoint.
    state = state.merged(
        parse_state({"rh": 88, "temperature": 79, "origin": 0}).updates
    )

    assert state.ambient_humidity == 88
    assert state.ambient_temperature == 79
    assert state.target_temperature == 67
    assert state.mode is Mode.COOL


def test_unknown_keys_are_reported_rather_than_silently_dropped():
    # timeron is known-but-unmodelled, so it is not news. somethingnew is.
    parsed = parse_state({"timeron": {"du": 0, "ts": 182}, "somethingnew": 5})

    assert parsed.updates == {}
    assert parsed.unknown == {"somethingnew": 5}
    assert parsed.rejected == {}


def test_booleans_arrive_as_ints_too():
    # poweron is a real bool in replies but an int inside stored schedule commands.
    assert parse_state({"poweron": 1}).updates["power"] is True
    assert parse_state({"poweron": 0}).updates["power"] is False


def test_unrecognised_enum_values_do_not_raise_but_are_flagged():
    # A device class we have not seen may use a mode integer outside 1-4. Dropping it
    # leaves the previous mode showing, so it has to be reported.
    parsed = parse_state({"mode": 99})

    assert parsed.updates == {}
    assert parsed.rejected == {"mode": 99}


def test_a_bad_value_does_not_cost_the_rest_of_the_frame():
    parsed = parse_state({"mode": 99, "temperature": 77, "brandnew": "x"})

    assert parsed.updates == {"ambient_temperature": 77}
    assert parsed.rejected == {"mode": 99}
    assert parsed.unknown == {"brandnew": "x"}


def test_the_swing_axes_are_not_transposed():
    """oscset1 is vertical, oscset2 horizontal. Shipped the other way round once.

    Nothing in the protocol names the axes, so this is only knowable by watching the
    louvres, and nothing downstream can tell it is wrong. Pinned in both directions.
    """
    state = DeviceState().merged(
        parse_state({**FULL_STATE, "oscset1": True, "oscset2": False}).updates
    )
    assert state.swing_vertical is True
    assert state.swing_horizontal is False

    assert build_command({"swing_vertical": True}) == {"oscset1": True}
    assert build_command({"swing_horizontal": True}) == {"oscset2": True}


def test_build_command_uses_wire_names():
    assert build_command({"power": False}) == {"poweron": False}
    assert build_command({"mode": Mode.DRY, "target_humidity": 45}) == {
        "mode": 2,
        "rhlevel": 45,
    }


def test_parse_base_info():
    info = parse_base_info(
        {
            "v": "I4SEASON",
            "p": "90038EAC0-12K-ZAZ",
            "ver": "1.0.29",
            "mcu_ver": "1.0.01",
            "mp": "SC95F8613B-3/US",
            "ssid": "wifi",
            "rssi": 44,
        }
    )
    assert info.model == "90038EAC0-12K-ZAZ"
    assert info.firmware == "1.0.29"
    assert info.rssi == 44


def test_device_list_is_grouped_by_room():
    flat = flatten_device_list(
        [
            {
                "room": "Living Room",
                "room_id": 17285,
                "devices": [{"sn": "ABC", "vendor": "I4SEASON"}],
            }
        ]
    )
    assert len(flat) == 1
    assert flat[0]["sn"] == "ABC"
    assert flat[0]["room"] == "Living Room"
    assert flat[0]["room_id"] == 17285


def test_device_list_tolerates_a_flat_array():
    assert flatten_device_list([{"sn": "ABC", "vendor": "I4SEASON"}])[0]["sn"] == "ABC"
