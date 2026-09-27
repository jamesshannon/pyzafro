"""Device state, base info, and wire translation.

Wire field names appear in this module and nowhere else. Everything above it speaks in
the names defined here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Final


class Mode(IntEnum):
    """Operating mode.

    Confirmed for air conditioners from ScheduleModel's label switch. On a tower fan the
    same field is a wind profile in the same positional order (unconfirmed).
    """

    COOL = 1
    DRY = 2
    FAN = 3
    HEAT = 4


class TemperatureUnit(IntEnum):
    """Unit the device reports temperatures in.

    Values on the wire are expressed in this unit; they are not normalised. CELSIUS is
    inferred from FAHRENHEIT being 1 and has never been observed.
    """

    CELSIUS = 0
    FAHRENHEIT = 1


class Origin(IntEnum):
    """Who caused a reported change."""

    #: Device-originated: ambient readings, and side effects it applied itself.
    DEVICE = 0
    #: Acknowledgement of a command, from this client or the phone app.
    COMMANDED = 1


@dataclass(frozen=True, slots=True)
class DeviceState:
    """A device's reported state.

    Every field is optional because cmd:4 frames are deltas. A field is None only until
    the first frame that mentions it.
    """

    power: bool | None = None
    mode: Mode | None = None
    target_temperature: int | None = None
    ambient_temperature: int | None = None
    target_humidity: int | None = None
    ambient_humidity: int | None = None
    fan_speed: int | None = None
    temperature_unit: TemperatureUnit | None = None
    swing_horizontal: bool | None = None
    swing_vertical: bool | None = None
    sleep: bool | None = None
    eco: bool | None = None
    #: EXTRA, the fan's fifth position rather than a sixth `fan_speed` — the name the
    #: unit's display and the app both use; the app's code calls it `turbo`. It is
    #: reported alongside whatever speed the device decided to run it at, so it has to
    #: be read before `fan_speed` to know what the fan is set to.
    extra: bool | None = None
    mute: bool | None = None
    display: bool | None = None
    child_lock: bool | None = None
    water_level: int | None = None
    filter_hours: int | None = None
    work_time: int | None = None
    reached_target: bool | None = None
    fault_code: int | None = None
    origin: Origin | None = None

    def merged(self, updates: dict[str, Any]) -> DeviceState:
        """Return a copy with `updates` applied.

        Always merge. A cmd:4 carrying only {"rh": 88, "origin": 0} must not blank the
        setpoint.
        """
        return replace(self, **updates)


@dataclass(frozen=True, slots=True)
class ParsedState:
    """The outcome of reading one wire frame."""

    #: DeviceState field name -> value, ready to merge.
    updates: dict[str, Any]
    #: Wire keys this library has never seen, with the values reported.
    unknown: dict[str, Any]
    #: Wire keys it models, carrying values it could not read.
    rejected: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BaseInfo:
    """Reply to cmd:5. Everything except ssid/rssi duplicates /device/list."""

    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    mcu_version: str | None = None
    mcu_type: str | None = None
    ssid: str | None = None
    rssi: int | None = None


# --- wire translation ----------------------------------------------------------------

#: wire key -> DeviceState field. The reverse of this drives command payloads.
_WIRE_TO_FIELD: Final[dict[str, str]] = {
    "poweron": "power",
    "mode": "mode",
    "templevel": "target_temperature",
    "temperature": "ambient_temperature",
    "rhlevel": "target_humidity",
    "rh": "ambient_humidity",
    "windlevel": "fan_speed",
    "tempunit": "temperature_unit",
    # Which osc field drives which axis was a guess until someone watched the louvres:
    # oscset1 is the up-and-down one.
    "oscset1": "swing_vertical",
    "oscset2": "swing_horizontal",
    "sleep": "sleep",
    "eco": "eco",
    # EXTRA, reached by holding the remote's fan button. The device answers with
    # {"windlevel": 3, "extra": true} — a speed comes with it, so this cannot be read
    # off windlevel alone.
    "extra": "extra",
    "muteon": "mute",
    "lighton": "display",
    "childlockon": "child_lock",
    "waterlevel": "water_level",
    "filterthr": "filter_hours",
    "worktime": "work_time",
    "reachtarget": "reached_target",
    "wrong": "fault_code",
    "origin": "origin",
}

FIELD_TO_WIRE: Final[dict[str, str]] = {v: k for k, v in _WIRE_TO_FIELD.items()}

#: The order control-frame keys are emitted in, and it is part of the protocol.
#:
#: The device applies a frame's keys in the order they appear, and some writes make it
#: recalculate other fields: clearing `sleep` restores the speed the fan had before
#: sleep, setting `extra` forces speed 3 and the lowest setpoint, setting `eco` forces
#: speed 1 and a setpoint of 76. So a frame that both clears sleep and names a speed
#: gets two different outcomes depending on key order, which is not something a JSON
#: object ought to mean. Measured against a 90038EAC0-12K-ZAZ in cool mode, entering
#: sleep from speed 2 and then asking for speed 3:
#:
#:     {"windlevel": 3, "sleep": false, ...}  -> device pushed windlevel 2. Lost.
#:     {..., "sleep": false, "windlevel": 3}  -> no correction pushed. Kept.
#:
#: Hence the rule this order encodes: whatever the device derives from another field
#: comes after the field it is derived from, so the later write wins and the caller's
#: actual request is what survives. Most global first (power, then mode), then the
#: programmes that move the fan and the setpoint, then the values they move.
#:
#: `sleep` precedes `extra` because only sleep's *clear* recalculates anything —
#: leaving EXTRA was observed to push no speed at all — so this one order is right for
#: both `{sleep: false, extra: true}` and `{sleep: true, extra: false}`.
#:
#: The app orders its one multi-key frame `{eco, extra, sleep, windlevel}`, which is
#: also correct; it just never sends the two frames that need sleep before extra.
_WIRE_ORDER: Final[tuple[str, ...]] = (
    "poweron",
    "mode",
    "eco",
    "sleep",
    "extra",
    "windlevel",
    "templevel",
    "rhlevel",
    "oscset1",
    "oscset2",
    "muteon",
    "lighton",
    "childlockon",
)

#: The fan speed sleep mode selects. Reported, never commanded: it is not one of the
#: positions the remote's fan button cycles, and the app never sends it. Asking for it
#: directly was acknowledged and then undone — a unit running at speed 2 was back at
#: speed 2 five and a half seconds later — so sleep is the only way to this speed.
SLEEP_FAN_SPEED: Final = 0

#: Wire keys some device classes report that this library deliberately does not model
#: yet. Listed so that "we know about this and skipped it" can be told apart from "we
#: have never seen this key before", which is the signal worth logging.
UNMODELLED_WIRE_KEYS: Final = frozenset(
    {
        "timeron",
        "timeroff",
        "oscset",
        "oscangle",
        "auto",
        "humilevel",
        "lightmode",
        "drymode",
        "schedset",
        "brightness",
    }
)

#: Fields the device reports but never accepts as a command.
READ_ONLY_FIELDS: Final = frozenset(
    {
        "ambient_temperature",
        "ambient_humidity",
        "temperature_unit",
        "water_level",
        "filter_hours",
        "work_time",
        "reached_target",
        "fault_code",
        "origin",
    }
)

_BOOL_FIELDS: Final = frozenset(
    {
        "power",
        "swing_horizontal",
        "swing_vertical",
        "sleep",
        "eco",
        "extra",
        "mute",
        "display",
        "child_lock",
        "reached_target",
    }
)

_INT_FIELDS: Final = frozenset(
    {
        "target_temperature",
        "ambient_temperature",
        "target_humidity",
        "ambient_humidity",
        "fan_speed",
        "water_level",
        "filter_hours",
        "work_time",
        "fault_code",
    }
)


def _coerce_bool(value: Any) -> bool | None:
    """Accept real booleans and the 0/1 ints the app's stored commands use."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in {"1", "true"}
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _coerce_field(field: str, value: Any) -> Any:
    """Read one wire value into its modelled type, or None if it cannot be read."""
    if field in _BOOL_FIELDS:
        return _coerce_bool(value)
    if field in _INT_FIELDS:
        return _coerce_int(value)
    if field == "mode":
        return _as_enum(Mode, value)
    if field == "temperature_unit":
        return _as_enum(TemperatureUnit, value)
    if field == "origin":
        return _as_enum(Origin, value)
    return None  # pragma: no cover - every field is covered above


