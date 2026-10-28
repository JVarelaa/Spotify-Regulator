"""`regulator run`: the 24/7 service.

Three independent tasks in one process, so a slow one never delays the others:
- player: snapshots of /me/player (short interval while music plays, long otherwise) fed to the
  reconstructor. When Spotify rejects the token it stops making requests and watches the token
  file until `regulator login` writes a new one.
- recently-played: syncs at start and then every hour, only while there is access.
- notices: login-expiry and lost-access emails, checked every minute.

The play in progress is saved as provisional state on every player round (not only on shutdown,
which a power cut skips), so the next run can carry it on instead of storing it twice.
If any task dies on an unexpected error the others are cancelled and the error propagates:
the process exits and Docker restarts it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from .auth import ReauthRequired, Token, TokenEndpointUnavailable
from .config import PollingConfig
from .reconstructor import Reconstructor, sample_from_playback
from .recent import RecentlyPlayedSource, sync_recently_played
from .spotify import QuotaExceeded, SpotifyError, SpotifyUnavailable
from .storage import Storage

log = logging.getLogger(__name__)

PLAY_STATE_KEY = "play_in_progress"
LOGIN_CHECK_S = 2.0
NOTICES_CHECK_S = 60.0
SYNC_RETRY_S = 5 * 60
HEARTBEAT_MAX_AGE_S = 120.0
# Long waits (exhausted quota) are slept in steps that keep the heartbeat fresh, so they don't look like a hang.
HEARTBEAT_STEP_S = 30.0

_TEMPORARY_ERRORS = (SpotifyError, SpotifyUnavailable, TokenEndpointUnavailable)


class PlayerSource(RecentlyPlayedSource, Protocol):
    async def playback_state(self) -> dict | None: ...


class LoginState(Protocol):
    @property
    def token(self) -> Token | None: ...
    @property
    def dead_token(self) -> Token | None: ...
    def check_for_new_login(self) -> bool: ...


class Notices(Protocol):
    async def check(self, token: Token | None, dead_token: Token | None) -> None: ...


class Service:
    def __init__(
        self,
        client: PlayerSource,
        tokens: LoginState,
        storage: Storage,
        notices: Notices,
        polling: PollingConfig,
        heartbeat_path: Path,
        *,
        reconstructor: Reconstructor | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self._tokens = tokens
        self._storage = storage
        self._notices = notices
        self._polling = polling
        self._heartbeat_path = heartbeat_path
        self._reconstructor = reconstructor or Reconstructor()
        self._clock = clock
        self._sleep = sleep
        # Set while Spotify accepts our token; the sync waits on it.
        self._access = asyncio.Event()
        self._waiting_for_login = False
        self._saved_play: str | None = None
        self._restore_play()

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set. If a task fails, the others are cancelled and the error propagates."""
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(self._player_loop(), name="player"),
                tg.create_task(self._sync_loop(), name="recently-played"),
                tg.create_task(self._notices_loop(), name="notices"),
            ]
            await stop.wait()
            for task in tasks:
                task.cancel()

    # --- player ------------------------------------------------------------------------------

    async def _player_loop(self) -> None:
        while True:
            await self._sleep_with_heartbeat(await self.poll_once())

    async def poll_once(self) -> float:
        """One player round. Returns how many seconds to wait before the next one."""
        try:
            payload = await self._client.playback_state()
        except ReauthRequired:
            await self._wait_for_login()
            return 0.0
        except QuotaExceeded as exc:
            return exc.retry_after  # the client already logged it
        except _TEMPORARY_ERRORS as exc:
            # The client already retried. Skip this sample: the play in progress just has a gap.
            log.warning("player_poll_failed", extra={"error": str(exc)})
            return self._polling.idle_interval_s

        if self._waiting_for_login:
            self._waiting_for_login = False
            log.info("access_restored")
        self._access.set()

        sample = sample_from_playback(payload, int(self._clock() * 1000))
        for event in self._reconstructor.observe(sample):
            self._storage.add_event(event)
            log.info(
                "listen_recorded",
                extra={
                    "track": event.track_name,
                    "outcome": event.outcome.value,
                    "is_repeat": event.is_repeat,
                    "position_ms": event.position_ms,
                },
            )
        self._save_play()
        playing = sample is not None and sample.is_playing
        return self._polling.playing_interval_s if playing else self._polling.idle_interval_s

    async def _wait_for_login(self) -> None:
        """No usable token: make no requests until `regulator login` writes a new token file."""
        self._access.clear()
        if not self._waiting_for_login:
            self._waiting_for_login = True
            log.error("waiting_for_login")  # the notices task emails about it
        while True:
            await self._sleep_with_heartbeat(LOGIN_CHECK_S)
            # No dead token means there was no token file at all: just try again.
            if self._tokens.dead_token is None or self._tokens.check_for_new_login():
                return

    async def _sleep_with_heartbeat(self, seconds: float) -> None:
        self._beat()
        while seconds > 0:
            step = min(seconds, HEARTBEAT_STEP_S)
            await self._sleep(step)
            seconds -= step
            self._beat()

    def _beat(self) -> None:
        self._heartbeat_path.touch()

    def _save_play(self) -> None:
        snapshot = self._reconstructor.snapshot()
        value = json.dumps(snapshot, ensure_ascii=False) if snapshot else None
        if value == self._saved_play:
            return
        if value is None:
            self._storage.delete_state(PLAY_STATE_KEY)
        else:
            self._storage.set_state(PLAY_STATE_KEY, value)
        self._saved_play = value

    def _restore_play(self) -> None:
        raw = self._storage.get_state(PLAY_STATE_KEY)
        if raw is None:
            return
        try:
            self._reconstructor.restore(json.loads(raw))
        except (KeyError, TypeError, ValueError):
            log.warning("play_in_progress_unreadable")
            self._storage.delete_state(PLAY_STATE_KEY)
            return
        self._saved_play = raw
        log.info("play_in_progress_restored", extra={"track": self._reconstructor.current.track_name})

    # --- recently-played ---------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        while True:
            await self._access.wait()
            try:
                await sync_recently_played(self._client, self._storage, now_ms=int(self._clock() * 1000))
            except ReauthRequired:
                self._access.clear()  # the player task notices too and waits for the new login
                continue
            except _TEMPORARY_ERRORS as exc:
                log.warning("recently_played_sync_failed", extra={"error": str(exc), "retry_in_s": SYNC_RETRY_S})
                await self._sleep(SYNC_RETRY_S)
                continue
            await self._sleep(self._polling.recently_played_interval_min * 60)

    # --- notices -----------------------------------------------------------------------------

    async def _notices_loop(self) -> None:
        while True:
            await self._notices.check(self._tokens.token, self._tokens.dead_token)
            await self._sleep(NOTICES_CHECK_S)


def heartbeat_age(path: Path, *, now: float | None = None) -> float | None:
    """Seconds since the player task last beat, or None if it never did."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return (time.time() if now is None else now) - mtime
