"""Session token acquisition and lifecycle.

The token never leaves this library. Consumers hold the credentials and nothing else, so
the 7-day expiry is invisible to them: it is refreshed here, and only a genuinely bad
password produces ZafroAuthError.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import (
    LOGIN_COUNTRY,
    OK_CODE,
    PATH_LOGIN,
    TOKEN_DEFAULT_LIFETIME,
    TOKEN_REFRESH_RATIO,
    UNAUTHORIZED_CODES,
)
from .exceptions import ZafroAuthError, ZafroConnectionError, ZafroTimeoutError

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Token:
    """A session token and the numeric user id issued with it."""

    access_token: str
    user_id: int
    expires_at: float

    @property
    def stale(self) -> bool:
        """Whether the token is close enough to expiry to be worth replacing."""
        return time.monotonic() >= self.expires_at


class Authenticator:
    """Owns the session token and refreshes it on demand."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        base_url: str,
    ) -> None:
        """Store credentials. No network access happens here."""
        self._session = session
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._token: Token | None = None
        self._lock = asyncio.Lock()

    @property
    def user_id(self) -> int | None:
        """The numeric user id, once a login has succeeded."""
        return self._token.user_id if self._token else None

    async def async_get_token(self) -> str:
        """Return a usable access token, logging in first if necessary."""
        async with self._lock:
            if self._token is None or self._token.stale:
                self._token = await self._async_login()
            return self._token.access_token

    def invalidate(self) -> None:
        """Discard the cached token so the next call re-logs in.

        Called when the server rejects a request with 401/424 despite the token looking
        fresh, which happens if the session was revoked server-side.
        """
        self._token = None

    async def _async_login(self) -> Token:
        payload = {
            "email": self._email,
            "password": self._password,
            # Hardcoded by the app for every user in every country; not a parameter.
            "country": LOGIN_COUNTRY,
        }
        url = f"{self._base_url}{PATH_LOGIN}"
        try:
            async with self._session.post(
                url, json=payload, headers={"Language": "en"}
            ) as response:
                body: Any = await response.json(content_type=None)
        except TimeoutError as err:
            raise ZafroTimeoutError(f"Timed out logging in to {url}") from err
        except Exception as err:  # aiohttp.ClientError and friends
            raise ZafroConnectionError(f"Could not reach {url}: {err}") from err

        if not isinstance(body, dict):
            raise ZafroConnectionError(f"Unexpected login response: {body!r}")

        code = body.get("code")
        if code in UNAUTHORIZED_CODES:
            raise ZafroAuthError(body.get("msg") or "Invalid email or password")
        if code != OK_CODE:
            raise ZafroAuthError(f"Login failed ({code}): {body.get('msg')}")

        data = body.get("data") or {}
        access_token = data.get("access_token")
        user_id = data.get("user_id")
        if not access_token or user_id is None:
            raise ZafroAuthError("Login succeeded but returned no token")

        lifetime = int(data.get("expires_in") or TOKEN_DEFAULT_LIFETIME)
        _LOGGER.debug("Logged in as user %s; token valid for %ss", user_id, lifetime)
        return Token(
            access_token=str(access_token),
            user_id=int(user_id),
            expires_at=time.monotonic() + lifetime * TOKEN_REFRESH_RATIO,
        )
