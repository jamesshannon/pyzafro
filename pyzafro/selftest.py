"""Live conformance checks, for whether a real device still does what we claim.

The unit tests prove that pyzafro builds the frames it means to. They cannot prove that
those frames still mean what they meant when the protocol was reverse engineered,
because the only authority on that is the hardware. Twice now a release has shipped on a
protocol detail that a two-minute probe would have corrected, so this module makes that
probe a thing which can be re-run rather than a script someone wrote once.

Every check goes through the ordinary public setters. That is the point: a probe that
publishes its own frames — which `pyzafro-diagnose probe` does — tests the device and
leaves the library's own write path, capability validation and optimistic
reconciliation alone. Those are exactly where the last two bugs were.

The checks are data, so they are unit-tested against a fake transport like any other
code here. A live check that has never itself been tested is a poor instrument for
catching someone else's mistakes.

**This runs the machine.** It is the full integration suite: rare, explicitly asked for,
and thorough in preference to quick or gentle. It will turn the unit on, drive the
compressor, dehumidify, and take fifteen-odd minutes over it, because the assumptions
that cost the most to get wrong — whether the thermostat really reports being satisfied,
whether a range in the table is the device's own — cannot be answered by a unit that is
not doing any work. A baseline is read first and restored afterwards, including after a
failure or an interrupt, and the restore is verified rather than hoped for.

It still cannot see the unit. Anything physical — whether the blower audibly changed,
which way a louvre moved, what happens when the plug is pulled — is for a human.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .capabilities import BinarySensorKey, Feature, SensorKey
from .const import RESYNC_DELAY
from .exceptions import ZafroUnsupportedError
from .models import (
    FIELD_TO_WIRE,
    READ_ONLY_FIELDS,
    SLEEP_FAN_SPEED,
    Mode,
    Origin,
    build_command,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable

    from .device import ZafroDevice
    from .models import DeviceState

    _CheckFunc = Callable[["Context"], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)

#: How long to let the device finish reacting before believing its state. The reactions
#: that matter arrive 0.5-1.5s after the acknowledgement — the sleep-exit speed restore
#: landed 1.1s after the command — and a programme flag the device means to refuse comes
#: back up to 6s later. Six seconds covers both. Being generous costs a slower run;
#: being tight costs a false pass.
SETTLE = 6.0

#: How long to wait, *at most*, for the machine to do something measurable. A settle is
#: long enough for the MCU to answer; nothing thermal happens on that scale. Ambient
#: temperature is reported in whole degrees, so a change has to clear a whole degree
#: before it is visible at all, and three minutes at the bottom of the setpoint range is
#: the longest that should ever be needed.
#:
#: A deadline, not a duration. Every wait ends the moment the thing it is waiting for
#: has happened, so a healthy unit spends a fraction of this and only a unit that is not
#: responding spends all of it. The first version slept the full three minutes
#: regardless, which is both slower and worse evidence: how long the change took is the
#: measurement, and a fixed sleep throws it away.
SOAK = 180.0

#: How often to look while waiting. The device pushes ambient readings on its own, but
#: on nobody's schedule, so a wait re-reads full state at this interval rather than
#: trusting a push to arrive inside the deadline.
POLL = 15.0

#: The deadline for `reachtarget`, which is a comparison the MCU makes rather than
#: something the room has to do, so it should flip within seconds of the setpoint moving
#: past ambient. Given its own limit so that a unit which never reports it does not cost
#: a full soak.
REACH_WAIT = 60.0

#: Suites, in the order they run. Named for the behaviour they exercise, never for the
#: bug that prompted them: a regression check belongs with the behaviour it protects, or
#: the list becomes an archaeology of past issues.
#:
#: `thermal` is last because it is the only one that waits on physics rather than on the
#: MCU, so an interrupted run loses the least, and because it leaves the room cold.
SUITES: tuple[str, ...] = ("fan", "protocol", "capabilities", "liveness", "thermal")


class CheckFailedError(Exception):
    """The device did not behave the way this library says it does."""


class CheckSkippedError(Exception):
    """A check that cannot say anything about this device, and the reason why."""


@dataclass(frozen=True, slots=True)
class Check:
    """One conformance check."""

    name: str
    suite: str
    #: What the library claims, in one line, phrased so a failure reads as news.
    claim: str
    run: Callable[[Context], Awaitable[None]]
    #: Settle periods this check spends, for estimating a run before starting it.
    cost: int
    #: Soak periods on top of that, for a check that waits on the machine working.
    soaks: int = 0
    #: Waits that give up after REACH_WAIT instead, for a check waiting on the MCU to
    #: compare two numbers it already has rather than on the room to change.
    reaches: int = 0


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What happened when a check ran."""

    name: str
    suite: str
    claim: str
    outcome: str  # "pass" | "fail" | "skip" | "error"
    detail: str = ""
    seconds: float = 0.0
    #: Frames seen while this check ran, so a failure arrives with its evidence.
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: What the check measured, whatever its verdict. A passing check that pinned down
    #: an unknown number — the real setpoint floor, the units of the runtime counter —
    #: is the main reason to run this at all, and a bare "pass" throws that away.
    measured: dict[str, Any] = field(default_factory=dict)


CHECKS: list[Check] = []


def _check(
    suite: str, claim: str, *, cost: int, soaks: int = 0, reaches: int = 0
) -> Callable[[_CheckFunc], _CheckFunc]:
    """Register a check, taking its name from the function's."""

    def decorate(func: _CheckFunc) -> _CheckFunc:
        CHECKS.append(
            Check(
                name=func.__name__,
                suite=suite,
                claim=claim,
                run=func,
                cost=cost,
                soaks=soaks,
                reaches=reaches,
            )
        )
        return func

    return decorate


class Context:
    """Give a check the device and the patience to observe it."""

    def __init__(
        self,
        device: ZafroDevice,
        *,
        settle: float = SETTLE,
        soak: float = SOAK,
        poll: float = POLL,
    ) -> None:
        """Wrap a device for one run. A `settle` of 0 is for unit tests only."""
        self.device = device
        self.settle_seconds = settle
        self.soak_seconds = soak
        self.poll_seconds = poll
        #: Numbers a check pinned down, reported whatever its verdict.
        self.measured: dict[str, Any] = {}
        #: Failures a check chose to finish walking past. Collected by the runner, so
        #: that recording one cannot silently become the check passing.
        self.deferred: list[str] = []

    @property
    def state(self) -> DeviceState:
        """Return the device's state as last reported."""
        return self.device.state

    @property
    def caps(self) -> Any:
        """Return the capability set under test."""
        return self.device.capabilities

    async def settle(self) -> None:
        """Wait for the device to finish reacting to what was just asked of it.

        Always yields, even at a settle of 0, because an acknowledgement cannot arrive
        while this coroutine holds the loop — which is as true of a real transport as it
        is of the fake one the unit tests use.
        """
        await asyncio.sleep(self.settle_seconds)

    async def until(
        self,
        ready: Callable[[], bool],
        *,
        timeout: float | None = None,
    ) -> float | None:
        """Wait for `ready`, and stop waiting the moment it is true.

        Returns the seconds it took, or None if the deadline passed first — so the check
        decides what a timeout means and can say how long it gave the device. How long
        the machine took is itself worth recording: it is the difference between a unit
        that is working and one that is merely not broken.

        The deadline is only consulted after at least one poll, so a `soak` of 0 — which
        is what the unit tests use — still gives the device one chance to answer rather
        than none.

        Each poll re-reads full state. The device does push ambient readings unprompted,
        but nothing guarantees one lands inside the deadline, and waiting on a push that
        never comes would fail a working unit.
        """
        deadline = self.soak_seconds if timeout is None else timeout
        started = time.monotonic()
        polled = False
        while True:
            if ready():
                return time.monotonic() - started
            elapsed = time.monotonic() - started
            if polled and elapsed >= deadline:
                return None
            polled = True
            await asyncio.sleep(max(0.0, min(self.poll_seconds, deadline - elapsed)))
            with contextlib.suppress(Exception):
                await self.device.async_refresh()

    async def command(self, awaitable: Awaitable[None]) -> None:
        """Issue one real command, then wait before believing anything about it."""
        await awaitable
        await self.settle()

    @contextlib.asynccontextmanager
    async def frames(self) -> AsyncIterator[list[dict[str, Any]]]:
        """Collect the raw frames that arrive inside this block.

        For the few claims that are about the frames themselves rather than about the
        state they add up to — which side of a change the device says caused it, most
        of all — where reading the merged state cannot answer the question.
        """
        seen: list[dict[str, Any]] = []
        unsubscribe = self.device.subscribe_raw(
            lambda _cmd, result: seen.append(result)
        )
        try:
            yield seen
        finally:
            unsubscribe()

    async def confirmed(self) -> DeviceState:
        """Re-read full state, for a claim that optimism could answer by itself.

        Most checks can read the merged state, because a settle outlasts the resync that
        would correct it. Not one about a value the library sent and the device merely
        declined to mention: there the merged view is this library quoting itself back.
        A cmd:3 reply is the device's own answer.
        """
        await self.device.async_refresh()
        return self.device.state

    def note(self, **values: Any) -> None:
        """Record what this check measured, for the report and for the table."""
        self.measured.update(values)

    def expect(self, condition: bool, detail: str) -> None:  # noqa: FBT001
        """Fail the check, saying what was expected and what the device did instead."""
        if not condition:
            raise CheckFailedError(detail)

    def expect_but_continue(self, condition: bool, detail: str) -> None:  # noqa: FBT001
        """Record a failure without abandoning the claims after it.

        For a check that walks a list — every claimed feature, both ends of a range —
        where stopping at the first bad one hides every later one behind it. A live run
        found the table claiming a display switch the device ignores, and because that
        aborted the loop the mute switch went untested; the same run never reached the
        humidity range's narrowness probe because the clamp check ahead of it failed.
        One run should name everything wrong with the table, not the first thing.

        The runner raises whatever accumulates here, so a check cannot record a failure
        and then pass by forgetting to look.
        """
        if not condition:
            self.deferred.append(detail)

    def expect_speed(self, wanted: int) -> None:
        """Assert the fan settled at `wanted`, with nothing left to override it."""
        state = self.state
        self.expect(
            state.fan_speed == wanted,
            f"asked for fan speed {wanted}, device settled at {state.fan_speed}",
        )
        for name, value in (
            ("sleep", state.sleep),
            ("extra", state.extra),
            ("eco", state.eco),
        ):
            self.expect(
                value is not True,
                f"fan speed {wanted} was accepted but {name} is still on, so the "
                f"device is free to override it",
            )

    def requires(self, *features: Feature) -> None:
        """Skip the check unless the device claims all of `features`."""
        missing = [str(f) for f in features if not self.caps.has(f)]
        if missing:
            raise CheckSkippedError(f"model does not claim {', '.join(missing)}")

    def requires_mode(self, mode: Mode) -> None:
        """Skip the check unless the device claims `mode`."""
        if mode not in self.caps.modes:
            raise CheckSkippedError(f"model has no {mode.name.lower()} mode")

    def temperature_bounds(self) -> tuple[int, int]:
        """Return the claimed setpoint range, or skip."""
        bounds: tuple[int, int] | None = self.caps.target_temperature_range
        if bounds is None:
            raise CheckSkippedError("model has no temperature setpoint")
        return bounds

    def humidity_bounds(self) -> tuple[int, int]:
        """Return the claimed humidity range, or skip."""
        bounds: tuple[int, int] | None = self.caps.target_humidity_range
        if bounds is None:
            raise CheckSkippedError("model has no humidity setpoint")
        return bounds

    def top_speed(self) -> int:
        """Return the highest speed this model offers."""
        return max(self._speeds())

    def slowest_speed(self) -> int:
        """Return the lowest selectable speed."""
        return min(self._speeds())

    def _speeds(self) -> list[int]:
        speeds = [s for s in self.caps.fan_speeds if s != SLEEP_FAN_SPEED]
        if not speeds:
            raise CheckSkippedError("model claims no fan speeds")
        return speeds


