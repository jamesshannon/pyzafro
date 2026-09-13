"""Per-model capability table.

Adding support for a new product should be a change to this file alone. Capabilities are
expressed as sets of enum members so that a new *combination* costs nothing; only a
genuinely new feature requires a consumer-side change (for Home Assistant, an entity
description and a translation).

The feature vocabulary mirrors the app's own IOTDeviceFunction enum, which is the axis
the manufacturer gates models on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from .models import Mode

if TYPE_CHECKING:
    from collections.abc import Collection

_LOGGER = logging.getLogger(__name__)


class Feature(StrEnum):
    """A controllable capability."""

    SLEEP = "sleep"
    ECO = "eco"
    TURBO = "turbo"
    SWING_HORIZONTAL = "swing_horizontal"
    SWING_VERTICAL = "swing_vertical"
    FAN_SPEED = "fan_speed"
    CHILD_LOCK = "child_lock"
    DISPLAY = "display"
    MUTE = "mute"


class SensorKey(StrEnum):
    """A numeric reading a device reports."""

    AMBIENT_TEMPERATURE = "ambient_temperature"
    AMBIENT_HUMIDITY = "ambient_humidity"
    RSSI = "rssi"
    WORK_TIME = "work_time"
    FILTER_HOURS = "filter_hours"
    WATER_LEVEL = "water_level"
    FAULT_CODE = "fault_code"


class SwitchKey(StrEnum):
    """A boolean a device accepts as a command."""

    SLEEP = "sleep"
    ECO = "eco"
    CHILD_LOCK = "child_lock"
    DISPLAY = "display"
    MUTE = "mute"


class BinarySensorKey(StrEnum):
    """A boolean a device reports but does not accept."""

    PROBLEM = "problem"
    REACHED_TARGET = "reached_target"


#: DeviceState fields that prove a capability when a device reports them. Used only to
#: narrow an unknown model down to what it demonstrably does — never to widen a model
#: the table already describes.
_FIELD_FEATURES: Final[dict[str, Feature]] = {
    "fan_speed": Feature.FAN_SPEED,
    "swing_horizontal": Feature.SWING_HORIZONTAL,
    "swing_vertical": Feature.SWING_VERTICAL,
    "sleep": Feature.SLEEP,
    "eco": Feature.ECO,
    "child_lock": Feature.CHILD_LOCK,
    "display": Feature.DISPLAY,
    "mute": Feature.MUTE,
}

_FIELD_SWITCHES: Final[dict[str, SwitchKey]] = {
    "sleep": SwitchKey.SLEEP,
    "eco": SwitchKey.ECO,
    "child_lock": SwitchKey.CHILD_LOCK,
    "display": SwitchKey.DISPLAY,
    "mute": SwitchKey.MUTE,
}

_FIELD_BINARY_SENSORS: Final[dict[str, BinarySensorKey]] = {
    "fault_code": BinarySensorKey.PROBLEM,
    "reached_target": BinarySensorKey.REACHED_TARGET,
}

#: Sensors named after the state field they read. RSSI is absent because it comes from
#: base info rather than from a state frame.
_FIELD_SENSORS: Final[dict[str, SensorKey]] = {
    "ambient_temperature": SensorKey.AMBIENT_TEMPERATURE,
    "ambient_humidity": SensorKey.AMBIENT_HUMIDITY,
    "work_time": SensorKey.WORK_TIME,
    "filter_hours": SensorKey.FILTER_HOURS,
    "water_level": SensorKey.WATER_LEVEL,
    "fault_code": SensorKey.FAULT_CODE,
}


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a device can do. Consumers branch on this, never on the model string."""

    modes: frozenset[Mode]
    fan_speeds: tuple[int, ...] = ()
    target_temperature_range: tuple[int, int] | None = None
    target_humidity_range: tuple[int, int] | None = None
    features: frozenset[Feature] = field(default_factory=frozenset)
    sensors: frozenset[SensorKey] = field(default_factory=frozenset)
    switches: frozenset[SwitchKey] = field(default_factory=frozenset)
    binary_sensors: frozenset[BinarySensorKey] = field(default_factory=frozenset)
    #: False when the model was not in the table and defaults were used.
    known_model: bool = True

    @property
    def is_climate(self) -> bool:
        """Whether this device is a thermostat-like thing at all.

        False for a product that reports no mode, no setpoint and no ambient
        temperature — a vacuum, say. A consumer must not build a climate entity for
        one of those.
        """
        return bool(self.modes) or self.target_temperature_range is not None

    def has(self, feature: Feature) -> bool:
        """Return whether this device supports `feature`."""
        return feature in self.features

    def refined(self, observed: Collection[str]) -> Capabilities:
        """Narrow these capabilities to the DeviceState fields actually reported.

        Only ever called for a model the table does not describe, and only ever
        subtracts — the fallback is a guess, and a guess contradicted by evidence
        should lose. Two things fall out of that:

        A product from a class we have never handled reports none of the climate
        fields, so it ends up claiming nothing, and the consumer builds no climate
        entity rather than putting a thermostat dial on a vacuum.

        An air conditioner we simply have not catalogued keeps everything it
        demonstrated, which is usually *more* than the fallback claims: the fallback
        offers no sleep or swing, but a unit reporting those fields gets them.
        """
        seen = set(observed)
        return Capabilities(
            modes=self.modes if "mode" in seen else frozenset(),
            fan_speeds=self.fan_speeds if "fan_speed" in seen else (),
            target_temperature_range=(
                self.target_temperature_range if "target_temperature" in seen else None
            ),
            target_humidity_range=(
                self.target_humidity_range if "target_humidity" in seen else None
            ),
            features=frozenset(
                feature for field, feature in _FIELD_FEATURES.items() if field in seen
            ),
            sensors=frozenset(
                {SensorKey.RSSI}
                | {sensor for field, sensor in _FIELD_SENSORS.items() if field in seen}
            ),
            switches=frozenset(
                switch for field, switch in _FIELD_SWITCHES.items() if field in seen
            ),
            binary_sensors=frozenset(
                sensor
                for field, sensor in _FIELD_BINARY_SENSORS.items()
                if field in seen
            ),
            known_model=False,
        )


