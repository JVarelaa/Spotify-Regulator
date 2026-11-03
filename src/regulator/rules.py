"""F2: daily limit of listens per song.

What counts as a listen was decided in phase 2: a detector play that reached the completion
threshold, or a recently-played entry the detector never saw. The "day" starts at
`day_starts_at` in the configured timezone, so a late-night session isn't split in two.

When a song that already has its limit of listens today starts playing, the rule waits until it
has played `skip_after_s` (by position, and not paused) and then skips it, turning repeat-one off
first so it doesn't come straight back. In dry-run it only records that it would have. Every
over-limit play is stored in `daily_limit_events` for the stats, including the ones you skipped
yourself before the rule acted (`cancelled`). Only the first action per song and day is notified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from .auth import AuthError
from .config import DailyLimitConfig
from .reconstructor import Sample
from .spotify import SpotifyError, SpotifyUnavailable
from .storage import DailyLimitEvent, Storage

log = logging.getLogger(__name__)


class Result(StrEnum):
    WOULD_SKIP = "would_skip"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Notice:
    subject: str
    body: str


class PlayerCommands(Protocol):
    async def skip_to_next(self, device_id: str | None = None) -> None: ...
    async def set_repeat(self, state: Literal["off", "context", "track"], device_id: str | None = None) -> None: ...


def regulator_day(now_ms: int, tz: ZoneInfo, starts_at: time) -> tuple[date, int]:
    """The day `now_ms` belongs to, and when that day started (epoch ms)."""
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    day = local.date() if local.time() >= starts_at else local.date() - timedelta(days=1)
    return day, int(datetime.combine(day, starts_at, tzinfo=tz).timestamp() * 1000)


@dataclass
class _OverLimit:
    """A play of a song that had already reached its limit when it started, waiting for the rule."""

    sample: Sample
    play_started_ms: int
    detected_at_ms: int
    day: date
    plays_today: int
    max_plays: int


class DailyLimitRule:
    def __init__(self, config: DailyLimitConfig, tz: str, storage: Storage, player: PlayerCommands):
        self._config = config
        self._tz = ZoneInfo(tz)
        self._storage = storage
        self._player = player
        self._checked: tuple[str, int] | None = None  # the last play evaluated: (track, start)
        self._pending: _OverLimit | None = None

    async def check(self, sample: Sample | None, play_started_ms: int | None, now_ms: int) -> list[Notice]:
        """Call with the play in progress after every player round. Returns notices to send."""
        play = (sample.track_id, play_started_ms) if sample and play_started_ms is not None else None
        pending = self._pending
        if pending and play != (pending.sample.track_id, pending.play_started_ms):
            # The over-limit play ended before the rule acted: you moved on yourself.
            self._pending = None
            self._record(pending, now_ms, Result.CANCELLED)
        if play is None:
            return []
        if play != self._checked:
            self._checked = play
            self._pending = self._evaluate(sample, play_started_ms, now_ms)
        pending = self._pending
        if pending and sample.is_playing and sample.progress_ms >= self._config.skip_after_s * 1000:
            self._pending = None
            return await self._act(pending, sample, now_ms)
        return []

    def _evaluate(self, sample: Sample, play_started_ms: int, now_ms: int) -> _OverLimit | None:
        if self._storage.has_daily_limit_event(sample.track_id, play_started_ms):
            return None  # already handled before a restart
        day, day_start_ms = regulator_day(now_ms, self._tz, self._config.day_starts_at)
        plays = self._storage.count_listens(sample.track_id, since_ms=day_start_ms)
        limit = self._config.limit_for(sample.track_id)
        if plays < limit:
            return None
        log.info("daily_limit_reached", extra={"track": sample.track_name, "plays_today": plays, "max_plays": limit})
        return _OverLimit(sample, play_started_ms, now_ms, day, plays, limit)

    async def _act(self, over: _OverLimit, sample: Sample, now_ms: int) -> list[Notice]:
        error = None
        if self._config.dry_run:
            result = Result.WOULD_SKIP
        else:
            try:
                if sample.repeat_state == "track":
                    await self._player.set_repeat("off")
                await self._player.skip_to_next()
                result = Result.SKIPPED
            except (SpotifyError, SpotifyUnavailable, AuthError) as exc:
                result, error = Result.FAILED, str(exc)
        first_today = not self._storage.daily_limit_acted_on(sample.track_id, over.day.isoformat())
        self._record(over, now_ms, result, error)
        log.info("daily_limit_applied", extra={"track": sample.track_name, "result": result.value, "error": error})
        return [_notice(over, result, error)] if first_today else []

    def _record(self, over: _OverLimit, now_ms: int, result: Result, error: str | None = None) -> None:
        s = over.sample
        self._storage.add_daily_limit_event(
            DailyLimitEvent(
                track_id=s.track_id,
                track_name=s.track_name,
                artist_names=s.artist_names,
                day=over.day.isoformat(),
                play_started_ms=over.play_started_ms,
                detected_at_ms=over.detected_at_ms,
                resolved_at_ms=now_ms,
                plays_today=over.plays_today,
                max_plays=over.max_plays,
                dry_run=self._config.dry_run,
                result=result.value,
                error=error,
            )
        )


def _notice(over: _OverLimit, result: Result, error: str | None) -> Notice:
    s = over.sample
    song = f"«{s.track_name}» de {', '.join(s.artist_names)}"
    count = f"{over.plays_today}/{over.max_plays} hoy"
    later = "Si vuelve a sonar hoy, solo quedará registrado."
    if result is Result.FAILED:
        return Notice(
            f"Límite diario: no se pudo saltar «{s.track_name}»",
            f"{song} ha llegado al límite ({count}), pero el salto falló: {error}\n\n{later}",
        )
    if result is Result.SKIPPED:
        body = f"{song} ha llegado al límite ({count}) y se ha saltado."
    else:
        body = (
            f"{song} ha llegado al límite ({count}). La regla está en modo simulación (dry_run), "
            "así que no se ha saltado; en modo real se habría saltado."
        )
    return Notice(f"Límite alcanzado: «{s.track_name}», {count}", f"{body}\n\n{later}")