# --- the fan: one control, and which position wins -----------------------------------
#
# Where 1.2.0's mis-mapped speeds and 1.3.0's lost speed would both have been caught
# before release — but the suite is not organised around either. It walks the fan
# control's positions and the transitions between them, and those two bugs are simply
# transitions that used to be wrong.


@_check("fan", "Leaving sleep for a speed gives you that speed", cost=3)
async def a_speed_leaves_sleep_at_that_speed(ctx: Context) -> None:
    """Check the transition that produced 1.3.1.

    The device restores the pre-sleep speed as it reads `sleep: false`, so the requested
    speed has to be written after it. Entering sleep from the slowest speed and then
    asking for the fastest is the transition that makes a silent override obvious: a
    device that loses the request ends up back at the slowest.
    """
    ctx.requires(Feature.SLEEP, Feature.FAN_SPEED)
    await ctx.command(ctx.device.async_set_fan_speed(ctx.slowest_speed()))
    await ctx.command(ctx.device.async_set_sleep(on=True))
    await ctx.command(ctx.device.async_set_fan_speed(ctx.top_speed()))
    ctx.expect_speed(ctx.top_speed())


@_check("fan", "Leaving EXTRA for a speed gives you that speed", cost=2)
async def a_speed_leaves_extra_at_that_speed(ctx: Context) -> None:
    """Check that EXTRA's own speed is not what the fan is left at."""
    ctx.requires(Feature.EXTRA, Feature.FAN_SPEED)
    await ctx.command(ctx.device.async_set_extra(on=True))
    await ctx.command(ctx.device.async_set_fan_speed(ctx.slowest_speed()))
    ctx.expect_speed(ctx.slowest_speed())


@_check("fan", "Leaving eco for a speed gives you that speed", cost=2)
async def a_speed_leaves_eco_at_that_speed(ctx: Context) -> None:
    """Check that asking for a faster speed clears the eco that would force one."""
    ctx.requires(Feature.ECO, Feature.FAN_SPEED)
    await ctx.command(ctx.device.async_set_eco(on=True))
    await ctx.command(ctx.device.async_set_fan_speed(ctx.top_speed()))
    ctx.expect_speed(ctx.top_speed())


@_check("fan", "Every speed the table offers can actually be held", cost=4)
async def every_claimed_speed_holds(ctx: Context) -> None:
    """Walk `fan_speeds`, because a speed the device undoes should not be offered.

    That list drives the whole fan-mode dropdown a consumer builds, so a value in it
    the device will not keep is a control that visibly reverts a few seconds after use.
    """
    ctx.requires(Feature.FAN_SPEED)
    for speed in ctx.caps.fan_speeds:
        await ctx.command(ctx.device.async_set_fan_speed(speed))
        ctx.expect(
            ctx.state.fan_speed == speed,
            f"fan_speeds offers {speed} but the device settled at "
            f"{ctx.state.fan_speed}; that speed should not be offered",
        )


@_check("fan", "Sleep drops the fan below every selectable speed", cost=1)
async def sleep_drops_the_fan_below_every_speed(ctx: Context) -> None:
    """Check the reason sleep is a fan mode rather than only a switch.

    If the device stopped reporting a speed nothing else can reach, `sleep` would have
    no business in a consumer's fan-mode list, and the capability-drift exemption for
    speed 0 would be hiding something instead of explaining it.
    """
    ctx.requires(Feature.SLEEP)
    await ctx.command(ctx.device.async_set_sleep(on=True))
    ctx.expect(ctx.state.sleep is True, "sleep was not accepted")
    ctx.note(sleep_fan_speed=ctx.state.fan_speed)
    ctx.expect(
        ctx.state.fan_speed not in ctx.caps.fan_speeds,
        f"sleep left the fan at {ctx.state.fan_speed}, which is a selectable speed, so "
        f"sleep is no longer a position of its own",
    )


@_check("fan", "EXTRA is a field of its own, reported with a speed", cost=1)
async def extra_is_reported_alongside_a_speed(ctx: Context) -> None:
    """Check why `extra` has to be read before `fan_speed` means anything."""
    ctx.requires(Feature.EXTRA)
    await ctx.command(ctx.device.async_set_extra(on=True))
    ctx.expect(ctx.state.extra is True, "EXTRA was not accepted")
    ctx.note(extra_fan_speed=ctx.state.fan_speed)
    ctx.expect(
        ctx.state.fan_speed is not None,
        "EXTRA is on but the device reports no fan speed, so reading extra before "
        "fan_speed is no longer necessary — check what a consumer should show",
    )


@_check("fan", "Sleep and EXTRA are mutually exclusive", cost=3)
async def the_fan_positions_are_exclusive(ctx: Context) -> None:
    """Check that two positions of one control cannot both be on."""
    ctx.requires(Feature.SLEEP, Feature.EXTRA)
    await ctx.command(ctx.device.async_set_sleep(on=True))
    await ctx.command(ctx.device.async_set_extra(on=True))
    ctx.expect(
        ctx.state.sleep is not True,
        "EXTRA was selected and sleep stayed on; they are not one control after all",
    )
    await ctx.command(ctx.device.async_set_sleep(on=True))
    ctx.expect(
        ctx.state.extra is not True,
        "sleep was selected and EXTRA stayed on; they are not one control after all",
    )


@_check("fan", "The speed sleep reports cannot be asked for directly", cost=0)
async def the_sleep_speed_is_not_selectable(ctx: Context) -> None:
    """Check that the library refuses the one speed the device will not hold."""
    ctx.requires(Feature.FAN_SPEED)
    if SLEEP_FAN_SPEED in ctx.caps.fan_speeds:
        raise CheckSkippedError("this model offers speed 0 as a real speed")
    try:
        await ctx.device.async_set_fan_speed(SLEEP_FAN_SPEED)
    except ZafroUnsupportedError:
        return
    raise CheckFailedError(
        f"the library accepted fan speed {SLEEP_FAN_SPEED}, which the device does not "
        f"hold; it should have been refused"
    )


@_check("fan", "The device will not hold the speed sleep reports", cost=2)
async def the_sleep_speed_is_refused_by_the_device(ctx: Context) -> None:
    """Check the measurement behind refusing speed 0, rather than the refusal.

    `the_sleep_speed_is_not_selectable` checks that the library says no. This checks
    that the library is right to, which is the part that ships as a claim about the
    hardware: commanded directly, speed 0 is acknowledged and then undone about five
    seconds later.

    Sent raw, because the library refuses it — and raw sends apply nothing
    optimistically, so whatever the state says afterwards is the device's own answer.
    The last version of this measurement was taken with the unit off, where every speed
    reverts, and the conclusion survived a re-run only by luck. Hence a check.
    """
    ctx.requires(Feature.FAN_SPEED)
    if SLEEP_FAN_SPEED in ctx.caps.fan_speeds:
        raise CheckSkippedError("this model offers speed 0 as a real speed")
    ctx.expect(
        ctx.state.power is True,
        "the unit is off, where it reverts every speed; this measurement would mean "
        "nothing",
    )
    held = ctx.slowest_speed()
    await ctx.command(ctx.device.async_set_fan_speed(held))
    await ctx.device.async_send_raw({FIELD_TO_WIRE["fan_speed"]: SLEEP_FAN_SPEED})
    await ctx.settle()
    ctx.note(speed_after_commanding_zero=ctx.state.fan_speed)
    ctx.expect(
        ctx.state.fan_speed != SLEEP_FAN_SPEED,
        f"the device held fan speed {SLEEP_FAN_SPEED} when asked for it directly, so "
        f"it is a real speed after all and belongs in fan_speeds",
    )


