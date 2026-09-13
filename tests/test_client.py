"""Enumeration: what the account reports, and what the caller decides to forget."""

from __future__ import annotations

from typing import Any

import pytest

from pyzafro.client import ZafroClient

RAW_A = {
    "sn": "SN-A",
    "vendor": "I4SEASON",
    "model": "90038EAC0-12K-ZAZ",
    "name": "Bedroom",
    "mac": "001cc2000000",
}
RAW_B = {
    "sn": "SN-B",
    "vendor": "I4SEASON",
    "model": "90038EAC0-12K-ZAZ",
    "name": "Office",
    "mac": "001cc2000001",
}


class FakeRest:
    """Returns whatever the test last put in `listing`."""

    def __init__(self) -> None:
        self.listing: list[dict[str, Any]] = []

    async def async_get_devices(self) -> list[dict[str, Any]]:
        return list(self.listing)


class FakeMqtt:
    """Records routing changes instead of talking to a broker."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    async def async_register(self, sink: Any) -> None:
        self.registered.append(sink.sn)

    async def async_unregister(self, sink: Any) -> None:
        self.unregistered.append(sink.sn)


@pytest.fixture
def client() -> tuple[ZafroClient, FakeRest, FakeMqtt]:
    client = ZafroClient(
        None,  # type: ignore[arg-type]
        "user@example.com",
        "secret",
        client_id="test-0001",
    )
    rest, mqtt = FakeRest(), FakeMqtt()
    client._rest = rest  # type: ignore[assignment]
    client._mqtt = mqtt  # type: ignore[assignment]
    return client, rest, mqtt


async def test_enumeration_returns_only_what_the_account_reports(client):
    zafro, rest, _ = client
    rest.listing = [RAW_A, RAW_B]
    assert {d.sn for d in await zafro.async_get_devices()} == {"SN-A", "SN-B"}

    # One unit leaves the account. It must drop out of the return value immediately,
    # because that absence is the only signal a caller has to apply a policy to.
    rest.listing = [RAW_A]
    assert [d.sn for d in await zafro.async_get_devices()] == ["SN-A"]


async def test_an_absent_device_is_not_forgotten_on_its_own(client):
    """Absence is reported, never acted on. Removal is the caller's decision."""
    zafro, rest, mqtt = client
    rest.listing = [RAW_A, RAW_B]
    await zafro.async_get_devices()

    rest.listing = [RAW_A]
    await zafro.async_get_devices()

    assert {d.sn for d in zafro.devices} == {"SN-A", "SN-B"}
    assert mqtt.unregistered == []


async def test_a_device_that_returns_is_the_same_object(client):
    """Subscriptions and pending state survive a blip in the listing."""
    zafro, rest, mqtt = client
    rest.listing = [RAW_A]
    first = (await zafro.async_get_devices())[0]

    rest.listing = []
    await zafro.async_get_devices()
    rest.listing = [RAW_A]
    second = (await zafro.async_get_devices())[0]

    assert first is second
    assert mqtt.registered == ["SN-A"]  # registered once, not again on return


async def test_forget_unroutes_and_drops(client):
    zafro, rest, mqtt = client
    rest.listing = [RAW_A, RAW_B]
    await zafro.async_get_devices()

    await zafro.async_forget("SN-B")
    assert {d.sn for d in zafro.devices} == {"SN-A"}
    assert mqtt.unregistered == ["SN-B"]

    # Idempotent: a second call is a no-op, not an error.
    await zafro.async_forget("SN-B")
    assert mqtt.unregistered == ["SN-B"]


async def test_a_forgotten_device_is_rediscovered_fresh(client):
    zafro, rest, mqtt = client
    rest.listing = [RAW_A]
    first = (await zafro.async_get_devices())[0]
    await zafro.async_forget("SN-A")

    second = (await zafro.async_get_devices())[0]
    assert first is not second
    assert mqtt.registered == ["SN-A", "SN-A"]


async def test_a_listing_entry_with_no_serial_is_skipped(client):
    zafro, rest, _ = client
    rest.listing = [{"vendor": "I4SEASON"}, RAW_A]
    assert [d.sn for d in await zafro.async_get_devices()] == ["SN-A"]