def parse_state(raw: dict[str, Any]) -> ParsedState:
    """Translate a wire `result` object into DeviceState field updates.

    Nothing raises. A frame carrying something unexpected still yields every field
    that *was* understood, because a device reporting one new key must not stop
    reporting its temperature. What could not be used is returned alongside so the
    caller can say so out loud — see `ZafroDevice._log_anomalies`.
    """
    updates: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    rejected: dict[str, Any] = {}

    for wire_key, value in raw.items():
        field = _WIRE_TO_FIELD.get(wire_key)
        if field is None:
            if wire_key not in UNMODELLED_WIRE_KEYS:
                unknown[wire_key] = value
            continue
        if value is None:
            continue
        coerced = _coerce_field(field, value)
        if coerced is None:
            # A key we claim to support, carrying a value we cannot read. Worse than
            # an unknown key: the field keeps its previous value and Home Assistant
            # shows something stale without knowing it.
            rejected[wire_key] = value
        else:
            updates[field] = coerced

    return ParsedState(updates=updates, unknown=unknown, rejected=rejected)


def _as_enum[T: IntEnum](enum_cls: type[T], value: Any) -> T | None:
    """Convert to an enum member, tolerating values this library has not seen."""
    number = _coerce_int(value)
    if number is None:
        return None
    try:
        return enum_cls(number)
    except ValueError:
        return None


