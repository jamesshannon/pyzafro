"""The live checks, run against a fake unit.

The point of putting the conformance checks in the library rather than in a script is
that they can be tested here. So these tests do two things: run the checks against a
fake window unit that behaves the way the real one was measured to, expecting them all
to pass; then run them against units that misbehave in each specific way a check exists
to catch, expecting the right check to catch it. A check that cannot fail is not a
check, and the fake needs a knob for every claim, or the suite is decoration.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pyzafro.capabilities import Feature, resolve
from pyzafro.device import ZafroDevice
from pyzafro.selftest import (
    CHECKS,
    POWER_DOWN,
    REACH_WAIT,
    SUITES,
    Check,
    CheckFailedError,
    CheckSkippedError,
    Context,
    SelfTest,
    summarise,
)

RAW = {
    "sn": "6ISEComboWF020BSJ0000000000",
    "vendor": "I4SEASON",
    "model": "90038EAC0-12K-ZAZ",
    "name": "Test AC",
    "version": "1.0.29",
}

UNKNOWN_RAW = {**RAW, "model": "SOMETHING-NEW-1K-ZAZ"}

COOL = 1
DRY = 2
FAN_ONLY = 3

#: Keys the unit moves on its own. A push carries whichever of these ended up differing
#: from what was asked for, which is exactly what the real unit does — acknowledging a
#: value with origin 1 and then correcting it with origin 0, as it does for sleep in fan
#: mode. Anything missing from this list can be asked for and silently believed, which
#: is a fake that flatters the library rather than testing it.
_DEVICE_OWNED = (
    "poweron",
    "mode",
    "windlevel",
    "muteon",
    "templevel",
    "rhlevel",
    "sleep",
    "extra",
    "eco",
    # Read-only, never asked for, and pushed whenever the machine moves them. Without
    # these the thermal suite would be measuring a device that never reports working.
    "temperature",
    "rh",
    "reachtarget",
    "worktime",
)

#: What the real unit parks the fan at when it is powered down.
_OFF_SPEED = 1


class FakeUnit:
    """A window air conditioner, as measured on 2026-09-27.

    Applies a frame's keys **in the order they appear**, because that is what the real
    one does and it is the whole subject of the fix these checks protect. Entering sleep
    saves the fan speed and drops to 0; leaving sleep restores the saved speed as that
    key is read, so a later `windlevel` in the same frame wins and an earlier one loses.

    The defaults describe a unit that behaves. Every keyword is one specific way for it
    to stop behaving, so that each check can be shown to fail against a device that
    breaks the claim it protects and only that claim.
    """

    def __init__(
        self,
        *,
        deferred_restore: bool = False,
        minimal: bool = False,
        model: str = RAW["model"],
        # What the device really accepts, defaulting to what the table now claims after
        # a live run tightened it. A fake whose real limits are wider than the table's
        # is a fake reporting the table too narrow, which is a finding, not a baseline.
        real_temp_range: tuple[int, int] = (61, 86),
        real_humidity_range: tuple[int, int] = (30, 70),
        refuses_modes: frozenset[int] = frozenset(),
        ambient_humidity: int = 40,
        ambient_temperature: int = 75,
        cools: bool = True,
        regulates_humidity: bool = True,
        tracks_target: bool = True,
        runtime_step: int = 1,
        holds_speed_zero: bool = False,
        keeps_settings_while_off: bool = False,
        parks_fan_when_off: bool = True,
        parks_after_reads: int = 0,
        allows_programmes_in_fan_mode: bool = False,
        reports_origin: bool = True,
        silent_keys: tuple[str, ...] = (),
        ignores: tuple[str, ...] = (),
    ) -> None:
        """Build a unit.

        `deferred_restore` simulates firmware that restores the pre-sleep speed after
        reading the whole frame rather than as it reads the key. `minimal` reports only
        the fields a bare air conditioner would, which is what an uncatalogued model has
        to be narrowed down to — capabilities are refined from what a device *reports*,
        so a fake that reports everything would be granted everything.
        """
        self.device: ZafroDevice | None = None
        self.published: list[dict[str, Any]] = []
        self.deferred_restore = deferred_restore
        self.model = model
        #: The limits the unit really enforces, clamping anything outside them — what a
        #: table carrying an invented range looks like from the outside. Defaulting to
        #: the table's own numbers is what makes the default unit a passing one.
        self.real_temp_range = real_temp_range
        self.real_humidity_range = real_humidity_range
        self.refuses_modes = refuses_modes
        self.cools = cools
        self.regulates_humidity = regulates_humidity
        self.tracks_target = tracks_target
        self.runtime_step = runtime_step
        self.holds_speed_zero = holds_speed_zero
        self.keeps_settings_while_off = keeps_settings_while_off
        #: Whether powering down drops the fan to its slowest speed, which is why Low
        #: shows while off.
        self.parks_fan_when_off = parks_fan_when_off
        #: How many full reads a power-down takes before the fan parks, which is what
        #: the window unit's twenty-second turn-off timer looks like to a check that
        #: polls. Zero parks at once; one means the first read after the power-off still
        #: reports the speed the unit was running at, which is what a check that reads
        #: straight away mistakes for a unit that does not park at all.
        self.parks_after_reads = parks_after_reads
        self._parking_in: int | None = None
        self.allows_programmes_in_fan_mode = allows_programmes_in_fan_mode
        self.reports_origin = reports_origin
        #: Wire keys this unit drops in total silence: no acknowledgement and no
        #: correction. Nothing has ever been observed doing it, which is exactly why the
        #: library carries a resync timer against the possibility.
        self.ignores = ignores
        self.wire: dict[str, Any] = {
            "poweron": True,
            "mode": COOL,
            "templevel": 70,
            "temperature": ambient_temperature,
            "rhlevel": 50,
            "rh": ambient_humidity,
            "windlevel": 2,
            "tempunit": 1,
            "sleep": False,
            "eco": False,
            "extra": False,
            "muteon": False,
            "lighton": True,
            "childlockon": False,
            "oscset1": False,
            "oscset2": False,
            "waterlevel": 0,
            "filterthr": 500,
            "worktime": 1200,
            "reachtarget": False,
            "wrong": 0,
        }
        if minimal:
            for key in (
                "sleep",
                "eco",
                "extra",
                "muteon",
                "lighton",
                "childlockon",
                "oscset1",
                "oscset2",
            ):
                del self.wire[key]
        for key in silent_keys:
            self.wire.pop(key, None)
        self._saved_speed = 2
        self._moved: set[str] = set()
        self._recompute()

    def register(self, sink: Any) -> None:
        """Match the transport interface. Frames are delivered directly here."""

    async def publish(self, vendor: str, sn: str, payload: dict[str, Any]) -> None:
        """Answer a request the way the unit does: an ack, then its own corrections.

        Replies are scheduled rather than delivered inline. A real device answers over
        the network, so the acknowledgement cannot possibly arrive before the publish
        returns — and delivering it inline hides a genuine ordering question, because
        `_apply_optimistic` runs after the publish and would otherwise mark a field
        pending that had already been confirmed.
        """
        assert self.device is not None
        self.published.append(payload)
        for frame in self._replies_to(payload):
            asyncio.get_running_loop().call_soon(self._deliver, *frame)

    def _deliver(self, cmd: int, result: dict[str, Any]) -> None:
        assert self.device is not None
        self.device.handle_frame(cmd, result)

    def _stamp(self, frame: dict[str, Any], origin: int) -> dict[str, Any]:
        """Add the origin, unless this unit is one that does not report it."""
        return frame if not self.reports_origin else {**frame, "origin": origin}

    def _replies_to(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        cmd = payload["cmd"]
        if cmd == 3:
            # A full read is also the moment a real unit's counter has advanced, and the
            # moment a power-down still running its timer has had time to finish. Both
            # are functions of time rather than of anything commanded.
            self._tick()
            self._finish_parking()
            self._recompute()
            return [(3, self._stamp(dict(self.wire), 0))]
        if cmd == 5:
            return [
                (5, {"v": "I4SEASON", "p": self.model, "ver": "1.0.29", "rssi": 44})
            ]

        asked = {
            key: value
            for key, value in payload["data"]["state"].items()
            if key not in self.ignores
        }
        self._apply(asked)
        replies = [(4, self._stamp(dict(asked), 1))] if asked else []
        # Only what it moved itself, and only where that differs from what was asked —
        # which is why a speed written last produces no correction at all.
        push = {
            key: self.wire[key]
            for key in _DEVICE_OWNED
            if key in self._moved
            and key in self.wire
            and asked.get(key, object()) != self.wire[key]
        }
        if push:
            replies.append((4, self._stamp(push, 0)))
        return replies

    def _apply(self, asked: dict[str, Any]) -> None:
        self._moved = set()
        left_sleep = False
        for key, asked_value in asked.items():
            previous = self.wire.get(key)
            if self._refuses(key, asked_value):
                continue
            self.wire[key] = self._clamp(key, asked_value)
            if self.wire[key] != asked_value:
                self._moved.add(key)
            left_sleep |= self._side_effects(key, self.wire[key], previous)
        if left_sleep:
            self._move("windlevel", self._saved_speed)
        self._recompute()

    def _refuses(self, key: str, value: Any) -> bool:
        """Acknowledge and then undo, the way the real unit turns down a programme."""
        programmes = {"sleep", "extra", "eco"}
        if (
            key in programmes
            and value
            and self.wire["mode"] != COOL
            and not self.allows_programmes_in_fan_mode
        ):
            # A cooling programme outside cool mode.
            self.wire[key] = False
            self._moved.add(key)
            return True
        if key == "mode" and value in self.refuses_modes:
            self._moved.add("mode")
            return True
        if key == "windlevel" and value == 0 and not self.holds_speed_zero:
            # Acknowledged, then reverted a few seconds later. The speed sleep reports
            # is not a speed that can be asked for.
            self._moved.add("windlevel")
            return True
        if not self.wire["poweron"] and not self.keeps_settings_while_off:
            # A unit that is off does not keep what it is told, and parks its fan.
            if key == "poweron":
                return False
            self._moved.add(key)
            return True
        return False

    def _side_effects(self, key: str, value: Any, previous: Any) -> bool:
        """Apply what the unit does off its own bat. Returns whether sleep was left."""
        if key == "poweron" and not value:
            if not self.parks_fan_when_off:
                pass
            elif self.parks_after_reads:
                self._parking_in = self.parks_after_reads
            else:
                self._move("windlevel", _OFF_SPEED)
        elif key == "poweron" and value:
            self._parking_in = None
        elif key == "sleep" and value and not previous:
            self._saved_speed = self.wire["windlevel"]
            self._move("windlevel", 0)
            self._move("muteon", value=True)
        elif key == "sleep" and previous and not value:
            self._move("muteon", value=False)
            if self.deferred_restore:
                return True
            self._move("windlevel", self._saved_speed)
        elif key == "extra" and value:
            self._move("windlevel", 3)
            self._move("sleep", value=False)
            # Observed at 61, which a later run established is the real floor.
            self._move("templevel", self.real_temp_range[0])
        elif key == "eco" and value:
            self._move("windlevel", 1)
            self._move("templevel", 76)
        return False

    def _finish_parking(self) -> None:
        """Let a power-down that takes a while get as far as parking the fan.

        A read at a time, because the fake has no clock and a check that waits out the
        turn-off timer is a check that polls. So "the state after the timer" and "the
        state after another full read" are the same thing here.
        """
        if self._parking_in is None:
            return
        if self._parking_in > 0:
            self._parking_in -= 1
            return
        self._parking_in = None
        self._move("windlevel", _OFF_SPEED)

    def _tick(self) -> None:
        """Advance the runtime counter, which a full read is a chance to notice."""
        if "worktime" in self.wire and self.wire["poweron"]:
            self.wire["worktime"] += self.runtime_step

    def _recompute(self) -> None:
        """Set the read-only fields that follow from everything else."""
        if "reachtarget" not in self.wire:
            return
        if not self.tracks_target:
            return
        # Which setpoint the thermostat watches depends on the mode, which is the live
        # unit's behaviour and the evidence that dry mode is the humidity mode: the
        # temperature target stayed satisfied across the change and reachtarget still
        # went out. A fake that called dry mode reached whatever the humidity was doing
        # would pass that check without the device having to do anything.
        if self.wire["mode"] == COOL:
            # Which side of the room this mode is satisfied on is the whole of what
            # `Mode.COOL = 1` claims. A thermostat satisfied when the target sits below
            # the room is a heating thermostat wearing cool's number, and the live unit
            # is satisfied above it.
            reached = (
                self.wire["temperature"] <= self.wire["templevel"]
                if self.cools
                else self.wire["temperature"] >= self.wire["templevel"]
            )
        elif self.wire["mode"] == DRY and self.regulates_humidity:
            reached = self.wire["rh"] <= self.wire["rhlevel"]
        else:
            reached = True
        if reached != self.wire["reachtarget"]:
            self._move("reachtarget", reached)

    def _clamp(self, key: str, value: Any) -> Any:
        if key == "templevel":
            bounds = self.real_temp_range
        elif key == "rhlevel":
            bounds = self.real_humidity_range
        else:
            return value
        return min(max(value, bounds[0]), bounds[1])

    def _move(self, key: str, value: Any) -> None:
        self.wire[key] = value
        self._moved.add(key)


@pytest.fixture
def unit() -> FakeUnit:
    return FakeUnit()


def _device(transport: FakeUnit, raw: dict[str, Any] = RAW) -> ZafroDevice:
    device = ZafroDevice(raw, transport)  # type: ignore[arg-type]
    transport.device = device
    return device


async def _run(transport: FakeUnit, **kwargs: Any) -> Any:
    """Baseline, run, and always close, returning the runner and its results."""
    device = _device(transport, kwargs.pop("raw", RAW))
    await device.async_refresh()
    kwargs.setdefault("settle", 0)
    # A max wait of 0 still gives the device one poll, which is all the fake needs: it
    # does a wait's worth of work per command rather than per second.
    kwargs.setdefault("max_wait", 0)
    kwargs.setdefault("poll", 0)
    runner = SelfTest(device, **kwargs)
    try:
        return runner, await runner.run()
    finally:
        device.close()


def _by_name(results: Any) -> dict[str, Any]:
    return {result.name: result for result in results}


def _one(results: Any, name: str) -> Any:
    return _by_name(results)[name]


# --- the checks pass against a unit that behaves ------------------------------------


async def test_a_well_behaved_unit_passes_every_check(unit):
    """The baseline. Anything failing here is a bug in a check, not in a device."""
    _, results = await _run(unit)
    counts = summarise(results)
    failures = [(r.name, r.detail) for r in results if r.outcome in {"fail", "error"}]
    assert failures == []
    assert counts["pass"] > 0


async def test_the_full_run_covers_every_suite(unit):
    """A suite nobody runs protects nothing."""
    _, results = await _run(unit)
    assert {result.suite for result in results} == set(SUITES)


async def test_every_registered_check_actually_ran(unit):
    """A check registered under a suite name nobody selects is dead code."""
    _, results = await _run(unit)
    assert len(results) == len(SelfTest(_device(FakeUnit())).checks())


# --- and fail against the unit as it behaved before 1.3.1 ---------------------------


async def test_the_sleep_exit_check_catches_a_lost_fan_speed():
    """The regression this whole module exists to catch.

    A unit that restores the pre-sleep speed *after* reading the whole frame cannot be
    fixed by ordering the keys, so the requested speed is lost — which is precisely what
    the reported bug looked like. The check has to notice.
    """
    transport = FakeUnit(deferred_restore=True)
    _, results = await _run(transport)

    lost = _one(results, "a_speed_leaves_sleep_at_that_speed")
    assert lost.outcome == "fail"
    assert "settled at" in lost.detail

    # And it fails for the right reason: the fan came back at the pre-sleep speed.
    assert "asked for fan speed 4" in lost.detail
    assert "device settled at 1" in lost.detail


async def test_a_failure_carries_the_frames_that_caused_it():
    """A failure with no trace sends someone back to the hardware to reproduce it."""
    transport = FakeUnit(deferred_restore=True)
    _, results = await _run(transport)
    lost = _one(results, "a_speed_leaves_sleep_at_that_speed")
    assert lost.trace, "a failing check recorded no frames"
    assert any(frame["result"].get("origin") == 0 for frame in lost.trace)


async def test_one_failing_check_does_not_end_the_run():
    """A device that breaks one claim should still be measured against the others."""
    transport = FakeUnit(deferred_restore=True)
    _, results = await _run(transport)
    assert summarise(results)["fail"] >= 1
    assert summarise(results)["pass"] >= 1


async def test_the_sleep_exit_check_would_have_caught_the_shipped_bug(monkeypatch):
    """Take the ordering guarantee away and the check must fail against a real unit.

    The strongest thing that can be said for a live check: run it against a device that
    behaves exactly as the hardware was measured to behave, with the library reverted to
    what 1.3.0 shipped, and it catches the bug that was reported. Emptying `_WIRE_ORDER`
    returns `build_command` to emitting whatever order the caller built its dict in,
    which put `windlevel` first.
    """
    monkeypatch.setattr("pyzafro.models._WIRE_ORDER", ())
    transport = FakeUnit()  # the faithful unit, applying keys strictly in order
    _, results = await _run(transport)

    lost = _one(results, "a_speed_leaves_sleep_at_that_speed")
    assert lost.outcome == "fail"
    assert "asked for fan speed 4" in lost.detail
    assert "device settled at 1" in lost.detail

    # And the order really was the only thing that changed.
    speed_frames = [
        frame["data"]["state"]
        for frame in transport.published
        if frame["cmd"] == 6 and "windlevel" in frame["data"]["state"]
    ]
    assert list(speed_frames[0]) == ["windlevel", "sleep", "extra", "eco"]


async def test_a_unit_that_holds_speed_zero_says_so():
    """The claim that produced a documentation error, now measured rather than assumed.

    1.3.0 shipped "windlevel 0 is refused" on a probe taken with the unit off, where
    every speed reverts. If a unit does hold it, it is a real speed and belongs in the
    table — and the check has to be the thing that says so.
    """
    _, results = await _run(FakeUnit(holds_speed_zero=True))
    held = _one(results, "the_sleep_speed_is_refused_by_the_device")
    assert held.outcome == "fail"
    assert "belongs in fan_speeds" in held.detail


# --- skipping, rather than failing, when a model cannot answer -----------------------


async def test_a_model_without_a_feature_skips_instead_of_failing():
    """A device narrowed to a fan speed alone cannot answer most of these checks.

    A check that failed here would tell every owner of an uncatalogued device that their
    unit is broken, when all that happened is the table does not describe it.
    """
    _, results = await _run(
        FakeUnit(minimal=True, model=UNKNOWN_RAW["model"]), raw=UNKNOWN_RAW
    )
    by_name = _by_name(results)
    assert by_name["a_speed_leaves_sleep_at_that_speed"].outcome == "skip"
    assert by_name["the_fan_positions_are_exclusive"].outcome == "skip"
    assert by_name["sleep_also_mutes_the_beeper"].outcome == "skip"
    # And it says so plainly, naming the model rather than blaming the device.
    unknown = by_name["the_model_is_in_the_capability_table"]
    assert unknown.outcome == "fail"
    assert UNKNOWN_RAW["model"] in unknown.detail


async def test_an_unexpected_exception_is_recorded_not_raised(unit):
    """A broken check must not take the run, or the restore, down with it."""
    device = _device(unit)
    await device.async_refresh()
    runner = SelfTest(device, suites=["fan"], settle=0, max_wait=0)

    async def explode(_: Context) -> None:
        raise RuntimeError("the check itself is broken")

    runner.checks = lambda: [  # type: ignore[method-assign]
        Check(name="explode", suite="fan", claim="nothing", run=explode, cost=0)
    ]
    results = await runner.run()
    device.close()

    assert results[0].outcome == "error"
    assert "RuntimeError: the check itself is broken" in results[0].detail


# --- the runner puts the unit back --------------------------------------------------


async def test_the_unit_is_restored_to_what_it_was_found_in(unit):
    """Every field the run moves has to come back, or the tool is a liability."""
    unit.wire.update({"poweron": True, "mode": COOL, "windlevel": 3, "templevel": 68})
    device = _device(unit)
    await device.async_refresh()
    before = device.state

    runner = SelfTest(device, settle=0, max_wait=0)
    await runner.run()
    device.close()

    for name in ("power", "mode", "fan_speed", "sleep", "eco"):
        assert getattr(device.state, name) == getattr(before, name), name


async def test_a_unit_found_off_is_powered_back_off_last(unit):
    """Settings written to a unit that is off do not stick, so power goes last.

    The real unit parks its fan at the slowest speed while off and reverts anything else
    asked of it, so restoring the speed after cutting power would silently restore
    nothing.
    """
    unit.wire["poweron"] = False
    device = _device(unit)
    await device.async_refresh()

    runner = SelfTest(device, settle=0, max_wait=0)
    await runner.run()
    device.close()

    controls = [
        frame["data"]["state"]
        for frame in unit.published
        if frame["cmd"] == 6 and "poweron" in frame["data"]["state"]
    ]
    assert controls[-1] == {"poweron": False}
    assert device.state.power is False


async def test_the_unit_is_restored_even_when_a_check_explodes(unit):
    """The restore is in a finally block, and this is the test that says so."""
    device = _device(unit)
    await device.async_refresh()
    before = device.state
    runner = SelfTest(device, suites=["fan"], settle=0, max_wait=0)

    async def explode(ctx: Context) -> None:
        await ctx.device.async_set_sleep(on=True)
        raise RuntimeError("boom")

    runner.checks = lambda: [  # type: ignore[method-assign]
        Check(name="explode", suite="fan", claim="nothing", run=explode, cost=0)
    ]
    await runner.run()
    device.close()

    assert device.state.sleep == before.sleep
    assert device.state.fan_speed == before.fan_speed


async def test_a_clean_restore_reports_nothing_outstanding(unit):
    """The promise the run is permitted on, checked rather than assumed."""
    runner, _ = await _run(unit)
    assert runner.restored == {}


async def test_a_restore_that_did_not_take_is_reported():
    """A tool that says it puts things back has to notice when it did not.

    A unit that drops a setting in silence — which this whole library's resync timer
    exists against — would otherwise leave someone's air conditioner somewhere they did
    not put it, with the run reporting success.

    Found with the beeper muted, and on a unit that will not be told to mute it again:
    leaving sleep unmutes it as a side effect, which the unit does of its own accord,
    and then the restore's own command goes nowhere.
    """
    unit = FakeUnit(ignores=("muteon",))
    unit.wire["muteon"] = True
    device = _device(unit)
    await device.async_refresh()

    runner = SelfTest(device, suites=["fan"], settle=0, max_wait=0)
    await runner.run()
    device.close()

    assert "mute" in runner.restored, runner.restored
    assert runner.restored["mute"]["wanted"] is True
    assert runner.restored["mute"]["got"] is False


# --- the guard rails ----------------------------------------------------------------


async def test_it_refuses_a_device_that_has_said_nothing(unit):
    """Nothing can be planned, and nothing can be restored, without a baseline."""
    device = _device(unit)
    assert SelfTest(device, settle=0).refuse_reason() is not None
    device.close()


async def test_a_warm_room_is_no_longer_a_reason_to_refuse(unit):
    """Running the machine is the point of the full integration suite.

    The earlier version refused to start when cool mode would really cool the room,
    which meant the numbers that most needed measuring were the ones a warm day made
    unmeasurable. A run that costs ten minutes of compressor is the cheaper mistake.
    """
    unit.wire["temperature"] = 95
    device = _device(unit)
    await device.async_refresh()
    assert SelfTest(device, settle=0).refuse_reason() is None
    device.close()


async def test_an_unknown_suite_is_rejected_before_anything_moves(unit):
    """A typo in --suite must not silently run nothing, or worse, everything."""
    device = _device(unit)
    with pytest.raises(ValueError, match="unknown suite"):
        SelfTest(device, suites=["fan", "nonsense"])
    device.close()


async def test_the_plan_says_what_it_will_change_and_for_how_long(unit):
    """It asks for consent, so the plan has to be worth reading."""
    device = _device(unit)
    await device.async_refresh()
    runner = SelfTest(device, suites=["fan"])
    plan = "\n".join(runner.plan())
    device.close()

    assert RAW["model"] in plan
    assert "fan" in plan
    assert "restore" in plan
    assert runner.estimate() > 0


async def test_the_plan_says_when_it_will_run_the_compressor(unit):
    """Consent has to be informed, and this is the part someone would object to."""
    device = _device(unit)
    await device.async_refresh()
    without = "\n".join(SelfTest(device, suites=["fan"]).plan())
    with_thermal = "\n".join(SelfTest(device, suites=["fan", "thermal"]).plan())
    device.close()

    assert "compressor" not in without.split("idles")[-1]
    assert "WILL run the compressor" in with_thermal


async def test_a_smaller_selection_of_suites_is_a_shorter_run(unit):
    """Why the suites are selectable at all."""
    device = _device(unit)
    one = SelfTest(device, suites=["fan"]).estimate()
    everything = SelfTest(device, suites=SUITES).estimate()
    device.close()
    assert one < everything


async def test_the_waits_dominate_the_estimate(unit):
    """Someone told five minutes and kept for twenty will not run this again.

    Against the settling between commands, which is the other thing the estimate is
    made of. Stated structurally rather than as a multiple of `--max-wait`, because no
    check spends the ceiling any more: every thermal wait is clamped to REACH_WAIT, and
    one of the four checks waits for nothing at all.
    """
    device = _device(unit)
    thermal = SelfTest(device, suites=["thermal"])
    device.close()
    settling = sum(c.cost for c in CHECKS if c.suite == "thermal") * thermal.settle
    assert thermal.estimate() > 4 * settling


async def test_the_plan_says_the_thermal_figure_is_a_ceiling(unit):
    """Every thermal wait ends early on a healthy unit, so the estimate over-states it.

    Erring high is the only safe direction for a number someone is consenting on, but a
    ceiling presented as a duration is its own kind of wrong.
    """
    device = _device(unit)
    await device.async_refresh()
    plan = "\n".join(SelfTest(device).plan())
    device.close()
    assert "ceiling" in plan
    assert "as soon as" in plan


# --- the check helpers --------------------------------------------------------------


async def test_a_speed_that_something_is_overriding_is_not_a_pass(unit):
    """The fan being at the right speed is not enough if a programme can move it."""
    device = _device(unit)
    await device.async_refresh()
    device.handle_frame(4, {"windlevel": 3, "eco": True, "origin": 0})
    ctx = Context(device, settle=0)
    with pytest.raises(CheckFailedError, match="eco is still on"):
        ctx.expect_speed(3)
    device.close()


async def test_requiring_an_absent_feature_skips_with_its_name():
    """A skip that does not say what was missing is a skip nobody can act on."""
    device = _device(FakeUnit(minimal=True, model=UNKNOWN_RAW["model"]), UNKNOWN_RAW)
    await device.async_refresh()
    ctx = Context(device, settle=0)
    with pytest.raises(CheckSkippedError, match="sleep"):
        ctx.requires(Feature.SLEEP)
    device.close()


# --- the protocol claims a consumer has been told to expect -------------------------


async def test_a_unit_that_keeps_sleep_in_fan_mode_is_a_failure():
    """Both READMEs promise the unit refuses sleep, so a unit that does not is news."""
    _, results = await _run(FakeUnit(allows_programmes_in_fan_mode=True))
    refused = _one(results, "sleep_is_refused_in_fan_mode")
    assert refused.outcome == "fail"
    assert "both READMEs say" in refused.detail


async def test_extra_and_eco_in_fan_mode_are_recorded_without_a_verdict():
    """A live run found fan mode keeping both, and nothing here depends on the answer.

    They used to be asserted alongside sleep on the reasoning that all three are cooling
    programmes. That was a guess about the device's reasoning, the device disagreed, and
    a claim this library does not rely on should not be able to fail a run.
    """
    _, results = await _run(FakeUnit(allows_programmes_in_fan_mode=True))
    refused = _one(results, "sleep_is_refused_in_fan_mode")

    assert refused.measured["extra_in_fan_mode"] is True
    assert refused.measured["eco_in_fan_mode"] is True
    assert "extra" not in refused.detail


async def test_the_refusal_is_also_how_reconciliation_gets_exercised():
    """The library must end up at the device's answer, not at what it hoped for."""
    _, results = await _run(FakeUnit(allows_programmes_in_fan_mode=True))
    overridden = _one(results, "an_overridden_command_ends_up_at_the_devices_answer")
    assert overridden.outcome == "fail"
    assert "no override here to reconcile" in overridden.detail