# --- protocol: the things the device does that we did not ask for ---------------------
#
# Every claim in this suite is one a consumer has been told to expect, in a README or a
# docstring, on the strength of a single observation. They are cheap to check and they
# are the ones that quietly stop being true across a firmware update.


@_check("protocol", "The device says which changes it caused itself", cost=2)
async def a_commanded_change_is_reported_as_commanded(ctx: Context) -> None:
    """Check `origin`, which is how anything here tells cause from coincidence.

    `watch` labels every line with it, the reconciliation logic reads it, and the whole
    method of characterising this protocol has been to command one thing and attribute
    what followed. If the device stopped distinguishing its own pushes from
    acknowledgements, every conclusion drawn that way would need re-taking.
    """
    ctx.requires(Feature.MUTE)
    was = ctx.state.mute
    async with ctx.frames() as seen:
        await ctx.command(ctx.device.async_set_mute(on=not was))
    origins = [frame.get("origin") for frame in seen if "origin" in frame]
    ctx.note(origins_after_a_command=origins)
    await ctx.command(ctx.device.async_set_mute(on=bool(was)))
    ctx.expect(
        bool(origins),
        "the device reported no origin at all, so a change can no longer be "
        "attributed to whoever caused it",
    )
    ctx.expect(
        Origin.COMMANDED in origins,
        f"a command was acknowledged with origin {origins}, none of which is "
        f"{int(Origin.COMMANDED)}; commanded and device-originated changes are no "
        f"longer distinguishable",
    )


@_check("protocol", "Sleep mutes the beeper as well as slowing the fan", cost=2)
async def sleep_also_mutes_the_beeper(ctx: Context) -> None:
    """Check the side effect that keeps sleep a switch and not only a fan mode.

    A fan mode cannot show the beeper, so the fact that sleep touches it is the whole
    argument for exposing sleep twice. If the device stopped muting, that argument goes
    and the duplicate control is just clutter.
    """
    ctx.requires(Feature.SLEEP, Feature.MUTE)
    await ctx.command(ctx.device.async_set_mute(on=False))
    await ctx.command(ctx.device.async_set_sleep(on=True))
    ctx.note(mute_after_sleep=ctx.state.mute)
    ctx.expect(
        ctx.state.mute is True,
        "sleep was accepted but the beeper stayed on, so sleep no longer has any "
        "effect a fan mode cannot show",
    )


@_check("protocol", "eco holds a fan speed and a setpoint of its own", cost=2)
async def eco_forces_its_own_speed_and_setpoint(ctx: Context) -> None:
    """Check the documented side effects of eco, and record the values.

    A consumer is told never to guess these — only the field that was sent is applied
    optimistically, and eco's consequences arrive as pushes a moment later. That is only
    sound advice while the device really does send them.
    """
    ctx.requires(Feature.ECO)
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("eco is a cooling programme; this needs cool mode")
    await ctx.command(ctx.device.async_set_eco(on=True))
    ctx.expect(ctx.state.eco is True, "eco was not accepted")
    ctx.note(
        eco_fan_speed=ctx.state.fan_speed,
        eco_setpoint=ctx.state.target_temperature,
    )
    ctx.expect(
        ctx.state.fan_speed is not None and ctx.state.target_temperature is not None,
        "eco is on but the device reported neither a speed nor a setpoint with it; the "
        "side effects a consumer is told to wait for are not arriving",
    )


@_check("protocol", "EXTRA moves the setpoint, and inside the claimed range", cost=2)
async def extra_moves_the_setpoint_within_the_claimed_range(ctx: Context) -> None:
    """Check EXTRA's setpoint side effect, and take a free reading of the floor.

    Setting `extra` makes the device choose a setpoint for itself — 61 was observed
    once. Two things are worth knowing. That it still happens at all, because a
    consumer is told to expect the setpoint to move on its own and not to guess the
    value. And what the value is, because a setpoint the device picks unprompted must be
    one it accepts, so a value outside `target_temperature_range` proves that range
    wrong without anything else having to be measured.

    Deliberately not asserting that it equals the floor. It was observed at 61 against a
    claimed floor of 60, and the device choosing a degree off its own limit is not
    evidence of anything; the range containing it is what can be checked.
    """
    ctx.requires(Feature.EXTRA)
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("EXTRA is a cooling programme; this needs cool mode")
    before = ctx.state.target_temperature
    await ctx.command(ctx.device.async_set_extra(on=True))
    forced = ctx.state.target_temperature
    ctx.note(setpoint_before_extra=before, setpoint_under_extra=forced, table_floor=low)
    try:
        ctx.expect(
            forced is not None,
            "EXTRA is on and the device reports no setpoint; a consumer told to wait "
            "for that side effect will wait forever",
        )
        ctx.expect(
            forced is not None and low <= forced <= high,
            f"EXTRA drove the setpoint to {forced}, outside the table's {low}-{high}; "
            f"a value the device picks for itself is one it accepts, so the table is "
            f"wrong",
        )
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))


@_check("protocol", "Sleep is refused in fan mode", cost=4)
async def sleep_is_refused_in_fan_mode(ctx: Context) -> None:
    """Check the documented non-bug, because users are told to expect it.

    In fan mode the device acknowledges a sleep command and then switches the setting
    straight back off, which looks exactly like a dropped write. Both READMEs promise it
    is the unit and not the integration; this is what keeps that promise honest.

    It doubles as the only live exercise of reconciliation that does not need a fault
    injected: the library applies the setting optimistically and has to end up back at
    the device's answer without anybody asking it to.

    Both READMEs used to extend that promise to EXTRA and eco, on the reasoning that all
    three are cooling programmes. A live run refused it: on firmware 1.0.29 fan mode
    accepted and kept both. Not unreasonable of it — EXTRA is the top of the fan
    control and eco forces the bottom of it, and both of those mean something with no
    compressor involved — but it does mean the grouping was a guess about the device's
    reasoning rather than an observation. So only sleep is asserted here. What the other
    two do is recorded, because a reader of this output deserves the number, and left
    without a verdict, because nothing in this library depends on the answer and a
    firmware that changed its mind either way would not be a bug in it.
    """
    ctx.requires_mode(Mode.FAN)
    was_mode = ctx.state.mode
    if was_mode is None:
        raise CheckSkippedError("device has not reported a mode")
    programmes: tuple[tuple[Feature, str, Callable[[], Awaitable[None]]], ...] = (
        (Feature.SLEEP, "sleep", lambda: ctx.device.async_set_sleep(on=True)),
        (Feature.EXTRA, "extra", lambda: ctx.device.async_set_extra(on=True)),
        (Feature.ECO, "eco", lambda: ctx.device.async_set_eco(on=True)),
    )
    await ctx.command(ctx.device.async_set_mode(Mode.FAN))
    try:
        kept = {}
        for feature, name, setter in programmes:
            if not ctx.caps.has(feature):
                continue
            await ctx.command(setter())
            kept[name] = getattr(ctx.state, name)
        ctx.note(**{f"{name}_in_fan_mode": value for name, value in kept.items()})
        if "sleep" in kept:
            ctx.expect(
                kept["sleep"] is not True,
                "fan mode accepted and kept sleep, which both READMEs say it refuses; "
                "a consumer could offer that control in fan mode after all",
            )
    finally:
        if was_mode is not Mode.FAN:
            await ctx.command(ctx.device.async_set_mode(was_mode))


@_check("protocol", "A unit that is off keeps the fan speed it was given", cost=4)
async def the_off_state_keeps_the_fan_speed_it_was_given(ctx: Context) -> None:
    """Check the two halves of what an off unit does with a speed, both now documented.

    Both were assumed the other way round until a live run, and both READMEs said so:
    that the fan parks at its slowest speed when the unit is powered down, and that a
    setting written to a unit that is off is discarded. `_restore` was built on the
    second, sending settings before power when the unit was found off, because the other
    order would silently lose them.

    Powered down at its top speed the unit reported the top speed, and a speed written
    while off was acknowledged and never corrected. So the claim is the other way up
    now, and this is what asserts the version the READMEs tell users: the fan does not
    park, and a write while off sticks. A unit that parks or discards is a firmware this
    documentation is wrong about, and it makes `_restore`'s ordering load-bearing again
    rather than the precaution it was demoted to.

    The speed written while off has to be a different speed from the one the unit is
    already at, which the first version of this check got wrong. It wrote the top speed
    to a unit already sitting at the top speed, so the read-back was the same number
    whether the write landed or was thrown away, and the check reported "settings do
    stick after all" on evidence that could not distinguish the two.

    Read from a `cmd:3` reply for the same reason the feature walk is: these are values
    the library sent and the device did not mention, so merged state would answer with
    this library's own optimism.
    """
    ctx.requires(Feature.FAN_SPEED)
    top, other = ctx.top_speed(), ctx.slowest_speed()
    if top == other:
        raise CheckSkippedError(
            f"the model offers only speed {top}, so a speed written while off "
            f"cannot be told apart from the one already set"
        )
    was_power = ctx.state.power
    await ctx.command(ctx.device.async_set_fan_speed(top))
    await ctx.command(ctx.device.async_set_power(on=False))
    parked = (await ctx.confirmed()).fan_speed
    ctx.note(fan_speed_while_off=parked)
    try:
        await ctx.command(ctx.device.async_set_fan_speed(other))
        kept = (await ctx.confirmed()).fan_speed
        ctx.note(fan_speed_written_while_off=other, fan_speed_read_back_while_off=kept)
        ctx.expect_but_continue(
            parked == top,
            f"the unit was powered down at speed {top} and reports {parked}; it "
            f"parks the fan, so both READMEs are wrong to tell users that a speed "
            f"shown while off is whatever it was last set to",
        )
        ctx.expect_but_continue(
            kept == other,
            f"speed {other} was written to a unit that is off and it reads back "
            f"{kept}; the write was discarded, so _restore has to keep sending "
            f"settings before power and that ordering is a requirement, not a "
            f"precaution",
        )
    finally:
        if was_power:
            await ctx.command(ctx.device.async_set_power(on=True))


