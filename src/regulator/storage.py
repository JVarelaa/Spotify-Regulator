"""SQLite storage: detector events, recently-played entries and small service state.

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

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_MATCH_MARGIN_MS = 20_000


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