async def test_a_unit_that_keeps_settings_while_off_is_a_failure():
    """`_restore` orders its frames on this, so the tool depends on it being true."""
    _, results = await _run(FakeUnit(keeps_settings_while_off=True))
    off = _one(results, "the_off_state_keeps_the_fan_speed_it_was_given")
    assert off.outcome == "fail"
    assert "_restore is ordering its frames for no reason" in off.detail
    assert (
        off.measured["fan_speed_read_back_while_off"]
        == off.measured["fan_speed_written_while_off"]
    )


@pytest.mark.parametrize("parks", [True, False])
async def test_the_speed_written_while_off_is_never_one_already_set(*, parks: bool):
    """The first version of this check wrote the top speed to a unit already at it.

    So the read-back was the same number whether the write landed or was thrown away,
    and it reported "settings do stick after all" on evidence that could not tell the
    two apart. Picking the slowest speed instead has the same problem the other way up,
    against a unit that parks, so the speed has to be chosen after the first reading and
    against whatever the unit turned out to report. Asserted both ways round for that
    reason.
    """
    _, results = await _run(FakeUnit(parks_fan_when_off=parks))
    off = _one(results, "the_off_state_keeps_the_fan_speed_it_was_given")
    assert (
        off.measured["fan_speed_while_off"]
        != off.measured["fan_speed_written_while_off"]
    )