@_check("protocol", "A read-only field cannot be commanded", cost=0)
async def a_read_only_field_is_refused(ctx: Context) -> None:
    """Check that the library will not try to write what the device only reports.

    Ambient readings, the runtime counter, the fault code and the temperature unit are
    all reported and none is settable. The device would ignore the field silently, so
    the library raises instead — and that refusal is the only thing standing between a
    consumer and a control that appears to work and does nothing.

    Walks `READ_ONLY_FIELDS` rather than a list of its own, so a field added to that set
    without a guard behind it is caught here instead of being trusted.
    """
    sent = []
    for name in sorted(READ_ONLY_FIELDS):
        try:
            await ctx.device._async_command(**{name: 1})  # noqa: SLF001
        except ZafroUnsupportedError:
            continue
        sent.append(name)
    ctx.expect(
        not sent,
        f"the library sent {', '.join(sent)}, which the device only reports; those "
        f"writes would be silently ignored",
    )


@_check("protocol", "The setpoint survives a trip through another mode", cost=4)
async def the_setpoint_survives_a_trip_through_fan_mode(ctx: Context) -> None:
    """Check that a mode the setpoint does not apply to does not destroy it.

    The climate entity shows `target_temperature` whenever the device reports one,
    without caring which mode produced it. If a trip through fan mode zeroed or moved
    the setpoint, the number a consumer shows on returning to cool would be wrong, and
    nothing in the library would notice.
    """
    ctx.requires_mode(Mode.FAN)
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("this needs to start in cool mode")
    wanted = max(low, high - 2)
    await ctx.command(ctx.device.async_set_target_temperature(wanted))
    await ctx.command(ctx.device.async_set_mode(Mode.FAN))
    during = ctx.state.target_temperature
    await ctx.command(ctx.device.async_set_mode(Mode.COOL))
    after = ctx.state.target_temperature
    ctx.note(setpoint_in_fan_mode=during, setpoint_on_return=after)
    try:
        ctx.expect(
            after == wanted,
            f"the setpoint was {wanted} before fan mode and {after} after it, so the "
            f"value a consumer shows on returning to cool is not the one the user set",
        )
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))


# --- capabilities: is the table still describing this device? ------------------------


@_check("capabilities", "This model is described by the capability table", cost=0)
async def the_model_is_in_the_capability_table(ctx: Context) -> None:
    """Check the premise of every other capability check."""
    ctx.expect(
        ctx.caps.known_model,
        f"model {ctx.device.model!r} is not in the table, so every capability below is "
        f"a fallback guess rather than a claim",
    )


@_check("capabilities", "The device reports nothing this library cannot read", cost=0)
async def the_device_reports_no_unreadable_values(ctx: Context) -> None:
    """Check for a modelled key carrying a value that cannot be decoded.

    Worse than an unknown key, which merely goes unsurfaced: a rejected value leaves the
    field at its last reading, so a consumer shows something stale as current. It is the
    one anomaly the library warns about, so a run that trips it should not pass quietly.
    """
    unreadable = ctx.device.diagnostics()["anomalies"]["unreadable_keys"]
    ctx.expect(
        not unreadable,
        f"the device reported values this version cannot read: {unreadable}",
    )


@_check("capabilities", "Every sensor the table claims has reported a value", cost=0)
async def every_claimed_sensor_has_a_value(ctx: Context) -> None:
    """Check that a claimed sensor is not permanently unknown.

    A sensor in the table the device never reports builds an entity that shows nothing,
    forever, with no error anywhere to explain it — the worst kind of wrong, because it
    looks like a broken integration rather than a wrong table. This runs late enough
    that everything the device has to say has been said.
    """
    state = ctx.state
    silent = sorted(
        str(sensor)
        for sensor in ctx.caps.sensors
        if sensor is not SensorKey.RSSI and getattr(state, str(sensor), None) is None
    )
    silent += sorted(
        str(sensor)
        for sensor in ctx.caps.binary_sensors
        if sensor is not BinarySensorKey.PROBLEM
        and getattr(state, str(sensor), None) is None
    )
    ctx.note(silent_sensors=silent)
    ctx.expect(
        not silent,
        f"the table claims {', '.join(silent)} but this device has never reported "
        f"{'them' if len(silent) > 1 else 'it'}; those entities will never have a "
        f"value",
    )


@_check("capabilities", "Every feature the table claims is actually accepted", cost=6)
async def every_claimed_feature_is_accepted(ctx: Context) -> None:
    """Toggle each claimed boolean, because a claim builds a control.

    Only the plain booleans are exercised here. The fan suite covers the positions of
    the fan control, which interact and need their own transitions.

    Read back from a full re-read rather than from merged state. A device that ignores
    the key answers nothing at all, so the merged view holds the library's own
    optimistic write until the resync corrects it — which means reading merged state
    tests this check's settle against RESYNC_DELAY rather than the device. A live run
    caught an ignored `lighton` only because the default settle happens to be the longer
    of the two; at `--settle 3` it would have passed a dud switch.
    """
    switches: tuple[tuple[Feature, str, Callable[[bool], Awaitable[None]]], ...] = (
        (
            Feature.SWING_VERTICAL,
            "swing_vertical",
            lambda on: ctx.device.async_set_swing(vertical=on),
        ),
        (
            Feature.SWING_HORIZONTAL,
            "swing_horizontal",
            lambda on: ctx.device.async_set_swing(horizontal=on),
        ),
        (
            Feature.CHILD_LOCK,
            "child_lock",
            lambda on: ctx.device.async_set_child_lock(on=on),
        ),
        (Feature.DISPLAY, "display", lambda on: ctx.device.async_set_display(on=on)),
        (Feature.MUTE, "mute", lambda on: ctx.device.async_set_mute(on=on)),
    )
    for feature, name, setter in switches:
        if not ctx.caps.has(feature):
            continue
        was = getattr(ctx.state, name)
        await ctx.command(setter(not was))
        got = getattr(await ctx.confirmed(), name)
        ctx.note(**{f"{name}_accepted": got is not was})
        ctx.expect_but_continue(
            got is not was,
            f"the table claims {feature}, but setting {name} to {not was} left it at "
            f"{got}; the control it builds does nothing",
        )
        await ctx.command(setter(bool(was)))


@_check("capabilities", "Both ends of the setpoint range are accepted", cost=3)
async def the_setpoint_range_is_accepted(ctx: Context) -> None:
    """Command both ends of `target_temperature_range`, which ships as a guess.

    A bound the device clamps is a slider whose end does nothing, and the value it
    clamps to is the real limit — which is the number the table should carry. The bottom
    of the range is below any room worth cooling, so this starts the compressor; that is
    what this suite is for. The satisfied setpoint goes back straight afterwards so the
    excursion lasts one settle rather than the rest of the run.
    """
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("the temperature setpoint only applies in cool mode")
    try:
        for wanted in (high, low):
            await ctx.command(ctx.device.async_set_target_temperature(wanted))
            got = ctx.state.target_temperature
            ctx.note(**{f"setpoint_{wanted}_became": got})
            ctx.expect_but_continue(
                got == wanted,
                f"the table offers {wanted} but the device clamped it to {got}; "
                f"{got} is the real limit",
            )
    finally:
        # Back to satisfied before the next check, whatever the verdict.
        await ctx.command(ctx.device.async_set_target_temperature(high))


@_check("capabilities", "The setpoint range is not narrower than the device's", cost=4)
async def the_setpoint_range_is_not_too_narrow(ctx: Context) -> None:
    """Ask for one degree past each end, which only a raw frame can do.

    Every other check here can find a range that is too *wide*, because the library
    offers a value and the device clamps it. None can find one that is too narrow: the
    library refuses out-of-range values before they reach the wire, so a device happily
    accepting 58 would never be asked and the user would simply never be offered it.

    Raw, therefore, and deliberately so — this is the one place where bypassing
    validation is the measurement rather than a shortcut. Nothing is applied
    optimistically for a raw send, so what comes back is the device's own answer.
    """
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("the temperature setpoint only applies in cool mode")
    wire = FIELD_TO_WIRE["target_temperature"]
    accepted = []
    try:
        for beyond in (low - 1, high + 1):
            await ctx.device.async_send_raw({wire: beyond})
            await ctx.settle()
            got = ctx.state.target_temperature
            ctx.note(**{f"setpoint_{beyond}_became": got})
            if got == beyond:
                accepted.append(beyond)
        ctx.expect(
            not accepted,
            f"the device accepted {accepted}, outside the table's range of "
            f"{low}-{high}; the table is too narrow and users are being denied "
            f"settings their unit has",
        )
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))


@_check("capabilities", "The setpoint moves a degree at a time", cost=3)
async def the_setpoint_step_is_one_degree(ctx: Context) -> None:
    """Check the step a consumer's slider is built with.

    `_attr_target_temperature_step = 1` in the climate entity, on the strength of every
    observed setpoint having been a whole number. A unit that snapped to even degrees
    would give a slider that jumps under the user's finger and a state that never
    matches what they asked for, and nothing in the library would report it.
    """
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("the temperature setpoint only applies in cool mode")
    # Three consecutive values just under the top, so the thermostat stays satisfied.
    wanted = [value for value in (high - 2, high - 1, high) if value >= low]
    try:
        for value in wanted:
            await ctx.command(ctx.device.async_set_target_temperature(value))
            got = ctx.state.target_temperature
            ctx.expect(
                got == value,
                f"asked for {value} and the device settled at {got}, so consecutive "
                f"degrees are not all reachable and a step of 1 is wrong",
            )
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))


