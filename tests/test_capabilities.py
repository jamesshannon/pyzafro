"""Capability resolution must degrade, never raise."""

from __future__ import annotations

from pyzafro.capabilities import (
    FALLBACK,
    Feature,
    SwitchKey,
    normalise_model,
    resolve,
)
from pyzafro.models import Mode


def test_known_model_resolves():
    caps = resolve("90038EAC0-12K-ZAZ")
    assert caps.known_model
    assert Mode.COOL in caps.modes
    # Heating never appeared on this cooling-only window unit.
    assert Mode.HEAT not in caps.modes
    # 0 is the silent speed sleep selects, not "off".
    assert caps.fan_speeds == (0, 1, 2, 3, 4)
    assert caps.has(Feature.ECO)


def test_vendor_aliases_normalise():
    assert normalise_model("A9038-12K") == "90038EAC0-12K-ZAZ"
    assert resolve("A9038-12K").known_model


def test_unknown_model_falls_back_instead_of_raising():
    caps = resolve("SOME-FUTURE-MODEL")
    assert caps is FALLBACK
    assert not caps.known_model
    # Still usable: power, mode, setpoint, ambient readings.
    assert caps.modes
    assert caps.target_temperature_range is not None


def test_family_prefix_match():
    caps = resolve("90045EAC0-8K-ZAZ")
    assert caps.known_model


def test_an_unknown_air_conditioner_keeps_what_it_demonstrates():
    caps = resolve("SOMEAC-9000").refined(
        {
            "mode",
            "target_temperature",
            "ambient_temperature",
            "fan_speed",
            "sleep",
            "swing_vertical",
        }
    )

    assert caps.is_climate
    # The fallback offers neither of these; the device proved it has them.
    assert caps.has(Feature.SLEEP)
    assert caps.has(Feature.SWING_VERTICAL)
    assert SwitchKey.SLEEP in caps.switches
    # And it does not invent the ones that never appeared.
    assert not caps.has(Feature.ECO)
    assert caps.target_humidity_range is None


def test_a_product_from_another_class_claims_nothing():
    """A vacuum must not end up with a thermostat dial."""
    caps = resolve("SMARTVAC-3000").refined({"fault_code", "work_time"})

    assert not caps.is_climate
    assert caps.modes == frozenset()
    assert caps.target_temperature_range is None
    assert caps.switches == frozenset()


def test_refinement_only_ever_subtracts():
    known = resolve("90038EAC0-12K-ZAZ")
    assert known.known_model
    # Every field the window unit reports, so nothing should be lost.
    everything = {
        "mode",
        "target_temperature",
        "target_humidity",
        "fan_speed",
        "sleep",
        "eco",
        "child_lock",
        "display",
        "mute",
        "swing_horizontal",
        "swing_vertical",
    }
    assert known.refined(everything).features == known.features