async def test_a_power_down_that_takes_its_time_is_waited_out():
    """The window unit runs a turn-off timer of about twenty seconds.

    The fan is still going for all of it, so a reading taken inside that window is of a
    unit shutting down and not of a unit that is off. The live run read at 5.5s and then
    at 12.1s and powered the unit back on at 12.7s, and reported a unit that does not
    park its fan — from a fan that had not finished stopping. Both READMEs were changed
    on that, and changed back.
    """
    assert POWER_DOWN > 20.0
    _, results = await _run(FakeUnit(parks_after_reads=1))
    off = _one(results, "the_off_state_keeps_the_fan_speed_it_was_given")

    assert off.outcome == "pass"
    # The park lands on the second read, so the first one alone would have called this a
    # unit that keeps its speed through a power-down.
    assert off.measured["fan_speed_while_off"] == 1


async def test_a_device_that_stops_reporting_origin_is_a_failure():
    """Every conclusion about this protocol was drawn by attributing a change."""
    _, results = await _run(FakeUnit(reports_origin=False))
    origin = _one(results, "a_commanded_change_is_reported_as_commanded")
    assert origin.outcome == "fail"
    assert "no origin at all" in origin.detail


async def test_the_documented_side_effects_are_measured_not_assumed(unit):
    """A consumer is told to wait for these rather than guess them."""
    _, results = await _run(unit)
    by_name = _by_name(results)
    assert by_name["sleep_also_mutes_the_beeper"].outcome == "pass"
    assert by_name["eco_forces_its_own_speed_and_setpoint"].outcome == "pass"

    extra = by_name["extra_moves_the_setpoint_within_the_claimed_range"]
    assert extra.outcome == "pass"
    # And the number it found travels with the result, which is the point of running it.
    assert extra.measured["setpoint_under_extra"] == 61