@_check("capabilities", "The humidity range the table offers is accepted", cost=6)
async def the_humidity_range_is_accepted(ctx: Context) -> None:
    """Walk `target_humidity_range`, the least evidenced thing in the whole table.

    Two values have ever been observed, 30 and 50, against a shipped range of 30-80 that
    is otherwise invented. Reaching it means dry mode, and dry mode dehumidifies; that
    is the price of the number being real. It also probes one step past each end, for
    the same reason the setpoint check does — a range that is too narrow is invisible
    from inside the library.
    """
    low, high = ctx.humidity_bounds()
    ctx.requires_mode(Mode.DRY)
    was_mode = ctx.state.mode
    wire = FIELD_TO_WIRE["target_humidity"]

    await ctx.command(ctx.device.async_set_mode(Mode.DRY))
    try:
        for wanted in (low, high):
            await ctx.command(ctx.device.async_set_target_humidity(wanted))
            got = ctx.state.target_humidity
            ctx.note(**{f"humidity_{wanted}_became": got})
            ctx.expect_but_continue(
                got == wanted,
                f"the table offers {wanted} but the device clamped it to {got}; "
                f"{got} is the real limit",
            )
        accepted = []
        for beyond in (low - 1, high + 1):
            await ctx.device.async_send_raw({wire: beyond})
            await ctx.settle()
            ctx.note(**{f"humidity_{beyond}_became": ctx.state.target_humidity})
            if ctx.state.target_humidity == beyond:
                accepted.append(beyond)
        ctx.expect(
            not accepted,
            f"the device accepted humidity {accepted}, outside the table's "
            f"{low}-{high}; the table is too narrow",
        )
    finally:
        if was_mode is not None and was_mode is not Mode.DRY:
            await ctx.command(ctx.device.async_set_mode(was_mode))


@_check("capabilities", "Every mode the table claims is accepted", cost=4)
async def every_claimed_mode_is_accepted(ctx: Context) -> None:
    """Check `modes`, which decides the whole HVAC-mode list a consumer builds.

    A mode in the table the device refuses is an option that silently snaps back. Heat
    has never been seen on any unit and is in the enum on the strength of the app's own
    label switch, so a model claiming it is exactly what wants checking.
    """
    was = ctx.state.mode
    if was is None:
        raise CheckSkippedError("device has not reported a mode")
    try:
        for mode in sorted(ctx.caps.modes):
            await ctx.command(ctx.device.async_set_mode(mode))
            got = ctx.state.mode
            ctx.note(**{f"mode_{mode.name.lower()}_became": got and got.name.lower()})
            ctx.expect(
                got is mode,
                f"the table claims {mode.name.lower()} mode but the device settled in "
                f"{got.name.lower() if got else got}; that option does nothing",
            )
    finally:
        await ctx.command(ctx.device.async_set_mode(was))


@_check("capabilities", "A setpoint the mode does not use is refused", cost=0)
async def the_wrong_setpoint_for_the_mode_is_refused(ctx: Context) -> None:
    """Check `_require_setpoint`, which rests on three captured schedules.

    The device ignores the wrong setpoint rather than reporting an error, which is why
    the library raises instead of sending it: a silent no-op is the worst outcome.
    """
    mode = ctx.state.mode
    if mode is None:
        raise CheckSkippedError("device has not reported a mode")
    if mode not in {Mode.COOL, Mode.DRY}:
        raise CheckSkippedError(f"{mode.name.lower()} mode uses neither setpoint")
    if mode is Mode.COOL:
        unused, setter = "target_humidity", ctx.device.async_set_target_humidity
        bounds = ctx.caps.target_humidity_range
    else:
        unused, setter = "target_temperature", ctx.device.async_set_target_temperature
        bounds = ctx.caps.target_temperature_range
    if bounds is None:
        raise CheckSkippedError(f"model has no {unused}")
    try:
        await setter(bounds[0])
    except ZafroUnsupportedError:
        return
    raise CheckFailedError(
        f"the library sent {unused} in {mode.name.lower()} mode, where the device "
        f"ignores it silently"
    )


@_check("capabilities", "Some assumptions cannot be tested from here at all", cost=0)
async def some_assumptions_cannot_be_checked_at_all(ctx: Context) -> None:
    """Report what even a full run does not cover, by always skipping.

    A run that reports nothing but passes invites the reading that every assumption has
    been verified. These never can be from here, and saying so is cheaper than someone
    later discovering the silence meant nothing.

    `TemperatureUnit.CELSIUS = 0` is inferred from Fahrenheit being 1 and has never been
    observed. `tempunit` is modelled read-only, so the library cannot command it and the
    assumption is unfalsifiable by this tool.

    The `wrong` fault-code vocabulary has only ever been seen as 0, and a fault cannot
    be induced on demand — which is why it ships as a raw diagnostic sensor rather than
    a decoded one. `waterlevel` and `filterthr` are the same: reported, never settable,
    and only a full tank or a dirty filter would move them.

    Availability is the other gap. It comes from the MQTT last-will topic, so testing it
    means pulling the plug out; and which louvre `oscset1` moves can only be settled by
    watching the unit. Both are jobs for a human standing next to it.

    Whether the machine cools, and whether it removes water in dry mode, are the two
    that went on this list after live runs, and both for the same reason. Each ambient
    reading alternates between two adjacent integers about a second apart, so the noise
    is twice the smallest change a check could look for. The humidity check failed
    against a unit that was dehumidifying. The cooling check passed in 0.0 seconds on
    one run, catching the reading on its way down from 83 to 81, and then failed after a
    full three minutes on the next, same unit and same room. Whether `rh` and
    `temperature` are the room rather than some reading inside the machine is
    unanswerable for exactly the same reason.

    What this library actually claims about those two modes is which setpoint each one's
    thermostat watches and in which direction, and both are tested without the room, in
    `cool_mode_is_the_mode_that_cools` and
    `dry_mode_regulates_humidity_not_temperature`.
    """
    unit = ctx.state.temperature_unit
    ctx.note(temperature_unit=unit and unit.name.lower())
    raise CheckSkippedError(
        "not covered: the Celsius mapping (tempunit is read-only, this unit reports "
        f"{unit.name.lower() if unit else unit}); the fault-code vocabulary and the "
        "water/filter readings (cannot be induced); availability (needs the plug "
        "pulled); which axis oscset1 moves (needs eyes on the louvres); whether the "
        "machine actually cools or removes any water, and whether the ambient readings "
        "are the room (each one's flap between two adjacent integers is larger than "
        "anything one run can measure)"
    )


# --- liveness: the assumptions behind optimistic writes -----------------------------
#
# Deliberately no induced transport failures. Dropping the socket to watch the reconnect
# is worth testing, but against a real broker it is slow, hard to arrange honestly, and
# the failure it induces is the transport's rather than the device's. The unit tests
# cover that path with a fake, deterministically.


@_check("liveness", "A command is acknowledged well inside the resync window", cost=2)
async def a_command_is_acknowledged_before_the_resync(ctx: Context) -> None:
    """Measure the margin the whole optimistic-write design rests on.

    If acknowledgements arrived near RESYNC_DELAY, every write would trigger a full
    state re-read and the device would spend the day answering cmd:3.
    """
    ctx.requires(Feature.MUTE)
    was = ctx.state.mute
    started = time.monotonic()
    await ctx.command(ctx.device.async_set_mute(on=not was))
    elapsed = time.monotonic() - started - ctx.settle_seconds
    ctx.note(ack_seconds=round(elapsed, 2), resync_delay=RESYNC_DELAY)
    await ctx.command(ctx.device.async_set_mute(on=bool(was)))
    ctx.expect(
        elapsed < RESYNC_DELAY,
        f"the acknowledgement took {elapsed:.1f}s, against a resync delay of "
        f"{RESYNC_DELAY}s; writes will be forcing a full re-read",
    )


@_check("liveness", "An acknowledged command leaves nothing pending", cost=2)
async def an_acknowledged_command_clears_the_pending_set(ctx: Context) -> None:
    """Check the invariant that stops every command being chased by a full re-read.

    Reaches into `_pending` deliberately: it is the mechanism under test, and there is
    no observable substitute short of waiting RESYNC_DELAY for a cmd:3 to appear.
    """
    ctx.requires(Feature.MUTE)
    was = ctx.state.mute
    await ctx.command(ctx.device.async_set_mute(on=not was))
    pending = set(ctx.device._pending)  # noqa: SLF001
    await ctx.command(ctx.device.async_set_mute(on=bool(was)))
    ctx.expect(
        not pending,
        f"the device acknowledged the write but {sorted(pending)} stayed pending, so a "
        f"full state re-read will fire {RESYNC_DELAY}s after every command",
    )


@_check("liveness", "A command that changes nothing is still acknowledged", cost=2)
async def a_no_op_command_is_still_acknowledged(ctx: Context) -> None:
    """Check the case the resync timer was never meant to fire on.

    Setting a field to the value it already has is the commonest write a home-automation
    system makes — a scene that names every setting, an automation that runs twice. If
    the device answers those with silence, each one leaves a field pending, fires a full
    cmd:3 five seconds later, and a busy scene turns into a burst of state re-reads.

    Nothing about that is visible to a user, which is why it wants checking rather than
    waiting to be reported.
    """
    ctx.requires(Feature.MUTE)
    was = ctx.state.mute
    if was is None:
        raise CheckSkippedError("the device has not reported the beeper's state")
    await ctx.command(ctx.device.async_set_mute(on=was))
    pending = sorted(ctx.device._pending)  # noqa: SLF001
    ctx.note(pending_after_a_no_op=pending)
    ctx.expect(
        not pending,
        f"writing the value the field already had left {pending} pending, so every "
        f"redundant write costs a full state re-read {RESYNC_DELAY}s later",
    )


