"""Turn periodic /me/player snapshots into listening events.

Rules (decided for this project):
- The completion threshold is measured by *position*: a play is completed when its last seen
  position reached 50% of the track or 4 minutes, whichever comes first.
- Track changes -> the previous play closes as completed or skipped.
- Same track and the position goes back (restart, repeat-one, or rewinding the bar) -> the
  previous play closes and a new one starts, flagged as a repeat. Repeats are classified too.
- Pauses produce nothing. When music stops (nothing playing, podcast, ad) the play closes as
  completed or, below the threshold, as interrupted, which is not a skip.

Pure logic: no network, no database. State lives in memory; snapshot()/restore() let the
service keep the play in progress across a restart instead of storing it twice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class Outcome(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Sample:
    """One /me/player snapshot reduced to what the reconstructor needs."""

    observed_at_ms: int
    track_id: str
    track_name: str
    artist_ids: tuple[str, ...]
    artist_names: tuple[str, ...]
    album_id: str | None
    album_name: str | None
    duration_ms: int
    progress_ms: int
    is_playing: bool
    device: str | None
    context_uri: str | None


@dataclass(frozen=True)
class ListenEvent:
    track_id: str
    track_name: str
    artist_ids: tuple[str, ...]
    artist_names: tuple[str, ...]
    album_id: str | None
    album_name: str | None
    duration_ms: int
    started_at_ms: int  # estimated: when the first sample was seen minus its position
    ended_at_ms: int  # when the play was last seen
    position_ms: int  # last seen position; with the position rule this is the "ms listened"
    outcome: Outcome
    is_repeat: bool
    device: str | None
    context_uri: str | None


def sample_from_playback(payload: dict | None, observed_at_ms: int) -> Sample | None:
    """Reduce a GET /me/player response to a Sample; None if no music track is playing."""
    if not payload or payload.get("currently_playing_type") != "track":
        return None  # nothing playing (204), podcast episode, ad or unknown
    item = payload.get("item")
    if not item or item.get("type") != "track":
        return None
    track_id = item.get("id") or item.get("uri")  # local files have no id, only a uri
    if not track_id:
        return None
    album = item.get("album") or {}
    artists = item.get("artists") or []
    return Sample(
        observed_at_ms=observed_at_ms,
        track_id=track_id,
        track_name=item.get("name") or "",
        artist_ids=tuple(a.get("id") or a.get("uri") or "" for a in artists),
        artist_names=tuple(a.get("name") or "" for a in artists),
        album_id=album.get("id"),
        album_name=album.get("name"),
        duration_ms=int(item.get("duration_ms") or 0),
        progress_ms=int(payload.get("progress_ms") or 0),
        is_playing=bool(payload.get("is_playing")),
        device=(payload.get("device") or {}).get("name"),
        context_uri=(payload.get("context") or {}).get("uri"),
    )


@dataclass
class _Play:
    first: Sample
    last: Sample
    is_repeat: bool


class Reconstructor:
    def __init__(
        self,
        *,
        completion_ratio: float = 0.5,
        completion_max_ms: int = 240_000,
        rewind_tolerance_ms: int = 3_000,
        resume_tolerance_ms: int = 10_000,
    ):
        self._ratio = completion_ratio
        self._max_ms = completion_max_ms
        # Spotify's reported position wobbles a little between snapshots; only a bigger jump
        # backwards counts as a restart or rewind.
        self._rewind_tolerance = rewind_tolerance_ms
        self._resume_tolerance = resume_tolerance_ms
        self._play: _Play | None = None
        # True right after restore(): the restored play is kept only if the next sample continues it.
        self._resumed = False

    @property
    def current(self) -> Sample | None:
        """Latest sample of the play in progress, if any (the rules engine will use this)."""
        return self._play.last if self._play else None

    def observe(self, sample: Sample | None) -> list[ListenEvent]:
        """Feed one snapshot (None = no music playing). Returns the plays it closed."""
        if self._resumed:
            self._resumed = False
            if not self._continues(sample):
                # We don't know how the restored play ended, so it's dropped instead of stored with
                # guessed data. recently-played reports it if it lasted more than 30 s.
                self._play = None
        if sample is None:
            return self._close(stopped=True)
        play = self._play
        if play is None:
            self._play = _Play(first=sample, last=sample, is_repeat=False)
            return []
        if sample.track_id != play.last.track_id:
            events = self._close(stopped=False)
            self._play = _Play(first=sample, last=sample, is_repeat=False)
            return events
        if sample.progress_ms < play.last.progress_ms - self._rewind_tolerance:
            events = self._close(stopped=False)
            self._play = _Play(first=sample, last=sample, is_repeat=True)
            return events
        play.last = sample
        return []

    def snapshot(self) -> dict | None:
        """The play in progress as plain (JSON-friendly) data, to carry it across a restart."""
        play = self._play
        if play is None:
            return None
        return {"first": asdict(play.first), "last": asdict(play.last), "is_repeat": play.is_repeat}

    def restore(self, snapshot: dict) -> None:
        """Take back a play saved by a previous run. It stays provisional until the next sample.

        Raises KeyError, TypeError or ValueError if the snapshot is malformed.
        """
        self._play = _Play(
            first=_sample_from_dict(snapshot["first"]),
            last=_sample_from_dict(snapshot["last"]),
            is_repeat=bool(snapshot["is_repeat"]),
        )
        self._resumed = True

    def _continues(self, sample: Sample | None) -> bool:
        """Is `sample` the restored play carrying on? Same track, and the position moved as much as
        the time that passed (none if it was paused). A play that restarted meanwhile, e.g. with
        repeat-one, would be somewhere else."""
        last = self._play.last if self._play else None
        if last is None or sample is None or sample.track_id != last.track_id:
            return False
        elapsed = sample.observed_at_ms - last.observed_at_ms
        expected = elapsed if last.is_playing else 0
        return abs((sample.progress_ms - last.progress_ms) - expected) <= self._resume_tolerance

    def threshold_ms(self, duration_ms: int) -> int:
        if duration_ms <= 0:
            return self._max_ms
        return min(int(duration_ms * self._ratio), self._max_ms)

    def _close(self, *, stopped: bool) -> list[ListenEvent]:
        play, self._play = self._play, None
        if play is None:
            return []
        last = play.last
        if last.progress_ms >= self.threshold_ms(last.duration_ms):
            outcome = Outcome.COMPLETED
        else:
            outcome = Outcome.INTERRUPTED if stopped else Outcome.SKIPPED
        first = play.first
        return [
            ListenEvent(
                track_id=last.track_id,
                track_name=last.track_name,
                artist_ids=last.artist_ids,
                artist_names=last.artist_names,
                album_id=last.album_id,
                album_name=last.album_name,
                duration_ms=last.duration_ms,
                started_at_ms=first.observed_at_ms - first.progress_ms,
                ended_at_ms=last.observed_at_ms,
                position_ms=last.progress_ms,
                outcome=outcome,
                is_repeat=play.is_repeat,
                device=last.device,
                context_uri=first.context_uri,
            )
        ]



def _sample_from_dict(data: dict) -> Sample:
    # JSON turns the tuples into lists.
    return Sample(**{**data, "artist_ids": tuple(data["artist_ids"]), "artist_names": tuple(data["artist_names"])})