async def test_an_extra_setpoint_outside_the_table_proves_the_table_wrong():
    """A value the device picks for itself is one it accepts, so the range is wrong."""
    _, results = await _run(FakeUnit(real_temp_range=(50, 86)))
    extra = _one(results, "extra_moves_the_setpoint_within_the_claimed_range")
    assert extra.outcome == "fail"
    assert "outside the table's 61-86" in extra.detail


# --- the assumptions the table only guesses at --------------------------------------


async def test_a_clamped_setpoint_bound_names_the_real_limit():
    """The whole point of walking the range: the bounds shipped as an invention.

    A unit that clamps is a unit whose real limits differ from the table's, and the
    value it clamps to is the number the table should carry — so the failure has to say
    it, not merely report a mismatch.
    """
    _, results = await _run(FakeUnit(real_temp_range=(62, 84)))
    bounds = _one(results, "the_setpoint_range_is_accepted")
    assert bounds.outcome == "fail"
    assert "is the real limit" in bounds.detail


async def test_a_range_the_device_exceeds_is_reported_as_too_narrow():
    """The direction no other check can see, because the library refuses it first.

    A unit that accepts 60 would never be asked for it, so its owner would simply never
    be offered a setting their hardware has. Only a raw frame can find that — and the
    live run vindicated it from the other side: the table claimed 60, the device clamped
    that to 61, so one guess was wrong in both directions at once.
    """
    _, results = await _run(FakeUnit(real_temp_range=(50, 90)))
    narrow = _one(results, "the_setpoint_range_is_not_too_narrow")
    assert narrow.outcome == "fail"
    assert "the table is too narrow" in narrow.detail
    assert "60" in narrow.detail


