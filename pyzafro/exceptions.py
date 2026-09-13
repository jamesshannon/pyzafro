"""Exception hierarchy.

This is the entire error contract a consumer needs. Home Assistant maps it as:

    ZafroAuthError        -> ConfigEntryAuthFailed   (reauth flow)
    ZafroConnectionError  -> ConfigEntryNotReady     (transient)
    ZafroTimeoutError     -> ConfigEntryNotReady
    ZafroUnsupportedError -> ServiceValidationError

An expired token is refreshed silently inside the library and must never surface as
ZafroAuthError, or every consumer would see a spurious reauth prompt once a week.
"""

from __future__ import annotations


class ZafroError(Exception):
    """Base class for every error raised by this library."""


class ZafroAuthError(ZafroError):
    """The credentials themselves are bad. Refreshing will not help."""


class ZafroConnectionError(ZafroError):
    """A transient transport failure. Retrying is reasonable."""


class ZafroTimeoutError(ZafroConnectionError):
    """The device or server did not answer in time."""


class ZafroApiError(ZafroError):
    """The REST envelope returned a non-zero code that is not an auth failure."""

    def __init__(self, code: int, message: str) -> None:
        """Record the body-level code and message."""
        super().__init__(f"API error {code}: {message}")
        self.code = code
        self.message = message


class ZafroUnsupportedError(ZafroError):
    """The caller asked for a feature this device does not have."""