def build_command(fields: dict[str, Any]) -> dict[str, Any]:
    """Translate DeviceState field names back into a wire `state` object.

    Emitted in `_WIRE_ORDER`, never in the order the caller happened to build its dict,
    because the device reads the keys in order and lets a later one override what an
    earlier one made it recalculate. Ordering here rather than at each setter is what
    stops a caller getting it wrong: there is one place a control frame is built, so
    there is one place the order has to be right.

    A key the order does not list is emitted after the ones it does, keeping the
    caller's own order among them. That only happens for a field added to the model
    without a thought about where it belongs, and last is where a new field is least
    likely to be silently overridden.
    """
    unordered: dict[str, Any] = {}
    for field, value in fields.items():
        wire_key = FIELD_TO_WIRE.get(field)
        if wire_key is None:
            msg = f"unknown field {field!r}"
            raise KeyError(msg)
        unordered[wire_key] = int(value) if isinstance(value, IntEnum) else value

    payload = {key: unordered.pop(key) for key in _WIRE_ORDER if key in unordered}
    payload.update(unordered)
    return payload


#: Every key a cmd:5 reply is known to carry. `sn` is echoed back and ignored.
BASE_INFO_WIRE_KEYS: Final = frozenset(
    {"v", "p", "ver", "mcu_ver", "mp", "ssid", "rssi", "sn"}
)


def parse_base_info(raw: dict[str, Any]) -> BaseInfo:
    """Translate a cmd:5 reply."""
    return BaseInfo(
        vendor=raw.get("v"),
        model=raw.get("p"),
        firmware=raw.get("ver"),
        mcu_version=raw.get("mcu_ver"),
        mcu_type=raw.get("mp"),
        ssid=raw.get("ssid"),
        rssi=_coerce_int(raw.get("rssi")),
    )


def flatten_device_list(data: Any) -> list[dict[str, Any]]:
    """Flatten /device/list, which groups by room rather than returning an array.

    The room name and id are pushed down onto each device so the caller can build
    areas without a second pass.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "devices" in entry:
            for device in entry.get("devices") or []:
                if not isinstance(device, dict):
                    continue
                merged = dict(device)
                merged.setdefault("room", entry.get("room"))
                merged.setdefault("room_id", entry.get("room_id"))
                out.append(merged)
        elif "sn" in entry:
            out.append(dict(entry))
    return out