async def test_the_humidity_range_is_always_tested_now():
    """It used to skip whenever the air was humid, which is most of the time."""
    transport = FakeUnit(ambient_humidity=90)
    _, results = await _run(transport)
    humidity = _one(results, "the_humidity_range_is_accepted")
    assert humidity.outcome == "pass", humidity.detail

    # It really did go to dry mode and command both ends, then come back.
    asked = [f["data"]["state"] for f in transport.published if f["cmd"] == 6]
    assert {"rhlevel": 30} in asked
    assert {"rhlevel": 70} in asked
    assert transport.wire["mode"] == COOL


async def test_a_clamped_humidity_bound_names_the_real_limit():
    """The least evidenced number in the table, and now the one most worth checking."""
    _, results = await _run(FakeUnit(real_humidity_range=(35, 60)))
    humidity = _one(results, "the_humidity_range_is_accepted")
    assert humidity.outcome == "fail"
    assert "is the real limit" in humidity.detail


async def test_an_excursion_leaves_the_setpoint_satisfied_behind_it():
    """A check that cools must not leave the unit cooling for the rest of the run."""
    transport = FakeUnit()
    device = _device(transport)
    await device.async_refresh()
    runner = SelfTest(device, suites=["capabilities"], settle=0, max_wait=0)
    await runner.run()
    device.close()

    asked = [f["data"]["state"] for f in transport.published if f["cmd"] == 6]
    bounds = resolve(RAW["model"]).target_temperature_range
    assert bounds is not None
    floor = bounds[0]
    low = max(i for i, frame in enumerate(asked) if frame.get("templevel") == floor)
    after = [f for f in asked[low + 1 :] if "templevel" in f]
    assert after, "nothing reset the setpoint after the excursion"
    assert after[0]["templevel"] == 86


