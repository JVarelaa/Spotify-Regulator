"""Spotify OAuth (Authorization Code + PKCE) and on-disk token storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
)
# Spotify docs: refresh tokens of Dashboard apps live "6 months" from the original authorization,
# and refreshing the access token does not extend that. The exact expiry isn't returned, so we
# assume a slightly shorter lifetime: expiry warnings should come early, never late.
REFRESH_TOKEN_LIFETIME_S = 180 * 24 * 3600
ACCESS_TOKEN_MARGIN_S = 60


class AuthError(Exception):
    pass


class ReauthRequired(AuthError):
    """No usable refresh token (missing, expired or revoked): `regulator login` is needed."""


class TokenEndpointUnavailable(AuthError):
    """Temporary failure talking to the token endpoint (network, 5xx, 429); worth retrying."""


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    scope: str
    authorized_at: float  # epoch seconds of the interactive login; anchors the 6-month lifetime

    def access_expired(self, now: float) -> bool:
        return now >= self.expires_at - ACCESS_TOKEN_MARGIN_S

    @property
    def refresh_expires_at(self) -> float:
        return self.authorized_at + REFRESH_TOKEN_LIFETIME_S

    @property
    def missing_scopes(self) -> set[str]:
        return set(SCOPES) - set(self.scope.split())


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Token | None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        return Token(**data)

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(token), f, indent=2)
        # The O_CREAT mode is ignored if tmp already existed, so enforce it explicitly.
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 chars, inside the 43–128 range PKCE allows
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def _token_request(http: httpx.AsyncClient, data: dict[str, str]) -> dict:
    try:
        resp = await http.post(TOKEN_URL, data=data)
    except httpx.TransportError as exc:
        raise TokenEndpointUnavailable(f"error de red con el endpoint de tokens: {exc!r}") from exc
    if resp.status_code == 400 and _error_code(resp) == "invalid_grant":
        raise ReauthRequired(f"Spotify rechazó el grant (invalid_grant): {_error_description(resp)}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TokenEndpointUnavailable(f"el endpoint de tokens devolvió {resp.status_code}")
    if resp.is_error:
        raise AuthError(f"el endpoint de tokens devolvió {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error")
    except ValueError:
        return None


def _error_description(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error_description", "")
    except ValueError:
        return resp.text[:200]


async def exchange_code(
    http: httpx.AsyncClient, client_id: str, redirect_uri: str, code: str, verifier: str, now: float | None = None
) -> Token:
    now = time.time() if now is None else now
    data = await _token_request(
        http,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return Token(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=now + data["expires_in"],
        scope=data.get("scope", ""),
        authorized_at=now,
    )


async def refresh_access_token(http: httpx.AsyncClient, client_id: str, token: Token, now: float | None = None) -> Token:
    now = time.time() if now is None else now
    data = await _token_request(
        http,
        {"grant_type": "refresh_token", "refresh_token": token.refresh_token, "client_id": client_id},
    )
    return Token(
        access_token=data["access_token"],
        # Spotify may rotate the refresh token; when the response omits it, the old one stays valid.
        refresh_token=data.get("refresh_token") or token.refresh_token,
        expires_at=now + data["expires_in"],
        scope=data.get("scope", token.scope),
        authorized_at=token.authorized_at,
    )


class TokenManager:
    """Hands out valid access tokens, refreshing and persisting them as needed."""

    def __init__(
        self,
        store: TokenStore,
        client_id: str,
        http: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._store = store
        self._client_id = client_id
        self._http = http
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._token: Token | None = None
        # The token Spotify rejected with invalid_grant, and the token file's mtime at that moment,
        # so we don't hammer the token endpoint with it. A new `regulator login` rewrites the file,
        # changes the mtime and revives the manager.
        self._dead_token: Token | None = None
        self._dead_mtime: float | None = None

    @property
    def token(self) -> Token | None:
        return self._token

    @property
    def dead_token(self) -> Token | None:
        """The rejected token while waiting for a new login; None otherwise."""
        return self._dead_token

    def check_for_new_login(self) -> bool:
        """Cheap (one stat, no network): has the token file changed since it was rejected?"""
        return self._dead_mtime is not None and self._store.mtime() != self._dead_mtime

    async def access_token(self) -> str:
        async with self._lock:
            token = self._current()
            if token.access_expired(self._clock()):
                token = await self._refresh(token)
            return token.access_token

    async def force_refresh(self, stale_access_token: str) -> None:
        """Refresh after a 401, unless another caller already replaced the stale token."""
        async with self._lock:
            token = self._current()
            if token.access_token == stale_access_token:
                await self._refresh(token)

    def _current(self) -> Token:
        if self._token is not None:
            return self._token
        if self._dead_mtime is not None and self._store.mtime() == self._dead_mtime:
            raise ReauthRequired("el refresh token guardado ya no es válido; ejecuta `regulator login`")
        token = self._store.load()
        if token is None:
            raise ReauthRequired(f"no hay token en {self._store.path}; ejecuta `regulator login`")
        if self._dead_token is not None:
            log.info("new_login_detected", extra={"authorized_at": token.authorized_at})
        self._token, self._dead_token, self._dead_mtime = token, None, None
        return token

    async def _refresh(self, token: Token) -> Token:
        for attempt in range(self._max_attempts):
            try:
                new = await refresh_access_token(self._http, self._client_id, token, now=self._clock())
                break
            except ReauthRequired:
                # Definitive answer from Spotify: retrying can't help, wait for a new login instead.
                self._token, self._dead_token, self._dead_mtime = None, token, self._store.mtime()
                log.error("refresh_token_invalid", extra={"authorized_at": token.authorized_at})
                raise
            except TokenEndpointUnavailable as exc:
                if attempt == self._max_attempts - 1:
                    raise
                delay = self._backoff_base * 2**attempt
                log.warning("token_refresh_retry", extra={"error": str(exc), "retry_in_s": delay})
                await self._sleep(delay)
        self._store.save(new)
        self._token = new
        log.info("access_token_refreshed", extra={"refresh_token_rotated": new.refresh_token != token.refresh_token})
        return new
