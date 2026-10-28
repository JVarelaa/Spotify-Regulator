"""Login-expiry notices.

Warns 3 days before, 2 days before and on the day the refresh token expires (calendar days in
the configured timezone), and once when Spotify rejects the token and the service starts
waiting for a new `regulator login`. What was sent is persisted per login, so restarts don't
repeat notices and a new login starts the cycle again.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .auth import Token
from .notify import NotificationError, Notifier

log = logging.getLogger(__name__)

WARN_DAYS_BEFORE = (3, 2, 0)
RETRY_AFTER_FAILURE_S = 15 * 60


def due_warning(today: date, expiry_day: date, sent: Iterable[int]) -> int | None:
    """Which warning (3, 2 or 0 days before) is due today, or None.

    Only the most recent applicable warning is returned, so after downtime you get one
    notice instead of a burst of stale ones.
    """
    days_left = (expiry_day - today).days
    applicable = [d for d in WARN_DAYS_BEFORE if 0 <= days_left <= d]
    if not applicable:
        return None
    latest = min(applicable)
    if any(d <= latest for d in sent):
        return None
    return latest


@dataclass
class NoticeState:
    authorized_at: float  # identifies the login these notices belong to
    warnings_sent: list[int] = field(default_factory=list)
    reauth_notice_sent: bool = False


class ExpiryNotices:
    def __init__(
        self,
        path: Path,
        notifier: Notifier,
        tz: str,
        redirect_port: int,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self._path = path
        self._notifier = notifier
        self._tz = ZoneInfo(tz)
        self._redirect_port = redirect_port
        self._clock = clock
        self._state: NoticeState | None = None
        self._retry_at = 0.0

    async def check(self, token: Token | None, dead_token: Token | None) -> None:
        """Call periodically (the service does every minute); cheap unless a notice is actually due."""
        now = self._clock()
        if now < self._retry_at:
            return
        try:
            if dead_token is not None:
                await self._notify_reauth_needed(dead_token)
            elif token is not None:
                await self._maybe_warn(token, now)
        except NotificationError as exc:
            self._retry_at = now + RETRY_AFTER_FAILURE_S
            log.error("notice_failed", extra={"error": str(exc), "retry_in_s": RETRY_AFTER_FAILURE_S})

    async def _maybe_warn(self, token: Token, now: float) -> None:
        state = self._state_for(token)
        today = datetime.fromtimestamp(now, self._tz).date()
        expires = datetime.fromtimestamp(token.refresh_expires_at, self._tz)
        due = due_warning(today, expires.date(), state.warnings_sent)
        if due is None:
            return
        days_left = (expires.date() - today).days
        when = "hoy" if days_left == 0 else f"en {days_left} día{'s' if days_left != 1 else ''}"
        await self._notifier.send(
            f"El login de Spotify caduca {when}",
            f"El acceso del servicio a tu cuenta de Spotify caduca hacia el "
            f"{expires.strftime('%Y-%m-%d %H:%M %Z')} (estimado: Spotify no da la hora exacta).\n\n"
            f"{self._login_help()}",
        )
        state.warnings_sent.append(due)
        self._save(state)
        log.info("expiry_warning_sent", extra={"days_before": due})

    async def _notify_reauth_needed(self, dead_token: Token) -> None:
        state = self._state_for(dead_token)
        if state.reauth_notice_sent:
            return
        await self._notifier.send(
            "El servicio ha perdido el acceso a Spotify",
            "Spotify ha rechazado el token del servicio (caducado o revocado). El servicio sigue en marcha "
            "pero no hace peticiones hasta que vuelvas a hacer login; detectará el nuevo token por sí solo.\n\n"
            f"{self._login_help()}",
        )
        state.reauth_notice_sent = True
        self._save(state)

    def _login_help(self) -> str:
        port = self._redirect_port
        return (
            "Para renovarlo, desde tu portátil:\n"
            f"    ssh -L {port}:127.0.0.1:{port} <tu-servidor>\n"
            "y en esa sesión, dentro del directorio del proyecto:\n"
            f"    docker compose run --rm -p 127.0.0.1:{port}:{port} regulator login\n"
            "Abre en el navegador del portátil la URL que imprime y acepta."
        )

    def _state_for(self, token: Token) -> NoticeState:
        if self._state is None:
            self._state = self._load()
        if self._state is None or self._state.authorized_at != token.authorized_at:
            self._state = NoticeState(authorized_at=token.authorized_at)
        return self._state

    def _load(self) -> NoticeState | None:
        try:
            return NoticeState(**json.loads(self._path.read_text()))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError):
            log.warning("notice_state_unreadable", extra={"path": str(self._path)})
            return None

    def _save(self, state: NoticeState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        os.replace(tmp, self._path)