async def test_a_mode_the_device_refuses_is_a_failure_not_a_shrug():
    """`modes` builds the HVAC dropdown, so a mode that snaps back is a dead option."""
    _, results = await _run(FakeUnit(refuses_modes=frozenset({FAN_ONLY})))
    modes = _one(results, "every_claimed_mode_is_accepted")
    assert modes.outcome == "fail"
    assert "fan mode" in modes.detail


async def test_dry_mode_is_now_checked_rather_than_named_as_untested(unit):
    """Skipping dry was defensible only while cooling was off limits."""
    _, results = await _run(unit)
    modes = _one(results, "every_claimed_mode_is_accepted")
    assert modes.outcome == "pass", modes.detail
    assert modes.measured["mode_dry_became"] == "dry"


async def test_a_sensor_the_table_claims_and_the_device_never_sends_is_a_failure():
    """An entity that shows nothing forever looks like a broken integration."""
    _, results = await _run(FakeUnit(silent_keys=("filterthr", "waterlevel")))
    silent = _one(results, "every_claimed_sensor_has_a_value")
    assert silent.outcome == "fail"
    assert "filter_hours" in silent.detail
    assert "water_level" in silent.detail


async def test_the_step_the_slider_is_built_with_is_checked(unit):
    """A unit that snapped to even degrees would give a slider that jumps."""
    _, results = await _run(unit)
    assert _one(results, "the_setpoint_step_is_one_degree").outcome == "pass"


async def test_the_run_says_which_assumptions_it_cannot_test_at_all(unit):
    """A passing run must not read as "every assumption verified"."""
    _, results = await _run(unit)
    gap = _one(results, "some_assumptions_cannot_be_checked_at_all")
    assert gap.outcome == "skip"
    assert "tempunit is read-only" in gap.detail
    assert "availability" in gap.detail
    assert "oscset1" in gap.detail


async def test_a_read_only_field_the_library_would_send_is_a_failure(unit):
    """Walks the whole read-only set, so a field added without a guard is caught."""
    _, results = await _run(unit)
    assert _one(results, "a_read_only_field_is_refused").outcome == "pass"


# --- the claims that need the machine to be working ---------------------------------


async def test_a_mode_number_that_may_be_mislabelled_is_a_failure():
    """`Mode.COOL = 1` came from decompiled Dart and labels the whole HVAC dropdown.

    Every other check would pass with the enum shuffled: commanding mode 1 and reading
    mode 1 back proves the device accepts the number, never that the number means
    cooling. A mislabelled member puts "Cool" on the button that dehumidifies.
    """
    _, results = await _run(FakeUnit(cools=False))
    cooling = _one(results, "cool_mode_is_the_mode_that_cools")
    assert cooling.outcome == "fail"
    assert "the whole mode list is mislabelled" in cooling.detail


