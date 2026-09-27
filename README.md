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
to, with the unit still running. Commanding it directly is acknowledged and then undone
by the device about five seconds later, so it is absent from `fan_speeds`: `sleep` is the
way to that speed.

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
is thorough in preference to quick or gentle. Twenty-five minutes at worst, usually much
less, and it runs the machine.

| suite | checks | what it exercises |
|---|---|---|
| `fan` | 9 | the fan control's positions and every transition between them |
| `protocol` | 8 | the changes the device makes that nobody asked for |
| `capabilities` | 11 | whether the table still describes this device, and whether its ranges are real |
| `liveness` | 6 | the assumptions optimistic writes rest on |
| `thermal` | 4 | the mappings that cannot be read without letting the unit run |

Selectable and repeatable with `--suite`, all of them by default. The first four take
about twelve minutes between them and never leave the thermostat unsatisfied for more than
a settle. `thermal` is all of the electricity and most of the clock: each of its four waits
ends the moment the unit has responded and gives up after `--soak` (three minutes), so the
printed estimate is a ceiling and a healthy unit finishes well inside it.

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
nothing reports an error. The only instrument for reading what a mode number means is the
direction the room moves. Likewise `reachtarget`, which gets a binary sensor named after a
behaviour nobody has watched it perform, and `worktime`, which ships as `total_increasing`
— a promise this library makes on the device's behalf that Home Assistant will read a
decrease as a meter reset.

Where a physical consequence is the only available instrument, a check reads it and says so.
Where it cannot tell a wrong mapping from a unit that simply is not cooling, it names both
and says which one is in scope.

Every check reports what it measured, pass or fail, because pinning down a number the
table only guesses at is half the reason to run it — including how long the machine took,
which is how `worktime`'s units get established at all:

```
  PASS  protocol      extra_moves_the_setpoint_within_the_claimed_range  (12s)
        setpoint_under_extra = 61
        table_floor = 60
  PASS  thermal       the_runtime_counter_advances  (63s)
        work_time_before = 1200
        work_time_after = 1201
        seconds_to_tick = 61.4
        step = 1
```

Two checks deliberately bypass validation, and only these two: the setpoint and humidity
ranges are probed one step past each end with a raw frame. Every other check can find a
range that is too *wide*, because the library offers a value and the device clamps it.
None can find one that is too narrow, because the library refuses out-of-range values
before they reach the wire — so a unit happily accepting 58 would never be asked, and its
owner would simply never be offered a setting their hardware has.

What a passing run still does not cover is stated rather than implied: the Celsius mapping
(`tempunit` is read-only), the fault-code vocabulary and the water and filter readings
(cannot be induced), availability (needs the plug pulled), and which louvre `oscset1`
moves (needs eyes on the unit).

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
