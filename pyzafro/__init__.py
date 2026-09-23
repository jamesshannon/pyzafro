"""Async client for the ZAFRO / i4Season air conditioner cloud API."""

from __future__ import annotations

from .capabilities import (
    BinarySensorKey,
    Capabilities,
    Feature,
    SensorKey,
    SwitchKey,
)
from .client import ZafroClient
from .device import ZafroDevice
from .exceptions import (
    ZafroApiError,
    ZafroAuthError,
    ZafroConnectionError,
    ZafroError,
    ZafroTimeoutError,
    ZafroUnsupportedError,
)
from .models import (
    BaseInfo,
    DeviceState,
    Mode,
    Origin,
    ParsedState,
    TemperatureUnit,
)

__all__ = [
    "BaseInfo",
    "BinarySensorKey",
    "Capabilities",
    "DeviceState",
    "Feature",
    "Mode",
    "Origin",
    "ParsedState",
    "SensorKey",
    "SwitchKey",
    "TemperatureUnit",
    "ZafroApiError",
    "ZafroAuthError",
    "ZafroClient",
    "ZafroConnectionError",
    "ZafroDevice",
    "ZafroError",
    "ZafroTimeoutError",
    "ZafroUnsupportedError",
]

__version__ = "1.1.0"