@_check("liveness", "An overridden command ends up at the device's answer", cost=3)
async def an_overridden_command_ends_up_at_the_devices_answer(ctx: Context) -> None:
    """Check that the device wins, using the one override it can be relied on to make.

    Optimistic writes mean the library briefly believes something the device has not
    agreed to. Everything rests on it giving that belief up: `_apply_optimistic` assumes
    a contradicted field arrives as a push, and the resync timer is the backstop if it
    does not.

    Commanding a speed while EXTRA is on is not an override — the library clears EXTRA
    in the same frame. Asking for sleep in fan mode is: it is accepted, applied
    optimistically, and then refused by the device. Which is exactly the shape of the
    bug that produced 1.3.1, seen from the reconciliation side.
    """
    ctx.requires(Feature.SLEEP)
    ctx.requires_mode(Mode.FAN)
    was_mode = ctx.state.mode
    if was_mode is None:
        raise CheckSkippedError("device has not reported a mode")
    await ctx.command(ctx.device.async_set_mode(Mode.FAN))
    try:
        await ctx.device.async_set_sleep(on=True)
        optimistic = ctx.state.sleep
        await ctx.settle()
        settled = ctx.state.sleep
        ctx.note(sleep_optimistic=optimistic, sleep_settled=settled)
        ctx.expect(
            optimistic is True,
            f"the library did not apply sleep optimistically at all (it read "
            f"{optimistic}), so this check is measuring nothing",
        )
        ctx.expect(
            settled is not True,
            "fan mode kept sleep on, so there is no override here to reconcile; check "
            "sleep_is_refused_in_fan_mode for what changed",
        )
    finally:
        if was_mode is not Mode.FAN:
            await ctx.command(ctx.device.async_set_mode(was_mode))


@_check("liveness", "A full re-read agrees with the merged deltas", cost=0)
async def a_full_refresh_agrees_with_the_merged_deltas(ctx: Context) -> None:
    """Check the delta merge itself, which nothing else here would notice drifting.

    Every field a consumer shows comes from merging cmd:4 deltas into a state last fully
    read minutes or hours ago. Each other check reads back the field it just wrote, so a
    merge that quietly lost a field would pass all of them.
    """
    before = ctx.state
    await ctx.device.async_refresh()
    after = ctx.state
    drifted = [
        f"{name} {getattr(before, name)} -> {getattr(after, name)}"
        for name in ("power", "mode", "fan_speed", "sleep", "eco", "extra", "mute")
        if getattr(before, name) != getattr(after, name)
    ]
    ctx.expect(
        not drifted,
        "a full re-read disagreed with the accumulated deltas: " + "; ".join(drifted),
    )


@_check("liveness", "Base info carries a signal strength", cost=0)
async def base_info_reports_a_signal_strength(ctx: Context) -> None:
    """Check the only base-info field that moves, and the only reason it is re-read."""
    await ctx.device.async_refresh_base_info()
    info = ctx.device.base_info
    ctx.expect(info is not None, "the device returned no base info")
    ctx.note(rssi=info and info.rssi, firmware=info and info.firmware)
    ctx.expect(
        info is not None and info.rssi is not None,
        "base info carried no rssi, so the signal sensor will never have a value",
    )


# --- thermal: the claims that cannot be checked without letting the unit run ---------
#
# Not a test of the machine. Nothing here asks whether the compressor is in good repair,
# or how well it cools, or whether the unit needs servicing — those are facts about
# somebody's hardware and none of this library's business. What is this library's
# business is that two of its central mappings were never verified against anything: the
# `Mode` values were read off the app's own label switch, and `worktime` ships with a
# state class that promises Home Assistant a monotonic statistic.
#
# Those claims happen to be unfalsifiable from a unit with a satisfied thermostat, which
# is the state every other suite arranges. So this is the one suite that leaves the
# thermostat unsatisfied, and the whole of why it is allowed to run the compressor.
#
# What it does not do is read the room. Every version of that was tried and none of it
# worked: both ambient readings alternate between two adjacent integers about a second
# apart, so the instrument's noise is twice the smallest change a check could look for,
# and two runs of one check on one unit disagreed. What these mappings actually claim is
# which setpoint a mode's thermostat compares against and in which direction, and the
# MCU answers that in a second from two numbers it already holds. Whether the machine
# cools or dries is a fact about somebody's appliance, and is reported as uncovered
# rather than guessed at.
#
# Each check still waits, so this is where the minutes go — but every wait ends as soon
# as it has its answer, and none of them waits on physics.


@_check(
    "thermal",
    "The thermostat reports whether it has reached the target",
    cost=4,
    reaches=1,
)
async def reached_target_follows_the_setpoint(ctx: Context) -> None:
    """Check whether the binary sensor this library ships is a sensor at all.

    `reachtarget` is read-only and gets a binary sensor of its own. Only one value has
    ever been seen, because every observation so far was taken with the thermostat
    satisfied — so "reached target" is a name this library gave a field on the strength
    of never having watched it change. If it does not change when the setpoint crosses
    ambient, the entity is a constant with a label on it and should not be built.

    Incidentally the cheapest evidence that `templevel` and `temperature` are what they
    are decoded as, since a flip means the MCU is comparing those two in the direction
    this library assumes.

    Both directions, because a field stuck at either value looks correct from one side.
    Given its own short deadline: this is the MCU comparing two numbers it already has,
    not the room having to change, so a unit that has not answered in a minute is not
    going to.
    """
    if BinarySensorKey.REACHED_TARGET not in ctx.caps.binary_sensors:
        raise CheckSkippedError("model does not report reached_target")
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("this needs cool mode")
    ambient = ctx.state.ambient_temperature
    if ambient is not None and not low < ambient < high:
        raise CheckSkippedError(
            f"ambient is {ambient}, outside the setpoint range {low}-{high}, so the "
            f"thermostat cannot be put on both sides of it"
        )
    try:
        await ctx.command(ctx.device.async_set_target_temperature(high))
        satisfied = ctx.state.reached_target
        await ctx.command(ctx.device.async_set_target_temperature(low))
        waited = await ctx.until(
            lambda: ctx.state.reached_target != satisfied,
            timeout=min(REACH_WAIT, ctx.soak_seconds),
        )
        working = ctx.state.reached_target
        ctx.note(
            reached_target_satisfied=satisfied,
            reached_target_working=working,
            seconds_to_change=waited and round(waited, 1),
        )
        ctx.expect(
            waited is not None,
            f"reached_target read {satisfied} with the setpoint at {high} and still "
            f"reads {working} with it at {low}; it does not track the target, so the "
            f"binary sensor this library builds from it means nothing",
        )
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))


@_check(
    "thermal",
    "Mode.COOL is the mode that cools",
    cost=2,
    reaches=1,
)
async def cool_mode_is_the_mode_that_cools(ctx: Context) -> None:
    """Check `Mode.COOL = 1`, read off the app's label switch and never tested since.

    The `Mode` values came from decompiled Dart — a switch statement mapping ints to
    display strings — and every check but this one would pass with the enum shuffled.
    Commanding mode 1 and reading mode 1 back proves the device accepts the number, not
    that the number means cooling. `Mode` is what labels the entire HVAC dropdown a
    consumer builds, so a mislabelled member puts "Cool" on the button that dehumidifies
    and nothing anywhere reports an error.

    What the claim amounts to is which setpoint this mode's thermostat watches and which
    way round it watches it, and both are readable from `reachtarget` without waiting
    for a room. The dry-mode check below closes the argument. It establishes that mode 2
    ignores the temperature target entirely and watches `rhlevel`, so mode 1 is not the
    dehumidify mode wearing cool's number; and it fixes the field's polarity, because a
    unit with the room at 76% and a target of 30% read `reachtarget` 0, and an air
    conditioner has no way to add water, so 0 is "not yet" and not "done". With the
    polarity pinned, the direction is the whole
    claim: a cooling thermostat is satisfied when the target sits above the room and
    unsatisfied when it sits below, and a heating thermostat is exactly the other way
    round. Which is what this check reads, twice, from either side of ambient.

    That is a change of instrument. This check used to set the setpoint to its floor,
    put the fan flat out and wait up to three minutes for the room to fall, on the
    argument that physical consequence was the only way to read a mode number. It is
    not, and the room was a bad instrument for it: the ambient reading alternates
    between two adjacent integers about a second apart, so the noise is twice the
    smallest change the check could detect. One live run passed it in 0.0 seconds — it
    caught the reading on the way down from 83 to 81 and credited the command with it —
    and the next failed it after a full 180 seconds, on the same unit in the same room.
    A stable baseline was tried first and does not help, because a reading that
    alternates reads the same at both ends of a poll.

    Whether the machine actually removes heat is a different question, and not this
    library's: it is reported as uncovered rather than guessed at.
    """
    if BinarySensorKey.REACHED_TARGET not in ctx.caps.binary_sensors:
        raise CheckSkippedError("model does not report reached_target")
    low, high = ctx.temperature_bounds()
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("this needs cool mode")
    ambient = ctx.state.ambient_temperature
    if ambient is None or not low < ambient < high:
        raise CheckSkippedError(
            f"ambient is {ambient}, not strictly inside the setpoint range "
            f"{low}-{high}, so the thermostat cannot be put on both sides of the room"
        )
    try:
        await ctx.command(ctx.device.async_set_target_temperature(high))
        above = (await ctx.confirmed()).reached_target
        await ctx.command(ctx.device.async_set_target_temperature(low))
        waited = await ctx.until(
            lambda: ctx.state.reached_target != above,
            timeout=min(REACH_WAIT, ctx.soak_seconds),
        )
        below = ctx.state.reached_target
    finally:
        await ctx.command(ctx.device.async_set_target_temperature(high))
    ctx.note(
        ambient_temperature=ambient,
        reached_target_with_target_above_ambient=above,
        reached_target_with_target_below_ambient=below,
        seconds_to_change=waited and round(waited, 1),
    )
    if waited is None:
        raise CheckSkippedError(
            f"reached_target read {above} with the target at {high} and still reads "
            f"{below} with it at {low}, so there is no comparator here to read a "
            f"direction off; reached_target_follows_the_setpoint is the check that "
            f"claim belongs to"
        )
    ctx.expect(
        above is True and below is False,
        f"mode {int(Mode.COOL)} is decoded as cool, but its thermostat reads {above} "
        f"with the target at {high} and {below} with it at {low}, against a room at "
        f"{ambient}. Satisfied below the room and unsatisfied above it is a heating "
        f"thermostat, so mode {int(Mode.COOL)} is not cool and the whole mode list is "
        f"mislabelled",
    )


