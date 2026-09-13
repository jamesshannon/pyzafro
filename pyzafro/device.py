"""A single device: state, capabilities, and the read/write paths.

Writes are fire-and-forget with optimistic local application. See
`_apply_optimistic` for why, and for how an unconfirmed write reconciles itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from . import capabilities as caps_module
from .capabilities import Capabilities, Feature
from .const import (
    CMD_BASE_INFO,
    CMD_CONTROL,
    CMD_STATE,
    CMD_STATE_PUSH,
    REQUEST_TIMEOUT,
    RESYNC_DELAY,
)
from .exceptions import ZafroTimeoutError, ZafroUnsupportedError
from .models import (
    BASE_INFO_WIRE_KEYS,
    READ_ONLY_FIELDS,
    BaseInfo,
    DeviceState,
    Mode,
    ParsedState,
    build_command,
    parse_base_info,
    parse_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .mqtt import ZafroMqtt

_LOGGER = logging.getLogger(__name__)

#: Which setpoint field each mode uses. Confirmed against three real schedules read
#: back from /job/list: cool carries templevel and never rhlevel, dry the reverse,
#: fan neither.
#:
#: This is the only mode-dependence the captures actually establish. The app also
#: omits sleep/eco from fan-mode schedules, but partial payloads are accepted
#: generally, so that is evidence about what the app sends rather than what the
#: device refuses — and it is not enforced here.
_MODE_SETPOINT: dict[Mode, str | None] = {
    Mode.COOL: "target_temperature",
    Mode.DRY: "target_humidity",
    Mode.FAN: None,
    Mode.HEAT: "target_temperature",
}


class ZafroDevice:
    """One air conditioner, dehumidifier, mistifier, or tower fan."""

    def __init__(self, raw: dict[str, Any], transport: ZafroMqtt) -> None:
        """Build from a flattened /device/list entry."""
        self._raw = raw
        self._transport = transport

        self.sn: str = str(raw["sn"])
        self.vendor: str = str(raw["vendor"])
        # `type` is always an empty string on the wire; the model string is the real
        # device-class signal, exactly as the app treats it.
        self.model: str = str(raw.get("model") or "")
        self.name: str = str(raw.get("name") or self.model or self.sn)
        self.mac: str = str(raw.get("mac") or "")
        self.firmware: str = str(raw.get("version") or "")
        self.mcu_version: str = str(raw.get("mcu_version") or "")
        self.room: str | None = raw.get("room")
        self.room_id: int | None = raw.get("room_id")

        self.capabilities: Capabilities = caps_module.resolve(self.model)
        self.state = DeviceState()
        self.base_info: BaseInfo | None = None
        self.available = False

        self._subscribers: list[Callable[[ZafroDevice], None]] = []
        self._raw_subscribers: list[Callable[[int, dict[str, Any]], None]] = []
        self._build_lock = asyncio.Lock()
        self._state_event = asyncio.Event()
        self._base_info_event = asyncio.Event()
        self._pending: set[str] = set()
        self._resync_handle: asyncio.TimerHandle | None = None
        # Anomalies are logged once per key, not once per frame: pushes arrive every
        # few seconds and a device on new firmware would otherwise flood the log.
        # The samples are kept so diagnostics() can carry them into a bug report.
        self._unknown_keys: dict[str, Any] = {}
        self._rejected_keys: dict[str, Any] = {}
        self._drift: set[str] = set()
        self._refined = False

    def __repr__(self) -> str:
        """Identify the device without leaking the full serial."""
        return f"<ZafroDevice {self.name!r} model={self.model} sn=…{self.sn[-4:]}>"

    # --- subscription ----------------------------------------------------------------

    def subscribe(self, callback: Callable[[ZafroDevice], None]) -> Callable[[], None]:
        """Register a state-change callback. Returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(callback)

        return _unsubscribe

    def subscribe_raw(
        self, callback: Callable[[int, dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a callback for undecoded inbound frames.

        Intended for diagnostics: it sees the wire keys, including any this library does
        not model yet, which is how an unsupported product gets characterised.
        """
        self._raw_subscribers.append(callback)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._raw_subscribers.remove(callback)

        return _unsubscribe

    def _notify(self) -> None:
        for callback in list(self._subscribers):
            try:
                callback(self)
            except Exception:
                _LOGGER.exception("Subscriber for %s raised", self.sn)

    # --- reads -----------------------------------------------------------------------

    async def async_refresh(self) -> None:
        """Request full state (cmd:3) and wait for the reply.

        /device/list carries no state, so this is the only way to get a baseline.
        Every subsequent cmd:4 delta is merged into it.
        """
        self._state_event.clear()
        await self._transport.publish(self.vendor, self.sn, {"cmd": CMD_STATE})
        await self._await_event(self._state_event, "state")

    async def async_refresh_base_info(self) -> None:
        """Request base info (cmd:5): model, firmware, wifi signal."""
        self._base_info_event.clear()
        await self._transport.publish(self.vendor, self.sn, {"cmd": CMD_BASE_INFO})
        await self._await_event(self._base_info_event, "base info")

    async def _await_event(self, event: asyncio.Event, what: str) -> None:
        try:
            await asyncio.wait_for(event.wait(), REQUEST_TIMEOUT)
        except TimeoutError as err:
            raise ZafroTimeoutError(
                f"{self.name} did not return {what} within {REQUEST_TIMEOUT}s"
            ) from err

    # --- writes ----------------------------------------------------------------------

    async def async_set_power(self, *, on: bool) -> None:
        """Turn the device on or off."""
        await self._async_command(power=on)

    async def async_set_mode(self, mode: Mode) -> None:
        """Change operating mode."""
        if mode not in self.capabilities.modes:
            raise ZafroUnsupportedError(
                f"{self.name} does not support mode {mode.name}"
            )
        await self._async_command(mode=mode)

    async def async_set_target_temperature(self, value: int) -> None:
        """Set the temperature setpoint, in the device's own reported unit."""
        self._require_setpoint("target_temperature")
        self._require_range("target_temperature", value)
        await self._async_command(target_temperature=int(value))

    async def async_set_target_humidity(self, value: int) -> None:
        """Set the humidity setpoint."""
        self._require_setpoint("target_humidity")
        self._require_range("target_humidity", value)
        await self._async_command(target_humidity=int(value))

    async def async_set_fan_speed(self, level: int) -> None:
        """Set fan speed. 0 is the silent speed sleep mode uses, not off."""
        self._require(Feature.FAN_SPEED)
        if level not in self.capabilities.fan_speeds:
            raise ZafroUnsupportedError(
                f"{self.name} fan speed must be one of {self.capabilities.fan_speeds}"
            )
        await self._async_command(fan_speed=level)

    async def async_set_swing(
        self, *, horizontal: bool | None = None, vertical: bool | None = None
    ) -> None:
        """Set one or both swing axes."""
        fields: dict[str, Any] = {}
        if horizontal is not None:
            self._require(Feature.SWING_HORIZONTAL)
            fields["swing_horizontal"] = horizontal
        if vertical is not None:
            self._require(Feature.SWING_VERTICAL)
            fields["swing_vertical"] = vertical
        if fields:
            await self._async_command(**fields)

    async def async_set_sleep(self, *, on: bool) -> None:
        """Toggle sleep mode.

        The device reacts by setting mute and moving the fan speed; those arrive as a
        separate device-originated push and are not assumed here.
        """
        self._require(Feature.SLEEP)
        await self._async_command(sleep=on)

    async def async_set_eco(self, *, on: bool) -> None:
        """Toggle eco mode. The device moves the setpoint as a side effect."""
        self._require(Feature.ECO)
        await self._async_command(eco=on)

    async def async_set_child_lock(self, *, on: bool) -> None:
        """Toggle the child lock."""
        self._require(Feature.CHILD_LOCK)
        await self._async_command(child_lock=on)

    async def async_set_display(self, *, on: bool) -> None:
        """Toggle the front panel display."""
        self._require(Feature.DISPLAY)
        await self._async_command(display=on)

    async def async_set_mute(self, *, on: bool) -> None:
        """Toggle the beeper."""
        self._require(Feature.MUTE)
        await self._async_command(mute=on)

    def _require(self, feature: Feature) -> None:
        if not self.capabilities.has(feature):
            raise ZafroUnsupportedError(f"{self.name} does not support {feature}")

    def _require_setpoint(self, field: str) -> None:
        """Reject a setpoint the current mode does not use.

        Cool carries templevel and dry carries rhlevel; fan carries neither. Sending the
        wrong one would be silently ignored by the device, which is worse than an error.
        """
        mode = self.state.mode
        if mode is None:
            return
        expected = _MODE_SETPOINT.get(mode)
        if expected != field:
            raise ZafroUnsupportedError(
                f"{self.name} does not use {field} in {mode.name.lower()} mode"
            )

    def _require_range(self, field: str, value: int) -> None:
        bounds = (
            self.capabilities.target_temperature_range
            if field == "target_temperature"
            else self.capabilities.target_humidity_range
        )
        if bounds is None:
            raise ZafroUnsupportedError(f"{self.name} has no {field}")
        low, high = bounds
        if not low <= value <= high:
            raise ZafroUnsupportedError(
                f"{self.name} {field} must be between {low} and {high}"
            )

    async def async_send_raw(self, state: dict[str, Any]) -> None:
        """Publish a control frame using wire field names, bypassing validation.

        An escape hatch for characterising an unsupported product, whose
        capabilities are by definition unknown, and for testing a field this library
        refuses. Nothing is applied optimistically — whatever the device reports back
        is the only truth.

        Not for normal use. Prefer the typed setters.
        """
        _LOGGER.warning("Sending unvalidated state to %s: %s", self.name, state)
        await self._transport.publish(
            self.vendor, self.sn, {"cmd": CMD_CONTROL, "data": {"state": state}}
        )

    async def _async_command(self, **fields: Any) -> None:
        """Publish a control frame and optimistically apply what we sent.

        The lock covers construction and the publish only — never a network wait. It
        exists because the payload depends on the current mode, so two concurrent
        setters are a read-modify-write hazard. Acks are not awaited: the
        acknowledging frame arrives on the normal push path like any other.
        """
        async with self._build_lock:
            payload = self._build_payload(fields)
            await self._transport.publish(
                self.vendor, self.sn, {"cmd": CMD_CONTROL, "data": {"state": payload}}
            )
        self._apply_optimistic(fields)

    def _build_payload(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Build the wire `state` object.

        Partial payloads are accepted — every schedule's end_command is a lone
        {"poweron": false} — so only what changed is sent, with no padding.
        """
        for name in fields:
            if name in READ_ONLY_FIELDS:
                raise ZafroUnsupportedError(f"{name} is read-only")
        payload = build_command(fields)
        if not payload:
            raise ZafroUnsupportedError("Nothing to send")
        return payload

    def _apply_optimistic(self, fields: dict[str, Any]) -> None:
        """Assume the commanded fields took effect, then verify.

        Only the fields actually sent are applied — never the device's side effects
        (eco moving the setpoint, sleep changing fan speed). Those arrive as normal
        device-originated pushes.

        A rejected value is *assumed* to produce no push at all — no rejection has ever
        been observed, so this is unverified. Anything still unconfirmed after
        RESYNC_DELAY therefore forces a full cmd:3 and the reply is taken as truth. If
        the device does in fact NAK somehow, the timer becomes redundant rather than
        wrong.
        """
        self.state = self.state.merged(fields)
        self._pending |= set(fields)
        self._notify()
        self._arm_resync()

    def close(self) -> None:
        """Cancel anything scheduled. Call when the client is being torn down.

        Only the resync timer is outstanding; without this it survives for
        RESYNC_DELAY after shutdown, which a consumer that audits pending callbacks
        will rightly complain about.
        """
        if self._resync_handle is not None:
            self._resync_handle.cancel()
            self._resync_handle = None

    def _arm_resync(self) -> None:
        if self._resync_handle is not None:
            self._resync_handle.cancel()
        loop = asyncio.get_running_loop()
        self._resync_handle = loop.call_later(RESYNC_DELAY, self._resync_if_pending)

    def _resync_if_pending(self) -> None:
        self._resync_handle = None
        if not self._pending:
            return
        _LOGGER.debug(
            "%s did not confirm %s; resyncing", self.name, sorted(self._pending)
        )
        self._pending.clear()
        task = asyncio.get_running_loop().create_task(self._safe_refresh())
        task.add_done_callback(lambda _: None)

    async def _safe_refresh(self) -> None:
        """Re-read state, treating no answer as evidence about the connection.

        A device that is present and still says nothing to a cmd:3 is the earliest
        sign available that the socket is half-open — the broker has hung up and the
        client will not notice until its next keepalive, during which every command
        published is lost. A device the last-will topic has already declared gone
        explains its own silence, so it is not taken as evidence.
        """
        try:
            await self.async_refresh()
        except ZafroTimeoutError:
            _LOGGER.debug("Resync of %s timed out", self.name)
            if self.available:
                self._transport.note_unresponsive(self.sn)
        except Exception:
            _LOGGER.debug("Resync of %s failed", self.name, exc_info=True)

    # --- inbound ---------------------------------------------------------------------

    def handle_frame(self, cmd: int, result: dict[str, Any]) -> None:
        """Route an inbound frame. Called by the transport."""
        for raw_callback in list(self._raw_subscribers):
            try:
                raw_callback(cmd, result)
            except Exception:
                _LOGGER.exception("Raw subscriber for %s raised", self.sn)

        if cmd == CMD_BASE_INFO:
            self._log_unknown(
                {k: v for k, v in result.items() if k not in BASE_INFO_WIRE_KEYS}
            )
            self.base_info = parse_base_info(result)
            self._base_info_event.set()
            self._notify()
            return

        if cmd not in {CMD_STATE, CMD_STATE_PUSH}:
            return

        parsed = parse_state(result)
        self._log_anomalies(parsed)
        updates = parsed.updates
        if not updates:
            return

        # Always merge. cmd:4 frames are deltas; treating one as a snapshot would blank
        # every field it omits.
        self.state = self.state.merged(updates)
        # Any report is authoritative, so stop waiting on the fields it covers.
        self._pending -= set(updates)
        if cmd == CMD_STATE:
            self._pending.clear()
            self._refine_capabilities(updates)
            self._state_event.set()
        self.available = True
        self._check_capability_drift()
        self._notify()

    def _log_anomalies(self, parsed: ParsedState) -> None:
        """Report anything in a frame this library could not use.

        Two different problems, logged differently. An unknown key means the device
        offers something we do not surface — nothing breaks, but a sensor is missing,
        so INFO and an invitation to file it. A rejected value means a field we claim
        to support arrived unreadable, so Home Assistant is showing a stale value and
        does not know it; that is a WARNING.
        """
        self._log_unknown(parsed.unknown)

        for key, value in parsed.rejected.items():
            if key in self._rejected_keys:
                continue
            self._rejected_keys[key] = value
            _LOGGER.warning(
                "%s reported %r=%r, which this version of pyzafro cannot read. That "
                "field will stay at its last known value. Please open an issue with "
                "a diagnostics dump.",
                self.name,
                key,
                value,
            )

    def _log_unknown(self, unknown: dict[str, Any]) -> None:
        """Announce each never-before-seen wire key once."""
        for key, value in unknown.items():
            if key in self._unknown_keys:
                continue
            self._unknown_keys[key] = value
            _LOGGER.info(
                "%s reports %r, which this version of pyzafro does not model "
                "(value: %r). Please open an issue with a diagnostics dump so it "
                "can be supported.",
                self.name,
                key,
                value,
            )

    def _refine_capabilities(self, updates: dict[str, Any]) -> None:
        """Replace a guessed capability set with what the device actually reports.

        Runs once, on the first full state snapshot. The model was not in the table,
        so the fallback is a guess about an air conditioner; this is the first moment
        there is evidence. A product from a class this library has never handled is
        also the moment it becomes obvious, which is the only chance to stop a
        consumer building a thermostat for a vacuum cleaner.
        """
        if self._refined or self.capabilities.known_model:
            return
        self._refined = True

        self.capabilities = self.capabilities.refined(updates)
        if self.capabilities.is_climate:
            _LOGGER.info(
                "%s (model %r) is not in the capability table; using what it reports: "
                "%s. Please open an issue with a diagnostics dump.",
                self.name,
                self.model,
                ", ".join(sorted(str(f) for f in self.capabilities.features))
                or "no optional features",
            )
            return

        _LOGGER.warning(
            "%s (model %r) is not a supported product. It reports none of the fields "
            "this library understands, so no controls will be created for it. Fields "
            "seen: %s. Please open an issue with a diagnostics dump — supporting it "
            "is a change to pyzafro alone.",
            self.name,
            self.model,
            ", ".join(sorted(self._unknown_keys)) or "none",
        )

    def _check_capability_drift(self) -> None:
        """Notice a device doing something its capability entry says it cannot.

        This is how a half-supported product announces itself: the model matched a
        family pattern, or fell back, and the guessed table is too narrow. It matters
        because a consumer builds its UI from the capability table — Home Assistant
        logs an error of its own when a device reports a fan speed that is not in the
        list of speeds it was told to offer.
        """
        caps = self.capabilities
        state = self.state

        if (
            state.mode is not None
            and state.mode not in caps.modes
            and self._note_drift(f"mode={state.mode.name}")
        ):
            _LOGGER.warning(
                "%s is in %s mode, which is not in the capability table for model "
                "%r. The table is incomplete; please open an issue.",
                self.name,
                state.mode.name.lower(),
                self.model,
            )

        speed = state.fan_speed
        if (
            speed is not None
            and caps.fan_speeds
            and speed not in caps.fan_speeds
            and self._note_drift(f"fan_speed={speed}")
        ):
            _LOGGER.warning(
                "%s reports fan speed %d, outside the known speeds %s for model %r. "
                "The table is incomplete; please open an issue.",
                self.name,
                speed,
                caps.fan_speeds,
                self.model,
            )

        for field, bounds in (
            ("target_temperature", caps.target_temperature_range),
            ("target_humidity", caps.target_humidity_range),
        ):
            value = getattr(state, field)
            if value is None or bounds is None:
                continue
            low, high = bounds
            if not low <= value <= high and self._note_drift(f"{field}={value}"):
                _LOGGER.warning(
                    "%s reports %s=%s, outside the assumed range %d-%d for model %r. "
                    "The range is a guess; please open an issue so it can be "
                    "corrected.",
                    self.name,
                    field,
                    value,
                    low,
                    high,
                    self.model,
                )

    def _note_drift(self, what: str) -> bool:
        """Return True the first time `what` is seen, so each is logged once."""
        if what in self._drift:
            return False
        self._drift.add(what)
        return True

    def handle_presence(self, *, online: bool) -> None:
        """Update availability from the LWT topic, or from a transport drop."""
        if self.available != online:
            self.available = online
            self._notify()

    def handle_reconnect(self) -> None:
        """Re-baseline after a reconnect; missed deltas are never replayed."""
        task = asyncio.get_running_loop().create_task(self._safe_refresh())
        task.add_done_callback(lambda _: None)

    # --- diagnostics -----------------------------------------------------------------

    @property
    def anon_id(self) -> str:
        """A stable pseudonym for this device, safe to publish.

        Distinguishes devices within one report without revealing the serial.
        """
        return hashlib.sha256(self.sn.encode()).hexdigest()[:8]

    def diagnostics(self) -> dict[str, Any]:
        """Return a redacted dump, for bug reports about unsupported models.

        Removes the serial, MAC, and every user-chosen name — device names and room
        names are often personal.
        """
        redacted = {
            key: value
            for key, value in self._raw.items()
            if key not in {"sn", "mac", "name", "room", "additional", "data"}
        }
        return {
            "anon_id": self.anon_id,
            "device_list_entry": redacted,
            "base_info": _asdict_or_none(self.base_info, drop={"ssid"}),
            "state": _asdict_or_none(self.state),
            # The point of the whole anomaly-tracking exercise: whatever this device
            # did that the library did not expect travels with the bug report.
            "anomalies": {
                "unknown_keys": dict(sorted(self._unknown_keys.items())),
                "unreadable_keys": dict(sorted(self._rejected_keys.items())),
                "outside_capabilities": sorted(self._drift),
            },
            "capabilities": {
                "known_model": self.capabilities.known_model,
                "normalised_model": caps_module.normalise_model(self.model),
                "modes": sorted(m.name for m in self.capabilities.modes),
                "fan_speeds": list(self.capabilities.fan_speeds),
                "features": sorted(str(f) for f in self.capabilities.features),
                "sensors": sorted(str(s) for s in self.capabilities.sensors),
                "switches": sorted(str(s) for s in self.capabilities.switches),
                "binary_sensors": sorted(
                    str(s) for s in self.capabilities.binary_sensors
                ),
            },
        }


def _asdict_or_none(obj: Any, *, drop: set[str] | None = None) -> dict[str, Any] | None:
    if obj is None:
        return None
    data = asdict(obj)
    for key in drop or ():
        data.pop(key, None)
    return {k: (v.name if hasattr(v, "name") else v) for k, v in data.items()}
