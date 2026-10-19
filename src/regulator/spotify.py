"""Thin async client for the Spotify Web API endpoints this project uses."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
DEFAULT_RETRY_AFTER_S = 5.0
# Ask for episodes too, otherwise a playing podcast comes back as item=null.
_PLAYBACK_PARAMS = {"additional_types": "track,episode"}


class TokenProvider(Protocol):
    async def access_token(self) -> str: ...
    async def force_refresh(self, stale_access_token: str) -> None: ...


class SpotifyError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(f"HTTP {status}: {message}" + (f" ({reason})" if reason else ""))
        self.status = status
        self.message = message
        self.reason = reason


class QuotaExceeded(SpotifyError):
    """Development-mode quota exhausted (429 + QUOTA_EXCEEDED). Retrying soon only makes it worse."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(429, message, "QUOTA_EXCEEDED")
        self.retry_after = retry_after


class SpotifyUnavailable(Exception):
    """Network errors, 5xx or rate limiting persisted through every retry."""


class SpotifyClient:
    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 300.0,
        player_command_gap_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._tokens = tokens
        self._http = http
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._command_gap = player_command_gap_s
        self._sleep = sleep
        self._clock = clock
        self._player_lock = asyncio.Lock()
        # Monotonic time before which no request may be sent (set by 429s, shared by all calls).
        self._not_before = 0.0

    # --- reads -------------------------------------------------------------------------------

    async def me(self) -> dict | None:
        return await self.request("GET", "/me")

    async def playback_state(self) -> dict | None:
        """GET /me/player. None means nothing is playing / no active device (204)."""
        return await self.request("GET", "/me/player", params=_PLAYBACK_PARAMS)

    async def currently_playing(self) -> dict | None:
        return await self.request("GET", "/me/player/currently-playing", params=_PLAYBACK_PARAMS)

    async def devices(self) -> dict | None:
        return await self.request("GET", "/me/player/devices")

    async def recently_played(self, *, after_ms: int | None = None, limit: int = 50) -> dict | None:
        params: dict[str, Any] = {"limit": limit}
        if after_ms is not None:
            params["after"] = after_ms
        return await self.request("GET", "/me/player/recently-played", params=params)

    # --- player commands (Premium only, serialized) -------------------------------------------

    async def skip_to_next(self, device_id: str | None = None) -> None:
        await self._player_command("POST", "/me/player/next", _device(device_id))

    async def pause(self, device_id: str | None = None) -> None:
        await self._player_command("PUT", "/me/player/pause", _device(device_id))

    async def set_repeat(self, state: Literal["off", "track", "context"], device_id: str | None = None) -> None:
        await self._player_command("PUT", "/me/player/repeat", {"state": state, **_device(device_id)})

    async def _player_command(self, method: str, path: str, params: dict[str, Any]) -> None:
        # Spotify doesn't guarantee ordering between player calls, so never have two in flight,
        # and leave a gap so the next one lands after this one took effect.
        async with self._player_lock:
            try:
                await self.request(method, path, params=params)
            finally:
                await self._sleep(self._command_gap)

    # --- transport ---------------------------------------------------------------------------

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any | None:
        """Send a request and return the decoded JSON, or None for an empty response (e.g. 204)."""
        refreshed = False
        for attempt in range(self._max_attempts):
            await self._wait_rate_limit_window()
            token = await self._tokens.access_token()
            try:
                resp = await self._http.request(
                    method, API_BASE + path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                delay = self._backoff(attempt)
                log.warning("spotify_network_error", extra={"path": path, "error": repr(exc), "retry_in_s": delay})
                await self._sleep(delay)
                continue

            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await self._tokens.force_refresh(token)
                continue
            if resp.status_code == 429:
                retry_after = _retry_after(resp)
                message, reason = _error_details(resp)
                if reason == "QUOTA_EXCEEDED":
                    self._not_before = self._clock() + retry_after
                    log.error("spotify_quota_exceeded", extra={"path": path, "retry_after_s": retry_after})
                    raise QuotaExceeded(message, retry_after)
                delay = max(retry_after, self._backoff(attempt))
                self._not_before = self._clock() + delay
                log.warning("spotify_rate_limited", extra={"path": path, "retry_after_s": retry_after, "wait_s": delay})
                continue
            if resp.status_code >= 500:
                delay = self._backoff(attempt)
                log.warning("spotify_server_error", extra={"path": path, "status": resp.status_code, "retry_in_s": delay})
                await self._sleep(delay)
                continue
            if resp.is_error:
                message, reason = _error_details(resp)
                raise SpotifyError(resp.status_code, message, reason)
            # The docs say player commands answer 204, but PUT /me/player/repeat really answers 200
            # with an opaque plain-text id and no Content-Type. Only JSON bodies carry data for us.
            if "json" not in resp.headers.get("content-type", ""):
                return None
            return resp.json()
        raise SpotifyUnavailable(f"{method} {path} falló tras {self._max_attempts} intentos")

    async def _wait_rate_limit_window(self) -> None:
        delay = self._not_before - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_max, self._backoff_base * 2**attempt)


def _device(device_id: str | None) -> dict[str, str]:
    return {"device_id": device_id} if device_id else {}


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return DEFAULT_RETRY_AFTER_S


def _error_details(resp: httpx.Response) -> tuple[str, str | None]:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason_phrase, None
    error = body.get("error", body)
    if isinstance(error, dict):
        return str(error.get("message", "")), error.get("reason") or body.get("reason")
    return str(body.get("error_description") or error), body.get("reason")
