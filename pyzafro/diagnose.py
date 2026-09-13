"""Diagnostic CLI: characterise an unsupported product, or debug a behaviour.

Three subcommands:

``report``
    Log in, enumerate, baseline every device, watch for a while, and write a redacted
    JSON report. This is what to attach to a GitHub issue asking for a new model.

``watch``
    Tail decoded frames live, showing what changed, when, and who caused it.

``probe``
    Send one change and record everything the device does in response. This is how an
    ambiguous field gets pinned down — set it, watch the unit, read the trace.

Nothing here is imported by ``pyzafro/__init__.py``; the library never loads it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from . import __version__
from .client import ZafroClient
from .const import CMD_BASE_INFO, CMD_PRESENCE, CMD_STATE, CMD_STATE_PUSH
from .exceptions import ZafroError
from .models import (
    FIELD_TO_WIRE,
    UNMODELLED_WIRE_KEYS,
    DeviceState,
    Origin,
    parse_state,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .device import ZafroDevice

_LOGGER = logging.getLogger(__name__)

#: Wire keys this library models. Anything else a device reports is a discovery.
KNOWN_WIRE_KEYS = frozenset(FIELD_TO_WIRE.values())

#: Caps on how much sample data a report keeps per field.
MAX_UNKNOWN_SAMPLES = 10
MAX_RANGE_SAMPLES = 25

_ORIGIN_NAMES = {Origin.DEVICE: "device", Origin.COMMANDED: "commanded"}

_CMD_NAMES = {
    CMD_PRESENCE: "presence",
    CMD_STATE: "state",
    CMD_STATE_PUSH: "push",
    CMD_BASE_INFO: "base_info",
}


# --- frame recording -----------------------------------------------------------------


class Recorder:
    """Captures raw frames for one device and summarises what was seen."""

    def __init__(self, device: ZafroDevice) -> None:
        """Attach to a device and start recording."""
        self.device = device
        self.started = time.monotonic()
        self.frames: list[dict[str, Any]] = []
        self._previous = DeviceState()
        self._unsubscribe = device.subscribe_raw(self._record)

    def close(self) -> None:
        """Stop recording."""
        self._unsubscribe()

    def _record(self, cmd: int, result: dict[str, Any]) -> None:
        self.frames.append(
            {
                "at": round(time.monotonic() - self.started, 2),
                "cmd": cmd,
                "keys": sorted(result),
                "result": result,
            }
        )

    # --- analysis --------------------------------------------------------------------

    def unknown_keys(self) -> dict[str, list[Any]]:
        """Wire keys this library does not model, with the values observed.

        The single most useful thing in a report about a new product.
        """
        found: dict[str, list[Any]] = {}
        for frame in self.frames:
            if frame["cmd"] not in {CMD_STATE, CMD_STATE_PUSH}:
                continue
            for key, value in frame["result"].items():
                if key in KNOWN_WIRE_KEYS:
                    continue
                values = found.setdefault(key, [])
                if value not in values and len(values) < MAX_UNKNOWN_SAMPLES:
                    values.append(value)
        return found

    def observed_ranges(self) -> dict[str, dict[str, Any]]:
        """Distinct values seen per modelled field, so ranges can be tightened."""
        seen: dict[str, list[Any]] = {}
        for frame in self.frames:
            if frame["cmd"] not in {CMD_STATE, CMD_STATE_PUSH}:
                continue
            for field, value in parse_state(frame["result"]).updates.items():
                plain = value.value if hasattr(value, "value") else value
                values = seen.setdefault(field, [])
                if plain not in values and len(values) < MAX_RANGE_SAMPLES:
                    values.append(plain)
        return {
            field: {
                "distinct": sorted(values, key=_sort_key),
                "count": len(values),
            }
            for field, values in sorted(seen.items())
        }

    def summary(self) -> dict[str, Any]:
        """Describe the recording compactly."""
        by_cmd: dict[str, int] = {}
        for frame in self.frames:
            name = _CMD_NAMES.get(frame["cmd"], f"cmd_{frame['cmd']}")
            by_cmd[name] = by_cmd.get(name, 0) + 1
        return {
            "duration_s": round(time.monotonic() - self.started, 1),
            "frame_count": len(self.frames),
            "frames_by_type": by_cmd,
            "observed_ranges": self.observed_ranges(),
            "unknown_wire_keys": self.unknown_keys(),
            "unmodelled_but_expected": sorted(
                key for key in self.unknown_keys() if key in UNMODELLED_WIRE_KEYS
            ),
        }


def _sort_key(value: Any) -> tuple[int, str]:
    return (0, "") if value is None else (1, str(value))


def _diff(before: DeviceState, after: DeviceState) -> list[str]:
    """Human-readable field changes between two states."""
    old, new = asdict(before), asdict(after)
    return [
        f"{field} {_show(old[field])} -> {_show(new[field])}"
        for field in sorted(new)
        if old[field] != new[field]
    ]


def _origin_name(value: Any) -> str:
    """Render the origin field: who caused this change."""
    try:
        return _ORIGIN_NAMES[Origin(value)]
    except (ValueError, KeyError):
        return "-"


def _show(value: Any) -> str:
    if value is None:
        return "-"
    return str(value.name.lower() if hasattr(value, "name") else value)


# --- shared session plumbing ---------------------------------------------------------


async def _connect(
    args: argparse.Namespace,
) -> tuple[aiohttp.ClientSession, asyncio.Task[None], list[ZafroDevice]]:
    """Log in, enumerate, and bring the MQTT connection up."""
    session = aiohttp.ClientSession()
    client = ZafroClient(
        session,
        args.email,
        args.password,
        client_id=f"diag-{secrets.token_hex(6)}",
    )
    try:
        await client.async_authenticate()
        devices = await client.async_get_devices()
    except Exception:
        await session.close()
        raise

    if not devices:
        await session.close()
        msg = "The account has no devices"
        raise ZafroError(msg)

    if args.device:
        devices = [d for d in devices if d.sn.endswith(args.device)]
        if not devices:
            await session.close()
            msg = f"No device matching {args.device!r}"
            raise ZafroError(msg)

    listener = asyncio.create_task(client.listen())
    await client.async_wait_connected()
    return session, listener, devices


async def _shutdown(
    session: aiohttp.ClientSession, listener: asyncio.Task[None]
) -> None:
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener
    await session.close()


async def _baseline(device: ZafroDevice) -> None:
    """Fetch base info and full state, tolerating a device that will not answer."""
    for label, coroutine in (
        ("base info", device.async_refresh_base_info()),
        ("state", device.async_refresh()),
    ):
        try:
            await coroutine
        except ZafroError as err:
            print(f"  ! could not read {label}: {err}", file=sys.stderr)


# --- report --------------------------------------------------------------------------


async def _cmd_report(args: argparse.Namespace) -> int:
    session, listener, devices = await _connect(args)
    try:
        print(f"Found {len(devices)} device(s). Baselining…", file=sys.stderr)
        recorders = [Recorder(device) for device in devices]
        for device in devices:
            model = device.model or "(no model)"
            print(f"  {model}  {device.anon_id}", file=sys.stderr)
            await _baseline(device)

        print(
            f"Listening for {args.seconds}s. Exercise the unit from the app now — "
            "change mode, speed, setpoint, swing — so the report covers more fields.",
            file=sys.stderr,
        )
        await asyncio.sleep(args.seconds)

        report = {
            "pyzafro_version": __version__,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "devices": [
                {**device.diagnostics(), "recording": recorder.summary()}
                for device, recorder in zip(devices, recorders, strict=True)
            ],
        }
        for recorder in recorders:
            recorder.close()
    finally:
        await _shutdown(session, listener)

    _emit(report, args.out)
    _print_report_advice(report, file=sys.stderr)
    return 0


def _emit(report: dict[str, Any], out: str | None) -> None:
    """Write the report to a file, or to stdout when no path was given."""
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {out}", file=sys.stderr)
    else:
        print(text)


def _print_report_advice(report: dict[str, Any], *, file: Any) -> None:
    print("\n--- what this found ---", file=file)
    for entry in report["devices"]:
        caps = entry["capabilities"]
        unknown = entry["recording"]["unknown_wire_keys"]
        print(
            f"\n{caps['normalised_model'] or '(unknown model)'}  {entry['anon_id']}",
            file=file,
        )
        if caps["known_model"]:
            print("  model is in the capability table", file=file)
        else:
            print(
                "  MODEL NOT IN THE CAPABILITY TABLE — this is worth reporting",
                file=file,
            )
        if unknown:
            print(
                f"  reports {len(unknown)} field(s) pyzafro does not model: "
                f"{', '.join(sorted(unknown))}",
                file=file,
            )
        else:
            print("  every reported field is modelled", file=file)
    print(
        "\nThe report contains no serial, MAC, wifi SSID, device name, or room name.\n"
        "Attach it to an issue at https://github.com/jamesshannon/pyzafro/issues",
        file=file,
    )


# --- watch ---------------------------------------------------------------------------


async def _cmd_watch(args: argparse.Namespace) -> int:
    session, listener, devices = await _connect(args)
    started = time.monotonic()
    previous: dict[str, DeviceState] = {}

    def make_handler(device: ZafroDevice) -> Any:
        def _on_frame(cmd: int, result: dict[str, Any]) -> None:
            before = previous.get(device.sn, DeviceState())
            after = before.merged(parse_state(result).updates)
            previous[device.sn] = after
            changes = _diff(before, after)
            who = _origin_name(result.get("origin"))
            label = _CMD_NAMES.get(cmd, f"cmd:{cmd}")
            stamp = f"{time.monotonic() - started:7.1f}s"
            head = f"{stamp}  {device.anon_id}  {label:<9} {who:<9}"
            if changes:
                print(f"{head} {'; '.join(changes)}", flush=True)
            else:
                print(f"{head} (no modelled change) {sorted(result)}", flush=True)

        return _on_frame

    try:
        for device in devices:
            device.subscribe_raw(make_handler(device))
            await _baseline(device)
        print(
            f"\nWatching {len(devices)} device(s) for {args.seconds}s. "
            "Change things in the app or on the unit.\n",
            file=sys.stderr,
        )
        await asyncio.sleep(args.seconds)
    finally:
        await _shutdown(session, listener)
    return 0


# --- probe ---------------------------------------------------------------------------


async def _cmd_probe(args: argparse.Namespace) -> int:
    if not args.set and not args.raw:
        print("Nothing to probe: pass --set or --raw", file=sys.stderr)
        return 2

    session, listener, devices = await _connect(args)
    device = devices[0]
    try:
        await _baseline(device)
        before = device.state
        recorder = Recorder(device)

        wire: dict[str, Any] = {}
        for item in args.raw or []:
            key, value = _parse_assignment(item)
            wire[key] = value
        for item in args.set or []:
            field, value = _parse_assignment(item)
            if field not in FIELD_TO_WIRE:
                print(
                    f"Unknown field {field!r}. Known: "
                    f"{', '.join(sorted(FIELD_TO_WIRE))}",
                    file=sys.stderr,
                )
                return 2
            wire[FIELD_TO_WIRE[field]] = value

        print(f"\nBefore: {_state_line(before)}", file=sys.stderr)
        print(f"Sending: {json.dumps(wire)}\n", file=sys.stderr)
        await device.async_send_raw(wire)

        await asyncio.sleep(args.observe)
        recorder.close()
    finally:
        await _shutdown(session, listener)

    print("--- response trace ---")
    for frame in recorder.frames:
        if frame["cmd"] not in {CMD_STATE, CMD_STATE_PUSH}:
            continue
        who = _origin_name(frame["result"].get("origin"))
        fields = {k: v for k, v in frame["result"].items() if k != "origin"}
        print(f"  +{frame['at']:5.1f}s  {who:<9} {json.dumps(fields)}")

    after = device.state
    changes = _diff(before, after)
    print("\n--- net change ---")
    for line in changes or ["  (nothing changed)"]:
        print(f"  {line}")

    commanded = {
        key
        for frame in recorder.frames
        if frame["result"].get("origin") == Origin.COMMANDED
        for key in frame["result"]
        if key != "origin"
    }
    unrequested = commanded - set(wire)
    if unrequested:
        print(
            f"\nThe device also acknowledged fields you did not send: "
            f"{', '.join(sorted(unrequested))}"
        )
    if not commanded:
        print(
            "\nNo acknowledgement (origin=1) arrived. The device may have ignored "
            "this field, or it may not report acknowledgements for it."
        )

    print(
        "\nNow look at the unit. If this probe was about a physically visible "
        "behaviour — which way a louvre moves, whether the fan actually stops — "
        "the trace above cannot tell you. Record what you saw alongside it."
    )
    return 0


def _state_line(state: DeviceState) -> str:
    parts = [
        f"{field}={_show(value)}"
        for field, value in asdict(state).items()
        if value is not None
    ]
    return ", ".join(parts) or "(empty)"


def _parse_assignment(item: str) -> tuple[str, Any]:
    """Parse ``key=value``, decoding the value as JSON when possible."""
    key, _, raw = item.partition("=")
    if not _:
        msg = f"Expected key=value, got {item!r}"
        raise SystemExit(msg)
    try:
        return key.strip(), json.loads(raw)
    except ValueError:
        return key.strip(), raw


# --- entry point ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyzafro-diagnose",
        description="Characterise a ZAFRO device or debug its behaviour.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-e", "--email", required=True)
        p.add_argument(
            "-p",
            "--password",
            help="prompted for if omitted, which keeps it out of shell history",
        )
        p.add_argument(
            "-d", "--device", help="serial suffix, if the account has several devices"
        )

    report = sub.add_parser("report", help="redacted JSON report for a bug report")
    common(report)
    report.add_argument("--seconds", type=int, default=120)
    report.add_argument("-o", "--out", help="write to a file instead of stdout")
    report.set_defaults(func=_cmd_report)

    watch = sub.add_parser("watch", help="tail decoded state changes live")
    common(watch)
    watch.add_argument("--seconds", type=int, default=300)
    watch.set_defaults(func=_cmd_watch)

    probe = sub.add_parser("probe", help="send one change and trace the response")
    common(probe)
    probe.add_argument(
        "--set",
        action="append",
        metavar="FIELD=VALUE",
        help="a pyzafro field, e.g. swing_horizontal=true",
    )
    probe.add_argument(
        "--raw",
        action="append",
        metavar="WIREKEY=VALUE",
        help="a raw wire key, bypassing validation, e.g. oscset1=true",
    )
    probe.add_argument("--observe", type=int, default=20)
    probe.set_defaults(func=_cmd_probe)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.password:
        args.password = os.environ.get("ZAFRO_PASSWORD") or getpass.getpass(
            f"Password for {args.email}: "
        )
    try:
        exit_code: int = asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    except ZafroError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
