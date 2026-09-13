# pyzafro

Async Python client for the ZAFRO (i4Season) air-conditioner cloud API — window air
conditioners, dehumidifiers, mistifiers and tower fans sold under the ZAFRO brand.

Built for [Home Assistant](https://www.home-assistant.io/), usable anywhere. `aiohttp` and
`aiomqtt`, fully typed, no sync fallback.

> Unofficial and unaffiliated. The protocol was recovered from the shipping Android app;
> the vendor publishes no documentation and offers no support for this.

## Install

```bash
pip install pyzafro
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

## Things worth knowing

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

## Unsupported models

Capabilities live in `capabilities.py`, keyed by model string. An unknown model still
works — it falls back to a minimal set covering power, mode, setpoint and ambient
readings — and logs the model at `INFO`.

To get a model properly supported, open an issue with the output of
`client.diagnostics()`, which is redacted and safe to paste.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio mypy ruff
.venv/bin/python -m pytest
.venv/bin/mypy pyzafro
.venv/bin/ruff check
```

## License

MIT