@_check(
    "thermal",
    "Mode.DRY regulates humidity, not temperature",
    cost=5,
    reaches=1,
)
async def dry_mode_regulates_humidity_not_temperature(ctx: Context) -> None:
    """Check `Mode.DRY = 2` and whether `rhlevel` drives anything at all.

    Two claims at once. The mode number has the same provenance as cool's and the same
    consequence if wrong. And `target_humidity` is a control built on the assumption
    that the device acts on it: `rhlevel` had been seen at two values, `rh` only ever
    drifting on its own, and nothing had ever connected them. If the device merely
    stores the number, the control should not exist.

    The instrument is the MCU's own comparator, not the room. Park the temperature
    setpoint where cool mode calls the thermostat satisfied and confirm it says so.
    Then change nothing about the temperature and switch to dry with a humidity target
    well below ambient. If `reachtarget` goes unsatisfied the device is comparing
    something other than the two temperatures, and `rhlevel` is its only other
    setpoint — so it reads the number this library sends, and dry is where it matters.

    This began as a three-minute wait for `rh` to fall, the wrong instrument, and a
    live run showed why: the reading flaps between two adjacent integers continuously,
    so the noise band is as large as the change being looked for, and a room reloads
    humidity while a unit removes it. It failed against a working unit. What survives of
    that question is in `some_assumptions_cannot_be_checked_at_all`; what is in scope is
    which setpoint the mode regulates, and that is answerable in a few seconds and
    answerable the same way every time.
    """
    if BinarySensorKey.REACHED_TARGET not in ctx.caps.binary_sensors:
        raise CheckSkippedError("model does not report reached_target")
    ctx.requires_mode(Mode.DRY)
    low, high = ctx.humidity_bounds()
    _tlow, thigh = ctx.temperature_bounds()
    was_mode = ctx.state.mode
    if ctx.state.mode is not Mode.COOL:
        raise CheckSkippedError("this needs to start in cool mode, with a known target")
    ambient = ctx.state.ambient_humidity
    if ambient is None:
        raise CheckSkippedError("the device reports no ambient humidity")
    if ambient <= low:
        raise CheckSkippedError(
            f"ambient humidity is {ambient}, at or below the lowest target {low}, so "
            f"the humidity target cannot be put out of reach"
        )

    await ctx.command(ctx.device.async_set_target_temperature(thigh))
    satisfied = ctx.state.reached_target
    ctx.note(reached_target_with_temperature_satisfied=satisfied)
    if satisfied is not True:
        raise CheckSkippedError(
            f"the setpoint is at {thigh} and reached_target reads {satisfied}, so "
            f"there is no satisfied baseline to change the mode away from"
        )
    try:
        await ctx.command(ctx.device.async_set_mode(Mode.DRY))
        await ctx.command(ctx.device.async_set_target_humidity(low))
        waited = await ctx.until(
            lambda: ctx.state.reached_target is not True,
            timeout=min(REACH_WAIT, ctx.soak_seconds),
        )
        after = ctx.state.reached_target
        ctx.note(
            humidity_target=low,
            ambient_humidity=ctx.state.ambient_humidity,
            reached_target_in_dry=after,
            seconds_to_change=waited and round(waited, 1),
        )
        ctx.expect(
            waited is not None,
            f"the temperature setpoint is still at {thigh} and satisfied, but asking "
            f"mode {int(Mode.DRY)} for {low}% against an ambient {ambient}% left "
            f"reached_target at {after}. The thermostat is not watching the humidity "
            f"target, so either mode {int(Mode.DRY)} is not dry or rhlevel is a number "
            f"the device only stores — and if it only stores it, the humidity control "
            f"should not be offered at all",
        )
        if high >= ambient:
            await ctx.command(ctx.device.async_set_target_humidity(high))
            back = await ctx.until(
                lambda: ctx.state.reached_target is True,
                timeout=min(REACH_WAIT, ctx.soak_seconds),
            )
            ctx.note(reached_target_with_humidity_satisfied=ctx.state.reached_target)
            ctx.expect(
                back is not None,
                f"reached_target went unsatisfied for a humidity target of {low} but "
                f"would not come back for {high} against an ambient {ambient}; it is "
                f"not tracking the humidity target either way",
            )
        else:
            # The converse needs a target the device will accept at or above ambient,
            # and the ceiling is below the room today. Nothing to do but say so.
            ctx.note(reached_target_with_humidity_satisfied="not reachable")
    finally:
        if was_mode is not None and was_mode is not Mode.DRY:
            await ctx.command(ctx.device.async_set_mode(was_mode))


@_check("thermal", "The runtime counter advances while the unit runs", cost=1, soaks=1)
async def the_runtime_counter_advances(ctx: Context) -> None:
    """Check the state class this library assigns `worktime`, and pin down its units.

    Nothing to do with how long the appliance has run. `total_increasing` is a promise
    *this library* makes on the device's behalf: Home Assistant will accept the readings
    as a monotonic counter, derive long-term statistics from them, and treat a decrease
    as a meter reset. A field that moves backwards, or never moves at all, makes it the
    wrong state class — a bug here, in the sensor definition, whatever the hardware is
    doing.

    The units have never been established either, and this is how they get established:
    the wait ends on the first tick, so the elapsed time and the size of the step are
    both recorded. A counter in minutes announces itself by moving by 1 after about a
    minute. Needs the unit running but not the compressor.

    No tick is a result too, and the interesting half of one. A live run sat through the
    whole deadline without the counter moving, which rules out seconds and rules out
    minutes — so the hours this library ships it in survive, having been a guess drawn
    from `filterthr` reading 600 beside it, which is a filter reminder in hours if it
    is anything. Not a confirmation, and not recorded as one, but a counter too
    coarse to catch is the outcome hours predicts and the outcome the other two units
    rule out, so it is reported as evidence rather than as nothing.
    """
    if SensorKey.WORK_TIME not in ctx.caps.sensors:
        raise CheckSkippedError("model does not report a runtime counter")
    before = ctx.state.work_time
    if before is None:
        raise CheckSkippedError("the device reports no runtime counter")
    waited = await ctx.until(lambda: ctx.state.work_time != before)
    after = ctx.state.work_time
    ctx.note(
        work_time_before=before,
        work_time_after=after,
        seconds_to_tick=waited and round(waited, 1),
        step=None if after is None else after - before,
    )
    ctx.expect(
        after is not None and after >= before,
        f"the runtime counter went from {before} to {after}; this library ships it as "
        f"total_increasing, and a decrease makes Home Assistant read that as a meter "
        f"reset, so the state class is wrong",
    )
    if waited is None:
        ctx.note(units_finer_than_the_deadline=False)
        raise CheckSkippedError(
            f"the counter did not move in {ctx.soak_seconds:.0f}s of running (still "
            f"{before}), which rules out seconds and minutes and leaves the hours this "
            f"library ships it in — consistent, unconfirmed, and as far as a run of "
            f"this length can get"
        )
    ctx.note(units_finer_than_the_deadline=True)


# --- the runner ---------------------------------------------------------------------


