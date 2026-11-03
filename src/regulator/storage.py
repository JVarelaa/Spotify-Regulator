"""SQLite storage: detector events, recently-played entries, rule activity and small service state.

Origin of each listen: a `recent_plays` row points to the detector event it was paired with
(`event_id`). NULL means Spotify knows the play but the detector never saw it, so
"what the detector misses" is a query, and neither source overwrites the other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .reconstructor import ListenEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    track_id TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist_ids TEXT NOT NULL,       -- JSON list
    artist_names TEXT NOT NULL,     -- JSON list
    album_id TEXT,
    album_name TEXT,
    duration_ms INTEGER NOT NULL,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER NOT NULL,   -- last time the detector saw the play
    position_ms INTEGER NOT NULL,   -- last seen position (the threshold is measured by position)
    outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'skipped', 'interrupted')),
    is_repeat INTEGER NOT NULL,
    device TEXT,
    context_uri TEXT
);
CREATE INDEX IF NOT EXISTS events_track_end ON events (track_id, ended_at_ms);

CREATE TABLE IF NOT EXISTS recent_plays (
    track_id TEXT NOT NULL,
    played_at_ms INTEGER NOT NULL,  -- Spotify's played_at: when the play ended
    track_name TEXT NOT NULL,
    artist_ids TEXT NOT NULL,
    artist_names TEXT NOT NULL,
    album_id TEXT,
    album_name TEXT,
    duration_ms INTEGER NOT NULL,
    context_uri TEXT,
    event_id INTEGER REFERENCES events (id),  -- NULL = only known from recently-played
    PRIMARY KEY (track_id, played_at_ms)
);
-- One recently-played entry per detector event at most.
CREATE UNIQUE INDEX IF NOT EXISTS recent_plays_event ON recent_plays (event_id) WHERE event_id IS NOT NULL;

-- F2: every play of a song that had already reached its daily limit, and what the rule did.
CREATE TABLE IF NOT EXISTS daily_limit_events (
    id INTEGER PRIMARY KEY,
    track_id TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist_names TEXT NOT NULL,         -- JSON list
    day TEXT NOT NULL,                  -- YYYY-MM-DD; the day starts at day_starts_at
    play_started_ms INTEGER NOT NULL,   -- same as events.started_at_ms of that play
    detected_at_ms INTEGER NOT NULL,
    resolved_at_ms INTEGER NOT NULL,
    plays_today INTEGER NOT NULL,       -- listens already counted when the play started
    max_plays INTEGER NOT NULL,
    dry_run INTEGER NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('would_skip', 'skipped', 'failed', 'cancelled')),
    error TEXT
);
-- One row per play, also across restarts.
CREATE UNIQUE INDEX IF NOT EXISTS daily_limit_events_play ON daily_limit_events (track_id, play_started_ms);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_MATCH_MARGIN_MS = 20_000


@dataclass(frozen=True)
class DailyLimitEvent:
    track_id: str
    track_name: str
    artist_names: tuple[str, ...]
    day: str
    play_started_ms: int
    detected_at_ms: int
    resolved_at_ms: int
    plays_today: int
    max_plays: int
    dry_run: bool
    result: str
    error: str | None = None


@dataclass(frozen=True)
class RecentPlay:
    track_id: str
    played_at_ms: int
    track_name: str
    artist_ids: tuple[str, ...]
    artist_names: tuple[str, ...]
    album_id: str | None
    album_name: str | None
    duration_ms: int
    context_uri: str | None


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        # WAL lets the stats web (F4) read while the service writes.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def add_event(self, e: ListenEvent) -> int:
        with self._db:
            cur = self._db.execute(
                """INSERT INTO events (track_id, track_name, artist_ids, artist_names, album_id, album_name,
                       duration_ms, started_at_ms, ended_at_ms, position_ms, outcome, is_repeat, device, context_uri)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.track_id,
                    e.track_name,
                    json.dumps(list(e.artist_ids)),
                    json.dumps(list(e.artist_names), ensure_ascii=False),
                    e.album_id,
                    e.album_name,
                    e.duration_ms,
                    e.started_at_ms,
                    e.ended_at_ms,
                    e.position_ms,
                    e.outcome.value,
                    int(e.is_repeat),
                    e.device,
                    e.context_uri,
                ),
            )
        return cur.lastrowid

    def add_recent_plays(self, plays: Iterable[RecentPlay]) -> int:
        """Insert new entries, ignoring ones already stored (same track_id and played_at)."""
        added = 0
        with self._db:
            for p in plays:
                cur = self._db.execute(
                    """INSERT OR IGNORE INTO recent_plays (track_id, played_at_ms, track_name, artist_ids,
                           artist_names, album_id, album_name, duration_ms, context_uri)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.track_id,
                        p.played_at_ms,
                        p.track_name,
                        json.dumps(list(p.artist_ids)),
                        json.dumps(list(p.artist_names), ensure_ascii=False),
                        p.album_id,
                        p.album_name,
                        p.duration_ms,
                        p.context_uri,
                    ),
                )
                added += cur.rowcount
        return added

    def link_recent_plays(self, *, since_ms: int, margin_ms: int = DEFAULT_MATCH_MARGIN_MS) -> int:
        """Pair still-unlinked recently-played entries with detector events.

        Same track, and played_at within `margin_ms` of the event's last-seen time. The
        closest free event wins; each event is paired at most once. Returns pairs made.
        """
        linked = 0
        with self._db:
            pending = self._db.execute(
                """SELECT track_id, played_at_ms FROM recent_plays
                   WHERE event_id IS NULL AND played_at_ms >= ? ORDER BY played_at_ms""",
                (since_ms,),
            ).fetchall()
            for track_id, played_at in pending:
                row = self._db.execute(
                    """SELECT e.id FROM events e
                       WHERE e.track_id = ? AND ABS(e.ended_at_ms - ?) <= ?
                         AND NOT EXISTS (SELECT 1 FROM recent_plays r WHERE r.event_id = e.id)
                       ORDER BY ABS(e.ended_at_ms - ?) LIMIT 1""",
                    (track_id, played_at, margin_ms, played_at),
                ).fetchone()
                if row:
                    self._db.execute(
                        "UPDATE recent_plays SET event_id = ? WHERE track_id = ? AND played_at_ms = ?",
                        (row[0], track_id, played_at),
                    )
                    linked += 1
        return linked

    def count_listens(self, track_id: str, *, since_ms: int, margin_ms: int = DEFAULT_MATCH_MARGIN_MS) -> int:
        """Listens of a track since `since_ms`: completed detector plays plus recently-played entries
        the detector never saw. When both saw a play, the detector's outcome decides, also before
        the next sync pairs them (an entry with a detector event within the margin is skipped)."""
        row = self._db.execute(
            """SELECT
                 (SELECT count(*) FROM events
                   WHERE track_id = ? AND outcome = 'completed' AND ended_at_ms >= ?)
               + (SELECT count(*) FROM recent_plays r
                   WHERE r.track_id = ? AND r.played_at_ms >= ? AND r.event_id IS NULL
                     AND NOT EXISTS (SELECT 1 FROM events e
                                     WHERE e.track_id = r.track_id AND ABS(e.ended_at_ms - r.played_at_ms) <= ?))""",
            (track_id, since_ms, track_id, since_ms, margin_ms),
        ).fetchone()
        return row[0]

    def add_daily_limit_event(self, e: DailyLimitEvent) -> None:
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO daily_limit_events (track_id, track_name, artist_names, day, play_started_ms,
                       detected_at_ms, resolved_at_ms, plays_today, max_plays, dry_run, result, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.track_id,
                    e.track_name,
                    json.dumps(list(e.artist_names), ensure_ascii=False),
                    e.day,
                    e.play_started_ms,
                    e.detected_at_ms,
                    e.resolved_at_ms,
                    e.plays_today,
                    e.max_plays,
                    int(e.dry_run),
                    e.result,
                    e.error,
                ),
            )

    def has_daily_limit_event(self, track_id: str, play_started_ms: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM daily_limit_events WHERE track_id = ? AND play_started_ms = ?", (track_id, play_started_ms)
        ).fetchone()
        return row is not None

    def daily_limit_acted_on(self, track_id: str, day: str) -> bool:
        """Did the rule already act (or would have) on this track that day? Cancelled plays don't count."""
        row = self._db.execute(
            "SELECT 1 FROM daily_limit_events WHERE track_id = ? AND day = ? AND result != 'cancelled' LIMIT 1",
            (track_id, day),
        ).fetchone()
        return row is not None

    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_state(self, key: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM state WHERE key = ?", (key,))

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Read-only helper for stats and tests."""
        return self._db.execute(sql, params).fetchall()