async def test_the_cool_check_reads_the_thermostat_and_not_the_room():
    """It used to wait three minutes for the room to fall, which cannot be measured.

    The ambient reading alternates between two adjacent integers, so two live runs of
    that version disagreed on the same unit in the same room: a pass in 0.0 seconds off
    a reading on its way down, then a failure after the whole wait. What the mapping
    claims is which way round the thermostat is satisfied, which the MCU answers at
    once — so the check is budgeted a `reaches` deadline and never reads
    `ambient_temperature` for a verdict. There is no room-waiting budget to carry
    instead, which is the point of having deleted the field.
    """
    cooling = next(c for c in CHECKS if c.suite == "thermal" and "cool_mode" in c.name)
    assert not hasattr(cooling, "soaks")
    assert cooling.reaches == 1

    _, results = await _run(FakeUnit())
    passed = _one(results, "cool_mode_is_the_mode_that_cools")
    assert passed.outcome == "pass"
    assert passed.measured["reached_target_with_target_above_ambient"] is True
    assert passed.measured["reached_target_with_target_below_ambient"] is False


async def test_a_humidity_target_the_device_only_stores_is_a_failure():
    """Two claims at once: the mode number, and whether `rhlevel` drives anything.

    If the device merely stores the number, `target_humidity` is a control that does
    nothing and should not be offered at all.
    """
    _, results = await _run(FakeUnit(ambient_humidity=90, regulates_humidity=False))
    dry = _one(results, "dry_mode_regulates_humidity_not_temperature")
    assert dry.outcome == "fail"
    assert "should not be offered" in dry.detail


async def test_dry_mode_is_read_from_the_thermostat_not_from_the_room(unit):
    """The comparator answers in seconds what three minutes of physics could not.

    The temperature target stays satisfied across the mode change, so a thermostat that
    goes unsatisfied anyway is comparing something else — and `rhlevel` is the only
    other setpoint the device has.
    """
    _, results = await _run(unit)
    dry = _one(results, "dry_mode_regulates_humidity_not_temperature")

    assert dry.outcome == "pass", dry.detail
    assert dry.measured["reached_target_with_temperature_satisfied"] is True
    assert dry.measured["reached_target_in_dry"] is False
    assert dry.measured["reached_target_with_humidity_satisfied"] is True


async def test_the_dry_check_no_longer_waits_on_the_room():
    """It used to soak for three minutes and failed a unit that was working.

    The reading flaps between two adjacent integers, so the noise band is the size of
    the change, and the old check asked for a fall past it. What is left is budgeted
    against the MCU comparing two numbers instead.
    """
    dry = next(c for c in CHECKS if c.suite == "thermal" and "dry_mode" in c.name)

    assert dry.reaches == 1
    assert REACH_WAIT < 180.0


async def test_no_check_can_budget_a_wait_on_the_room():
    """Three did, and all three were wrong, so the budget line is gone.

    Both mode checks because each ambient reading alternates between two adjacent
    integers, which makes the noise twice the smallest change either could look for; the
    runtime counter because it ticks in hours, which no tolerable wait can see. None of
    them is fixable by waiting longer. Keeping a `soaks` field would invite a fourth, so
    a check that wants to wait on physics now has to reintroduce the idea on purpose.
    """
    assert not hasattr(Check, "soaks")
    assert all(check.reaches <= 1 for check in CHECKS)


async def test_a_reached_target_that_never_moves_is_a_constant_with_a_name():
    """The entity is named after a behaviour nobody has ever watched it perform."""
    _, results = await _run(FakeUnit(tracks_target=False))
    reached = _one(results, "reached_target_follows_the_setpoint")
    assert reached.outcome == "fail"
    assert "does not track the target" in reached.detail
    assert "this library builds from it" in reached.detail


async def test_reached_target_is_measured_on_both_sides_of_the_setpoint(unit):
    """A field stuck at either value looks correct from one side."""
    _, results = await _run(unit)
    reached = _one(results, "reached_target_follows_the_setpoint")
    assert reached.outcome == "pass", reached.detail
    assert reached.measured["reached_target_satisfied"] is True
    assert reached.measured["reached_target_working"] is False


async def test_a_runtime_counter_too_coarse_to_move_is_a_skip_not_a_pass():
    """Its units have never been established, so a flat reading proves nothing."""
    _, results = await _run(FakeUnit(runtime_step=0))
    runtime = _one(results, "the_runtime_counter_is_monotonic")
    assert runtime.outcome == "skip"
    # And it says what the flat reading did settle, because a counter too coarse to
    # catch is what hours predicts and is not what seconds or minutes would look like.
    assert "rules out seconds and minutes" in runtime.detail
    assert runtime.measured["units_finer_than_the_run"] is False


async def test_the_runtime_check_is_about_the_state_class_not_the_appliance():
    """`total_increasing` is a promise this library makes, so a decrease is its bug."""
    _, results = await _run(FakeUnit(runtime_step=-5))
    runtime = _one(results, "the_runtime_counter_is_monotonic")
    assert runtime.outcome == "fail"
    assert "the state class is wrong" in runtime.detail


async def test_the_runtime_window_is_the_whole_run_and_costs_nothing():
    """It used to soak for three minutes of its own and learn nothing, twice.

    The counter did not move in either live run, so six minutes bought two skips. The
    state the run found is a window four times longer for no wait at all, which is also
    long enough to tell "minutes, and the tick was just missed" from "hours".
    """
    runtime = next(c for c in CHECKS if "runtime_counter" in c.name)
    assert runtime.cost == 0
    assert runtime.reaches == 0

    transport = FakeUnit()
    _, results = await _run(transport)
    result = _one(results, "the_runtime_counter_is_monotonic")

    # Measured against the counter as the run found it rather than as this check first
    # saw it. The fake advances on every full read, and the checks ahead of this one do
    # plenty, so a step of more than one is only possible from the wider window.
    began = result.measured["work_time_when_the_run_began"]
    assert result.measured["step"] == result.measured["work_time_now"] - began
    assert result.measured["step"] > 2


async def test_a_wait_ends_as_soon_as_the_machine_has_responded(unit):
    """A fixed sleep is slower and worse evidence than a deadline.

    How long the device took is the measurement — it is what tells a thermostat that
    answered at once from one that never answered — and a wait that always runs to its
    deadline throws that away while making the run minutes longer than it needs.
    """
    device = _device(unit)
    await device.async_refresh()
    ctx = Context(device, settle=0, max_wait=30.0, poll=0)

    calls = 0

    def ready() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    waited = await ctx.until(ready)
    device.close()

    # It came back on the third look rather than after thirty seconds.
    assert waited is not None
    assert waited < 1.0