# --- model normalisation -------------------------------------------------------------

#: Vendor aliases, from the app's converModel. Both sides appear in the wild; the right
#: hand side is what the capability table is keyed on.
MODEL_ALIASES: Final[dict[str, str]] = {
    "A90045-10K": "90045EAC0-10K-ZAZ",
    "A9045-10K": "90045EAC0-10K-ZAZ",
    "A90045-8K": "90045EAC0-8K-ZAZ",
    "A9045-8K": "90045EAC0-8K-ZAZ",
    "A90038-12K": "90038EAC0-12K-ZAZ",
    "A9038-12K": "90038EAC0-12K-ZAZ",
    "W54091D-8K": "54091EWA1-8K-ZAZ",
    "W54091S-8K": "54091EWA0-8K-ZAZ",
    "W54091S-6K": "54091EWA0-6K-ZAZ",
    "D026W": "D026W-50Pt3M",
}


def normalise_model(model: str) -> str:
    """Apply the app's vendor alias table and normalise case/whitespace."""
    cleaned = model.strip().upper()
    return MODEL_ALIASES.get(cleaned, cleaned)


# --- the table -----------------------------------------------------------------------

_WINDOW_AC_SENSORS: Final = frozenset(
    {
        SensorKey.AMBIENT_TEMPERATURE,
        SensorKey.AMBIENT_HUMIDITY,
        SensorKey.RSSI,
        SensorKey.WORK_TIME,
        SensorKey.FILTER_HOURS,
        SensorKey.WATER_LEVEL,
        SensorKey.FAULT_CODE,
    }
)

#: Window air conditioner, cooling only.
#:
#: Confirmed live against 90038EAC0-12K-ZAZ: modes 1/2/3 (4 never appeared), fan speeds
#: 0-4 where 0 is the silent speed sleep mode selects, whole-degree setpoints, sleep and
#: eco independently
#: settable.
#:
#: The two ranges are NOT confirmed. Observed setpoints span 65-76F and the only
#: humidity target ever seen is 50. They are set to the conventional range for a
#: US window unit and should be tightened when someone hits a limit.
_WINDOW_AC = Capabilities(
    modes=frozenset({Mode.COOL, Mode.DRY, Mode.FAN}),
    fan_speeds=(0, 1, 2, 3, 4),
    target_temperature_range=(60, 86),
    target_humidity_range=(30, 80),
    features=frozenset(
        {
            Feature.SLEEP,
            Feature.ECO,
            Feature.SWING_HORIZONTAL,
            Feature.SWING_VERTICAL,
            Feature.FAN_SPEED,
            Feature.CHILD_LOCK,
            Feature.DISPLAY,
            Feature.MUTE,
        }
    ),
    sensors=_WINDOW_AC_SENSORS,
    switches=frozenset(
        {
            SwitchKey.SLEEP,
            SwitchKey.ECO,
            SwitchKey.CHILD_LOCK,
            SwitchKey.DISPLAY,
            SwitchKey.MUTE,
        }
    ),
    binary_sensors=frozenset({BinarySensorKey.PROBLEM, BinarySensorKey.REACHED_TARGET}),
)

MODELS: Final[dict[str, Capabilities]] = {
    "90038EAC0-12K-ZAZ": _WINDOW_AC,
}

#: Families we can guess at from the model prefix when the exact model is unknown.
#: Derived from the app's IOTDeviceModelType groupings.
_FAMILY_PATTERNS: Final[tuple[tuple[re.Pattern[str], Capabilities], ...]] = (
    (re.compile(r"^9004[58]EAC0|^90038EAC0"), _WINDOW_AC),
)

#: Last resort. Enough for a usable climate entity: power, mode, setpoint, ambient
#: readings. Deliberately claims no optional feature, so nothing is offered that the
#: device might reject.
FALLBACK = Capabilities(
    modes=frozenset({Mode.COOL, Mode.DRY, Mode.FAN}),
    fan_speeds=(1, 2, 3, 4),
    target_temperature_range=(60, 86),
    features=frozenset({Feature.FAN_SPEED}),
    sensors=frozenset(
        {
            SensorKey.AMBIENT_TEMPERATURE,
            SensorKey.AMBIENT_HUMIDITY,
            SensorKey.RSSI,
            SensorKey.FAULT_CODE,
        }
    ),
    binary_sensors=frozenset({BinarySensorKey.PROBLEM}),
    known_model=False,
)


def resolve(model: str) -> Capabilities:
    """Look up a model's capabilities, degrading rather than raising.

    An unknown model must still produce a working device. When that happens the model
    string is logged once so the user can file it along with a diagnostics dump.
    """
    normalised = normalise_model(model)
    if (caps := MODELS.get(normalised)) is not None:
        return caps

    for pattern, caps in _FAMILY_PATTERNS:
        if pattern.match(normalised):
            _LOGGER.info(
                "Model %r is not in the capability table; using the closest family "
                "match. Please open an issue with a diagnostics dump.",
                model,
            )
            return caps

    _LOGGER.info(
        "Model %r is unknown; falling back to a minimal capability set. Please open an "
        "issue with a diagnostics dump so it can be supported properly.",
        model,
    )
    return FALLBACK
