"""Transport concerns that do not need a broker."""

from __future__ import annotations

import ssl
import threading
from typing import TYPE_CHECKING, Any

from pyzafro.mqtt import ZafroMqtt

if TYPE_CHECKING:
    import pytest


def _mqtt(**kwargs: Any) -> ZafroMqtt:
    """Build a transport with collaborators it will not reach in these tests."""
    return ZafroMqtt(None, None, "ha-test", **kwargs)  # type: ignore[arg-type]


async def test_default_tls_context_is_built_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading the trust store must not block the caller's event loop.

    Home Assistant watches for exactly this and tells the user to file a bug, so the
    assertion is on where the work happened, not just on what came back.
    """
    real = ssl.create_default_context
    built_on: list[str] = []

    def _record() -> ssl.SSLContext:
        built_on.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(ssl, "create_default_context", _record)

    context = await _mqtt()._async_tls_context()

    assert isinstance(context, ssl.SSLContext)
    assert built_on
    assert threading.main_thread().name not in built_on


async def test_default_tls_context_is_built_once() -> None:
    """Every reconnect reuses the first context rather than re-reading from disk."""
    transport = _mqtt()
    first = await transport._async_tls_context()
    second = await transport._async_tls_context()
    assert first is second


async def test_supplied_tls_context_is_used_as_is() -> None:
    """An application with a pre-warmed context passes it in and nothing is built."""
    supplied = ssl.create_default_context()
    transport = _mqtt(tls_context=supplied)
    assert await transport._async_tls_context() is supplied
