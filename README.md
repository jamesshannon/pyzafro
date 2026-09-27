# pyzafro

Async Python client for the ZAFRO (i4Season) air-conditioner cloud API — window air
conditioners, dehumidifiers, mistifiers and tower fans sold under the ZAFRO brand.

Built for [Home Assistant](https://www.home-assistant.io/), usable anywhere. `aiohttp` and
`aiomqtt`, fully typed, no sync fallback.

> Unofficial and unaffiliated. The protocol was recovered from the shipping Android app;
> the vendor publishes no documentation and offers no support for this.

## Install

Not on PyPI yet.

```bash
pip install git+https://github.com/jamesshannon/pyzafro
```

## Use

```python
import asyncio
import aiohttp
from pyzafro import ZafroClient, Mode


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = ZafroClient(
            session,
            "you@example.com",
            "password",
            client_id="my-app-3f9c1a2b",  # see below
        )
        devices = await client.async_get_devices()
        listener = asyncio.create_task(client.listen())
        await client.async_wait_connected()

        ac = devices[0]
        ac.subscribe(lambda d: print(d.state))

        await ac.async_refresh_base_info()  # model, firmware, wifi signal
        await ac.async_refresh()  # full state baseline

        await ac.async_set_mode(Mode.COOL)
        await ac.async_set_target_temperature(70)

        await asyncio.sleep(60)
        listener.cancel()


asyncio.run(main())
```

## Behaviour

**Pick a unique `client_id` and keep it.** The phone app connects as `app_{user_id}`. If
you reuse that, the broker evicts whichever client connected first and the app and your
code kick each other in a loop forever. Generate one value at setup, persist it, pass the
same one every time.

**All state arrives over MQTT.** The REST device list contains no state at all — no
temperature, no mode, not even online/offline. `async_refresh()` publishes a request and
waits for the reply; everything after that is pushed.

**Pushes are deltas.** The library merges them for you. `DeviceState` fields are `None`
until the first frame that mentions them.

**Writes are optimistic, not confirmed.** A setter publishes, applies the field locally,
and returns. If the device does not confirm within a few seconds the library re-requests
full state and takes that as truth.

**The device overrides what you send.** Enabling eco moves the setpoint; enabling sleep
changes the fan speed and mutes the beeper. Only the field you set is applied
optimistically — side effects arrive as normal pushes a moment later. Never assume a
consequence the device has not reported.

**Temperatures are in the device's own unit.** `DeviceState.temperature_unit` says which.
Values are not normalised to Celsius; convert at the edge if you need to.

**The fan is one control with six positions, not three axes.** `fan_speed` 1-4 are the
positions the remote's fan button cycles — three bars and auto — `extra`, reached by
holding that button, is above them, and `sleep` is below them. Both of those are fields
of their own and both come back alongside a `fan_speed`, so `state.sleep` and
`state.extra` have to be read before `state.fan_speed` means anything.

Entering any position leaves the others: `async_set_fan_speed` clears sleep, EXTRA and
eco (the app's own speed payload does, because the device overrides the speed under the
last two), and `async_set_sleep` and `async_set_extra` clear each other.

EXTRA is the vendor's own name for it, on the unit's display and in the app; the app's
code calls it `turbo` internally, and nothing here does.

**Key order in a control frame is part of the protocol.** The device applies a frame's
keys in the order they appear, and some writes make it recalculate others: clearing
`sleep` restores the speed the fan had before sleep, setting `extra` forces speed 3 and
the lowest setpoint, setting `eco` forces speed 1 and a setpoint of 76. So the same JSON
object means two different things depending on the order its keys are written in —
`{"windlevel": 3, "sleep": false}` leaves the fan at its old speed, and
`{"sleep": false, "windlevel": 3}` leaves it at 3.

The library emits a fixed order so that whatever the caller asked for is the last word,
and the order the caller built its dict in is not something you have to think about.
`async_send_raw` does not reorder anything, which is one more reason it is a diagnostic
rather than a way to write state.

**Fan speed `0` is not off, and cannot be asked for.** It is the speed sleep mode drops
to, with the unit still running. Commanding it directly is acknowledged and then undone,
so it is absent from `fan_speeds`: `sleep` is the way to that speed.

The undoing is worth a word, because it is not how this device turns anything else down.
A value the unit means to refuse comes back corrected in the frame right after the
acknowledgement, within a second. Speed `0` is not refused: it is stored, reported back as
the current speed, and then quietly replaced by the speed the fan is really running,
riding the next ambient reading the unit was going to push anyway — several seconds later,
on nobody's schedule. So a consumer that reads the speed straight after writing one can
see a `0` that is about to stop being true, and anything asserting the device does not
keep it has to wait for the replacement rather than sleep and look once.

## The diagnostic tool

Installing the package also installs `pyzafro-diagnose`, which is how an unsupported
product gets characterised and how an ambiguous field gets pinned down. It needs nothing
but the package and your account.

```bash
pyzafro-diagnose report -e you@example.com -o zafro-report.json
```

Logs in, baselines every device, records for two minutes while you exercise the unit from
the app, and writes a report. It names any wire field the library does not model, and
tells you whether your model is in the capability table.

The report contains **no serial, MAC, wifi SSID, device name, or room name**. Devices are
identified by a hash of the serial so several can be told apart. Attach it to an issue.

```bash
pyzafro-diagnose watch -e you@example.com
```

Tails decoded changes live, showing what changed and whether the device or a command
caused it:

```
   12.4s  a3f91c02  push      device    ambient_humidity 88 -> 87
   19.8s  a3f91c02  push      commanded swing_horizontal False -> True
   20.9s  a3f91c02  push      device    fan_speed 4 -> 1; target_temperature 65 -> 76
```

```bash
pyzafro-diagnose probe -e you@example.com --raw oscset1=true --observe 20
```

Sends one change and traces everything that follows, then reports the net change and any
field the device acknowledged that you did not ask for. `--raw` bypasses validation, so it
works on a product whose capabilities are unknown, or on a field the library refuses.

The tool cannot see the hardware. For a physically visible question — which way a louvre
moves, whether the fan really stops — it makes the causal link unambiguous and leaves the
observation to you.

```bash
pyzafro-diagnose selftest -e you@example.com
```

Drives a real unit through this library's own setters and checks it still behaves the way
the library says it does. `probe` tests the device and leaves the write path, capability
validation and optimistic reconciliation untouched; this exercises all of them, which is
where the bugs have actually been.

This is the full integration suite, not a smoke test. It is meant to be run rarely and
deliberately — once before a release, or after a bug that got past the unit tests — and it
is thorough in preference to quick or gentle. Seventeen minutes at worst, nine or ten in
practice, and it runs the machine.

| suite | checks | what it exercises |
|---|---|---|
| `fan` | 9 | the fan control's positions and every transition between them |
| `protocol` | 8 | the changes the device makes that nobody asked for |
| `capabilities` | 11 | whether the table still describes this device, and whether its ranges are real |
| `liveness` | 6 | the assumptions optimistic writes rest on |
| `thermal` | 4 | the mode mappings, which only a running unit's thermostat can answer |

Selectable and repeatable with `--suite`, all of them by default. The first four take
about eight minutes between them in practice and never leave the thermostat unsatisfied
for more than a settle. `thermal` is all of the electricity and a third of the clock:
three of its four checks wait on the MCU to compare two numbers it already holds, and give
up after a minute; the fourth waits for nothing at all. Every wait ends the moment the unit
has answered, so the printed estimate is a ceiling and a healthy unit finishes well inside
it — around ten minutes against a ceiling of about seventeen.

**It runs the machine.** It prints what it will change, asks before starting, and restores
every field afterwards — including after a failure or a Ctrl-C — then re-reads the device
and reports anything that did not go back. Between checks it clears sleep, EXTRA and eco
and parks the setpoint at its maximum, so the compressor only runs where a check is about
the compressor.

None of this tests the appliance. How well a unit cools, or whether it needs servicing, is
a fact about somebody's hardware and no business of this library's. What *is* its business
is that some of its own mappings were never verified against anything — and a few of those
happen to be unreadable from a unit whose thermostat is satisfied, which is the state every
other suite arranges.

`Mode.COOL = 1` was read off a switch statement in decompiled Dart, and every other check
would pass with the enum shuffled: commanding mode 1 and reading mode 1 back proves the
device accepts the number, never that the number means cooling. `Mode` labels the entire
HVAC dropdown, so a mislabelled member puts "Cool" on the button that dehumidifies and
nothing reports an error. Likewise `reachtarget`, which gets a binary sensor named after a
behaviour nobody has watched it perform, and `worktime`, which ships as `total_increasing`
— a promise this library makes on the device's behalf that Home Assistant will read a
decrease as a meter reset.

**Nothing here waits on the room, and nothing should.** Three checks did and all three
were wrong. The two mode checks because each ambient reading alternates between two
adjacent integers, so the instrument's noise is twice the smallest change either could
look for; the runtime counter because it ticks in hours, which no wait anyone would sit
through can see. None of those is a tuning problem, so the budget has no line for
waiting on the room at all — a check that wants to wait on physics has to reintroduce
the idea deliberately. What survives is `--max-wait`, the ceiling every remaining
deadline is clamped to; it used to be `--soak`, back when the suite thought its job was
to let the machine run for a while.

What a mode number actually claims is which setpoint that mode's thermostat compares
against and in which direction, and the MCU answers that in about a second from two
numbers it already holds.

Dry mode: park the temperature setpoint where the unit calls itself satisfied, change
nothing about it, and switch to dry with a humidity target below ambient. If `reachtarget`
goes out, the device is comparing something other than the two temperatures, and `rhlevel`
is the only other setpoint it has — so dry is the humidity mode and the humidity target is
a number the device acts on. That also fixes the field's polarity: a unit with the room at
76% and a target of 30% read `reachtarget` 0, and an air conditioner cannot add water, so
0 is "not yet".

Cool mode, with the polarity pinned and mode 2 accounted for, is then just the direction.
Put the target above the room and the thermostat is satisfied; put it below and it is not.
A heating thermostat is exactly the other way round, so one reading from each side of
ambient settles it.

Both of those replaced a three-minute wait for the room to move, and both waits were wrong
for the same reason. Each ambient reading alternates between two adjacent integers about a
second apart, so the instrument's noise is twice the smallest change either check could
look for. The humidity version failed against a unit that was dehumidifying; the cooling
version passed and failed on consecutive runs against the same unit in the same room,
having once caught the reading on its way down and credited the command with it. Watching
for a stable baseline first does not rescue it, because a reading that alternates reads the
same at both ends of a poll.

So whether the machine removes any heat or any water is not something this suite answers.
It is on the uncovered list below, with the reason.

The runtime counter is the third case and fails differently: the instrument is exact and
the timescale is wrong. `worktime` moves in hours, so a three-minute wait could never see
it and never did. It compares against the state the run found instead: a window four times
longer for no wait at all, and long enough to tell "minutes, and the tick was just missed"
from "hours". What would actually settle the units is two runs a few days apart, which is
one of the things `-o` is for; no single run can see it however long it waits.

Every check reports what it measured, pass or fail, because pinning down a number the
table only guesses at is half the reason to run it — including how long the machine took,
which is how `worktime`'s units get established at all:

```
  PASS  protocol      extra_moves_the_setpoint_within_the_claimed_range  (12s)
        setpoint_before_extra = 86
        setpoint_under_extra = 61
        table_floor = 61
  FAIL  capabilities  the_setpoint_range_is_accepted  (18s)
        The setpoint range is accepted at both ends
        -> the table offers 60 but the device clamped it to 61; 61 is the real limit
        setpoint_86_became = 86
        setpoint_60_became = 61
```

That second one came from hardware, and is what the table now says. Running this against a
real unit moved four numbers — the setpoint floor from 60 to 61, the humidity ceiling from
80 to 70, and the display light off the window unit's feature list entirely, because it
reports `lighton` and ignores every command to it, so the switch built from that field did
nothing. Both READMEs lost a claim too: fan mode turns down sleep, but accepts Extra and
eco.

The other thing a rare, thorough run buys is that **the checks get audited by the hardware
they audit**, and several were wrong:

- The cooling check described above, which the room could not answer.
- An off-state check that wrote the fan speed the unit was already at, so its read-back was
  the same number whether the write landed or not.
- Every early off-state reading, because this unit takes about twenty seconds to finish
  switching off with the fan running for all of it. Those checks read the state and powered
  the unit back on well inside that window, so they never saw the unit off, and one of their
  conclusions reached both READMEs before the timer came up. A check that reads a state the
  device takes time to reach has to be told how long that is — the deadline is not always
  the one you were thinking about.
- A check that read back its own optimistic write and called it the device's answer.

Measured past the turn-off timer: **the fan does not park**, so the speed shown while the
unit is off is the setting rather than a parked value, and **a setting written to a unit
that is off is kept**. Earlier readings said otherwise and were all taken inside the
shutdown.

The last thing hardware found is a bug in the suite's shape rather than in a check. Three
of the four thermal checks skip unless they are handed cool mode, and nothing was
establishing it: the liveness check that sends two conflicting commands and keeps whichever
the device answered with sends two *modes*, so the mode left behind was the outcome of a
race. When it came out cool all three ran; when it came out dry all three skipped. The
between-check reset parks the mode now, beside the setpoint it already parked. A
precondition a later check needs cannot be left to what an earlier one happened to do.

Two checks deliberately bypass validation, and only these two: the setpoint and humidity
ranges are probed one step past each end with a raw frame. Every other check can find a
range that is too *wide*, because the library offers a value and the device clamps it.
None can find one that is too narrow, because the library refuses out-of-range values
before they reach the wire — so a unit happily accepting 58 would never be asked, and its
owner would simply never be offered a setting their hardware has. Hardware found the
shipped range wrong in both directions at once, which is the argument for both halves.

A check that walks a list — every claimed feature, both ends of a range — reports every
item, not the first bad one. Both failure modes have happened: a run stopped at a display
switch the device ignores and so never tested the beeper, and a failing humidity bound hid
the probe behind it.

What a passing run still does not cover is stated rather than implied: the Celsius mapping
(`tempunit` is read-only), the fault-code vocabulary and the water and filter readings
(cannot be induced), availability (needs the plug pulled), which louvre `oscset1` moves
(needs eyes on the unit), and whether the machine actually cools or removes any water —
each ambient reading's own flap between two adjacent integers is larger than anything one
run can measure, which is also why neither reading can be confirmed as the room's.

A failure means a claim in this library that your hardware does not support — a different
firmware, or a model the table describes wrongly. `-o results.json` writes the run with
the same redaction as `report`: no serial, MAC, wifi name or room name. Attach it to an
issue.

## Unsupported models

Capabilities live in `capabilities.py`, keyed by model string. An unknown model still
works — it falls back to a minimal set covering power, mode, setpoint and ambient
readings — and logs the model at `INFO`.

To get a model properly supported, open an issue with a `pyzafro-diagnose report`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio mypy ruff
.venv/bin/python -m pytest
.venv/bin/mypy pyzafro
.venv/bin/ruff check
```

## License

MIT
