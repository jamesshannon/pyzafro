"""Capability resolution must degrade, never raise."""

from __future__ import annotations

from pyzafro.capabilities import FALLBACK, Feature, normalise_model, resolve
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