class SelfTest:
    """Run checks against one real device, and put it back afterwards."""

    #: Fields restored at the end. Power is handled separately: settings written while a
    #: unit is off do not stick — which is itself one of the things now checked.
    _RESTORED: tuple[str, ...] = (
        "mode",
        "eco",
        "sleep",
        "extra",
        "fan_speed",
        "target_temperature",
        "target_humidity",
        "swing_vertical",
        "swing_horizontal",
        "mute",
        "display",
        "child_lock",
    )

    def __init__(
        self,
        device: ZafroDevice,
        *,
        suites: Collection[str] = SUITES,
        settle: float = SETTLE,
        soak: float = SOAK,
        poll: float = POLL,
    ) -> None:
        """Prepare a run. A `settle` of 0 is for unit tests, not for hardware."""
        unknown = sorted(set(suites) - set(SUITES))
        if unknown:
            msg = f"unknown suite(s) {', '.join(unknown)}; known: {', '.join(SUITES)}"
            raise ValueError(msg)
        self.device = device
        self.suites = [name for name in SUITES if name in suites]
        self.settle = settle
        self.soak = soak
        self.poll = poll
        self.baseline: DeviceState | None = None
        #: What the restore put back, and what it could not. Read after `run`.
        self.restored: dict[str, Any] = {}
        self._context = Context(device, settle=settle, soak=soak, poll=poll)

    # --- planning --------------------------------------------------------------------

    def checks(self) -> list[Check]:
        """Return the checks this run will attempt, in order."""
        return [check for check in CHECKS if check.suite in self.suites]

    def estimate(self) -> float:
        """Estimate the run in seconds, erring high.

        Someone deciding whether to let this run their air conditioner should not be
        told five minutes and then kept for twenty, so preparation, every wait and the
        restore are all counted, and every wait is counted at its deadline.

        That makes this a ceiling rather than a duration once the thermal suite is in:
        each of its waits ends as soon as the machine has done the thing, so a unit that
        responds promptly finishes in a fraction of the figure. Erring high in the
        direction of "sooner than promised" is the only safe direction for a number
        someone is consenting on.
        """
        checks = self.checks()
        # One settle per check for `_neutralise`, which does not always need to send
        # anything but is budgeted as though it does.
        settles = sum(check.cost + 1 for check in checks) + _PREPARE_SETTLES
        soaks = sum(check.soaks for check in checks)
        reaches = sum(check.reaches for check in checks)
        return (
            settles * self.settle
            + soaks * self.soak
            + reaches * min(REACH_WAIT, self.soak)
            + len(checks) * 0.5
        )

    def plan(self) -> list[str]:
        """Describe what the run will do, for a human to approve before it does it."""
        minutes = self.estimate() / 60
        lines = [
            f"Device:   {self.device.name} ({self.device.model})",
            f"Suites:   {', '.join(self.suites)}",
            f"Checks:   {len(self.checks())}",
            (
                f"Estimate: about {minutes:.0f} min "
                f"({self.estimate():.0f}s at a {self.settle:.0f}s settle)"
            ),
            "",
            "It will turn the unit on and drive it. Between checks it clears sleep,",
            "EXTRA and eco and parks the setpoint at its maximum, so the compressor",
            "idles; the claims that need the machine working make it work, and then",
            "hand it back idle.",
        ]
        if "thermal" in self.suites:
            without = SelfTest(
                self.device,
                suites=[s for s in self.suites if s != "thermal"],
                settle=self.settle,
                soak=self.soak,
            )
            lines += [
                "",
                "The thermal suite WILL run the compressor and dehumidify, at the",
                "bottom of the setpoint range. Each of its four waits ends as soon as",
                f"the unit has responded, or gives up after {self.soak:.0f}s — so the",
                (
                    f"estimate above is a ceiling. Without it the run is about "
                    f"{without.estimate() / 60:.0f} min."
                ),
            ]
        lines += [
            "",
            "It will change, and then restore: power, mode, fan speed, sleep, eco,",
            "EXTRA, both setpoints, both swing axes, the beeper, the display and the",
            "child lock. The restore is verified and reported.",
        ]
        if self.baseline is not None:
            lines += ["", f"Found: {_summarise(self.baseline)}"]
        return lines

    def refuse_reason(self) -> str | None:
        """Return why this run must not start, or None if it may.

        Almost nothing refuses a run any more. This is the full integration suite: it is
        asked for rarely and explicitly, and a run that quietly measured nothing
        because the room was warm is worse than one that costs ten minutes of cooling.
        The only thing left is a device that has not said anything yet, where there is
        no baseline to restore and so no safe way to begin.
        """
        if self.device.state.power is None:
            return "the device has not reported its state yet"
        return None

    # --- running ---------------------------------------------------------------------

    async def run(self) -> list[CheckResult]:
        """Baseline, run every selected check, and restore — whatever happens."""
        self.baseline = self.device.state
        results: list[CheckResult] = []
        try:
            await self._prepare()
            for check in self.checks():
                results.append(await self._run_one(check))
                await self._neutralise()
        except (KeyboardInterrupt, asyncio.CancelledError):
            _LOGGER.warning("Interrupted; restoring the device")
            raise
        finally:
            with contextlib.suppress(Exception):
                await self._restore()
        return results

    async def _run_one(self, check: Check) -> CheckResult:
        frames: list[dict[str, Any]] = []
        started = time.monotonic()

        def record(cmd: int, result: dict[str, Any]) -> None:
            frames.append(
                {
                    "at": round(time.monotonic() - started, 2),
                    "cmd": cmd,
                    "result": result,
                }
            )

        unsubscribe = self.device.subscribe_raw(record)
        self._context.measured = {}
        self._context.deferred = []
        try:
            await check.run(self._context)
        except CheckSkippedError as err:
            outcome, detail = "skip", str(err)
        except CheckFailedError as err:
            outcome, detail = "fail", str(err)
        except Exception as err:  # noqa: BLE001 - one bad check must not end the run
            outcome, detail = "error", f"{type(err).__name__}: {err}"
        else:
            outcome, detail = "pass", ""
        finally:
            unsubscribe()
        # Deferred failures outrank a pass and a skip alike, and a hard failure after
        # them does not bury them: a check that walks past one chose to keep testing,
        # not to forgive it. Collecting them here is what makes that impossible to
        # forget, and what stops one fault at the top of a walk hiding the rest.
        if self._context.deferred:
            found = [*self._context.deferred]
            if outcome == "fail" and detail:
                found.append(detail)
            outcome = "fail"
            detail = "; ".join(found)
        return CheckResult(
            name=check.name,
            suite=check.suite,
            claim=check.claim,
            outcome=outcome,
            detail=detail,
            seconds=round(time.monotonic() - started, 1),
            trace=frames,
            measured=dict(self._context.measured),
        )

    #: The state every check is written to start from: nothing overriding the fan, and
    #: the thermostat satisfied.
    _NEUTRAL: tuple[tuple[str, Any], ...] = (
        ("power", True),
        ("sleep", False),
        ("extra", False),
        ("eco", False),
    )

    async def _neutralise(self) -> None:
        """Put the unit back in the state the next check expects to find it in.

        Several checks leave the device somewhere it was not found, and one does it
        without touching the field at all: entering EXTRA makes the unit choose the
        coldest setpoint it has. Before this existed, the fan suite handed everything
        after it a unit with the compressor running and a room already at the bottom of
        the setpoint range — which cost `reached_target_follows_the_setpoint` the
        headroom it needs and ran the machine for the whole suite rather than for the
        part that is about the machine.

        Done here rather than in each check on purpose. A check is a claim, and making
        every claim responsible for tidying up is how one of them ends up not doing it.
        Raw frames rather than the setters, for the reason `_restore` uses them: a
        setter can refuse — the setpoint outside cool mode — and giving up half way
        through is worse than not trying.
        """
        state = self.device.state
        caps = self.device.capabilities
        fields: dict[str, Any] = {
            name: value
            for name, value in self._NEUTRAL
            if getattr(state, name) is not None and getattr(state, name) != value
        }
        bounds = caps.target_temperature_range
        if (
            state.mode is Mode.COOL
            and bounds is not None
            and state.target_temperature is not None
            and state.target_temperature != bounds[1]
        ):
            fields["target_temperature"] = bounds[1]
        if not fields:
            return
        await self.device.async_send_raw(build_command(fields))
        await self._context.settle()

    async def _prepare(self) -> None:
        """Get the unit into the one state in which the fan can be measured.

        On, in cool mode, with the setpoint at its maximum. Nothing about the fan can be
        measured with the unit off, because a unit that is off reports whatever speed it
        was last told and keeps a speed written while off, all without the fan turning —
        so every reading would be of the field and none of the fan. Sleep is refused in
        fan mode. Cool mode is therefore the only state where all of them are
        answerable.

        The setpoint goes to the top not to protect the room but to keep the compressor
        out of checks that are not about it: a check that has to make the machine work
        makes it work, and puts the setpoint back when it is done.
        """
        caps = self.device.capabilities
        if Mode.COOL not in caps.modes:
            return
        await self._context.command(self.device.async_set_power(on=True))
        await self._context.command(self.device.async_set_mode(Mode.COOL))
        if (bounds := caps.target_temperature_range) is not None:
            await self._context.command(
                self.device.async_set_target_temperature(bounds[1])
            )

    async def _restore(self) -> None:
        """Put every field back where it was found, and check that it went back.

        Published as raw frames built through `build_command` rather than through the
        setters, because a setter can refuse — restoring a humidity setpoint with the
        unit back in cool mode would raise — and a restore that gives up half way is
        worse than no restore at all. `build_command` still orders the keys.

        Settings first and power last when the unit was found off, because a unit that
        is off does not keep what it is told. Power first when it was found on, so what
        follows lands on a running unit.

        Then it reads the device again and says what did not come back. The promise to
        put the unit back is the one this tool makes to the person who agreed to let it
        run, and the version that made that promise without checking left them to
        notice for themselves.
        """
        baseline = self.baseline
        if baseline is None:
            return
        fields = {
            name: value
            for name in self._RESTORED
            if (value := getattr(baseline, name)) is not None
        }
        frames = [build_command(fields)] if fields else []
        if baseline.power is not None:
            power = build_command({"power": baseline.power})
            if baseline.power:
                frames.insert(0, power)
            else:
                frames.append(power)

        for frame in frames:
            await self.device.async_send_raw(frame)
            await self._context.settle()

        with contextlib.suppress(Exception):
            await self.device.async_refresh()
        now = self.device.state
        self.restored = {
            name: {"wanted": getattr(baseline, name), "got": getattr(now, name)}
            for name in ("power", *self._RESTORED)
            if getattr(baseline, name) is not None
            and getattr(baseline, name) != getattr(now, name)
        }
        if self.restored:
            _LOGGER.warning(
                "The device did not come back to how it was found: %s", self.restored
            )


#: Settle periods `_prepare` and `_restore` spend between them, for the estimate.
_PREPARE_SETTLES = 6


def _summarise(state: DeviceState) -> str:
    """Describe the fields a human would check against the unit's own display."""
    return ", ".join(
        f"{name}={getattr(state, name)}"
        for name in ("power", "mode", "fan_speed", "target_temperature", "sleep", "eco")
        if getattr(state, name) is not None
    )


def summarise(results: Iterable[CheckResult]) -> dict[str, int]:
    """Count outcomes, for an exit status and a closing line."""
    counts = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for result in results:
        counts[result.outcome] += 1
    return counts