async def test_a_wait_that_times_out_says_so_rather_than_passing(unit):
    """A deadline that succeeded on expiry would make every thermal check pass."""
    device = _device(unit)
    await device.async_refresh()
    ctx = Context(device, settle=0, max_wait=0, poll=0)
    assert await ctx.until(lambda: False) is None
    device.close()


async def test_a_wait_gives_the_device_at_least_one_look(unit):
    """A max wait of 0 must still poll once, or every unit test would time out."""
    device = _device(unit)
    await device.async_refresh()
    ctx = Context(device, settle=0, max_wait=0, poll=0)
    looks = 0

    def ready() -> bool:
        nonlocal looks
        looks += 1
        return looks > 1

    assert await ctx.until(ready) is not None
    device.close()


async def test_the_size_of_the_step_travels_with_the_result(unit):
    """The number that establishes what worktime's units actually are."""
    _, results = await _run(unit)
    runtime = _one(results, "the_runtime_counter_is_monotonic")
    assert runtime.outcome == "pass"
    assert runtime.measured["step"] >= 1
    assert runtime.measured["seconds_of_running_observed"] is not None


async def test_the_measured_numbers_travel_with_a_passing_result(unit):
    """Pinning down a number the table guesses at is the main reason to run this."""
    _, results = await _run(unit)
    runtime = _one(results, "the_runtime_counter_is_monotonic")
    assert runtime.outcome == "pass"
    assert (
        runtime.measured["work_time_now"]
        > runtime.measured["work_time_when_the_run_began"]
    )

    rssi = _one(results, "base_info_reports_a_signal_strength")
    assert rssi.measured["rssi"] == 44


# --- liveness -----------------------------------------------------------------------


async def test_a_redundant_write_is_acknowledged_by_a_unit_that_behaves(unit):
    """A scene that names every setting must not cost a burst of state re-reads."""
    _, results = await _run(unit)
    assert _one(results, "a_no_op_command_is_still_acknowledged").outcome == "pass"


async def test_a_write_the_device_never_answers_leaves_a_field_pending():
    """The failure mode nobody has seen, which is the reason the resync timer exists.

    A unit that answers a redundant write with silence leaves the field pending, fires a
    full state re-read five seconds later, and turns a busy scene into a burst of them —
    with nothing visible to a user to explain the traffic.
    """
    _, results = await _run(FakeUnit(ignores=("muteon",)))
    noop = _one(results, "a_no_op_command_is_still_acknowledged")
    assert noop.outcome == "fail"
    assert "every redundant write" in noop.detail
    assert noop.measured["pending_after_a_no_op"] == ["mute"]


# --- what a live run found the checks themselves getting wrong ----------------------


async def test_a_dud_switch_does_not_hide_the_switches_behind_it():
    """A live run stopped at the first bad feature and never reached the rest.

    The table claimed a display switch the device ignores; the check aborted there, so
    mute went untested and the run could not say whether it worked. One run
    should name everything wrong with the table, not the first thing.
    """
    _, results = await _run(FakeUnit(ignores=("muteon", "childlockon")))
    features = _one(results, "every_claimed_feature_is_accepted")

    assert features.outcome == "fail"
    assert "mute" in features.detail
    assert "child_lock" in features.detail
    # And every claim it walked is reported, so a reader can see what was reached.
    assert features.measured["mute_accepted"] is False
    assert features.measured["child_lock_accepted"] is False
    assert features.measured["swing_vertical_accepted"] is True


async def test_a_clamped_bound_does_not_hide_the_narrowness_probe_behind_it():
    """Same bug, same run: the humidity clamp check aborted before the raw probe.

    The two halves look for opposite faults, so the first one failing is the worst time
    to skip the second.
    """
    _, results = await _run(FakeUnit(ambient_humidity=90, real_humidity_range=(20, 60)))
    humidity = _one(results, "the_humidity_range_is_accepted")

    assert humidity.outcome == "fail"
    # Too wide at the top, and too narrow at the bottom, from one run.
    assert "is the real limit" in humidity.detail
    assert "too narrow" in humidity.detail
    assert humidity.measured["humidity_29_became"] == 29


async def test_a_deferred_failure_outranks_a_later_skip():
    """A check that walks past a failure and then gives up has still found one."""
    ctx = Context(_device(FakeUnit()), settle=0, max_wait=0, poll=0)

    async def walks_then_skips(_ctx: Context) -> None:
        _ctx.expect_but_continue(condition=False, detail="the first claim was wrong")
        raise CheckSkippedError("and then there was nothing more to do")

    check = Check(
        name="walks_then_skips",
        suite="protocol",
        claim="Walking past a failure is not forgiving it",
        run=walks_then_skips,
        cost=0,
    )
    runner = SelfTest(ctx.device, settle=0, max_wait=0, poll=0)
    runner._context = ctx
    result = await runner._run_one(check)
    ctx.device.close()

    assert result.outcome == "fail"
    assert result.detail == "the first claim was wrong"


async def test_the_off_state_check_asks_the_device_rather_than_its_own_optimism():
    """A live run read back its own optimistic write and called it the device's answer.

    Both halves are about a value the library sent and the device did not mention, so
    the merged state is this library quoting itself. Only a full re-read can settle it.
    """
    transport = FakeUnit(keeps_settings_while_off=True)
    _, results = await _run(transport)
    off = _one(results, "the_off_state_keeps_the_fan_speed_it_was_given")

    assert off.outcome == "fail"
    assert "_restore is ordering its frames for no reason" in off.detail
    # There has to be a full read after the last speed the check wrote, because that is
    # the value merged state would answer from the library's own optimism.
    frames = off.trace
    last_ack = max(
        i
        for i, frame in enumerate(frames)
        if frame["cmd"] == 4 and "windlevel" in frame["result"]
    )
    assert any(frame["cmd"] == 3 for frame in frames[last_ack + 1 :])


async def test_a_unit_that_does_not_park_the_fan_is_reported_too():
    """The other half of the same claim, which the check used to measure and not assert.

    Both READMEs tell users that Low while off is the parked speed rather than a stale
    reading. A unit that holds its running speed through a power-down makes that wrong —
    but only if the reading was taken after the turn-off timer, which is why this fake
    parks never rather than late.
    """
    _, results = await _run(FakeUnit(holds_speed_zero=True, parks_fan_when_off=False))
    off = _one(results, "the_off_state_keeps_the_fan_speed_it_was_given")

    assert off.outcome == "fail"
    assert "it does not park the fan" in off.detail
