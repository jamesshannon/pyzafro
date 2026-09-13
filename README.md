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

**Fan speed `0` is not off.** It is the silent speed sleep mode selects, with the unit
still running.

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
