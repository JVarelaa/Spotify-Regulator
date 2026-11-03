"""F4: statistics over the database (read-only).

Listens follow the shared definition (storage.LISTENS_SQL), so the stats and the daily limit
always agree. Periods are made of the same days as the limit: a day starts at `day_starts_at`
(04:00 by default) in the configured timezone. Weeks start on Monday.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .storage import DEFAULT_MATCH_MARGIN_MS, LISTENS_SQL

PeriodKind = Literal["day", "week", "month"]

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
TOP_N = 10


@dataclass(frozen=True)
class Period:
    kind: PeriodKind
    first_day: date
    end_day: date  # exclusive

    @classmethod
    def containing(cls, kind: PeriodKind, day: date) -> Period:
        if kind == "day":
            return cls(kind, day, day + timedelta(days=1))
        if kind == "week":
            start = day - timedelta(days=day.weekday())
            return cls(kind, start, start + timedelta(days=7))
        start = day.replace(day=1)
        return cls(kind, start, (start + timedelta(days=32)).replace(day=1))

    def previous(self) -> Period:
        return Period.containing(self.kind, self.first_day - timedelta(days=1))

    def next(self) -> Period:
        return Period.containing(self.kind, self.end_day)

    @property
    def last_day(self) -> date:
        return self.end_day - timedelta(days=1)

    def label(self) -> str:
        first, last = self.first_day, self.last_day
        if self.kind == "day":
            return f"{WEEKDAYS[first.weekday()]} {first.day} de {MONTHS[first.month - 1]} de {first.year}"
        if self.kind == "month":
            return f"{MONTHS[first.month - 1]} de {first.year}"
        if first.month == last.month:
            return f"del {first.day} al {last.day} de {MONTHS[last.month - 1]} de {last.year}"
        if first.year == last.year:
            return f"del {first.day} de {MONTHS[first.month - 1]} al {last.day} de {MONTHS[last.month - 1]} de {last.year}"
        return (f"del {first.day} de {MONTHS[first.month - 1]} de {first.year} "
                f"al {last.day} de {MONTHS[last.month - 1]} de {last.year}")


@dataclass(frozen=True)
class Listen:
    track_id: str
    track_name: str
    artists: tuple[tuple[str, str], ...]  # (id, name)
    at_ms: int


@dataclass(frozen=True)
class Ranked:
    name: str
    detail: str
    count: int


@dataclass(frozen=True)
class Summary:
    listens: int
    songs: int
    artists: int
    top_songs: list[Ranked]
    top_artists: list[Ranked]
    repetition: float | None  # listens of the most repeated song / all listens
    completed: int  # detector plays, for the skip rate
    skipped: int
    limit_results: dict[str, int]  # daily-limit events by result
    limit_top: list[Ranked]  # songs that went over their limit most often

    @property
    def most_repeated(self) -> Ranked | None:
        return self.top_songs[0] if self.top_songs else None

    @property
    def skip_rate(self) -> float | None:
        plays = self.completed + self.skipped
        return self.skipped / plays if plays else None

    @property
    def limit_total(self) -> int:
        return sum(self.limit_results.values())


@dataclass(frozen=True)
class WeekDiversity:
    first_day: date
    listens: int
    artists: int
    new_artists: int  # first ever listened that week
    entropy_bits: float  # Shannon entropy of how listens spread over artists


def shannon_entropy(counts: Counter) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum(c / total * math.log2(c / total) for c in counts.values() if c)


class Stats:
    def __init__(self, db_path: Path, tz: str, day_starts_at: time, *, margin_ms: int = DEFAULT_MATCH_MARGIN_MS):
        # Read-only: the stats can never modify what the service records.
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._tz = ZoneInfo(tz)
        self._starts_at = day_starts_at
        self._margin = margin_ms

    def close(self) -> None:
        self._db.close()

    def day_start_ms(self, day: date) -> int:
        return int(datetime.combine(day, self._starts_at, tzinfo=self._tz).timestamp() * 1000)

    def bounds_ms(self, period: Period) -> tuple[int, int]:
        return self.day_start_ms(period.first_day), self.day_start_ms(period.end_day)

    def listens(self, since_ms: int | None = None, until_ms: int | None = None) -> list[Listen]:
        rows = self._db.execute(
            f"SELECT track_id, track_name, artist_ids, artist_names, at_ms FROM ({LISTENS_SQL})"
            " WHERE at_ms >= ? AND at_ms < ? ORDER BY at_ms",
            (self._margin, since_ms if since_ms is not None else 0, until_ms if until_ms is not None else 2**62),
        ).fetchall()
        return [
            Listen(track_id, name, tuple(zip(json.loads(ids), json.loads(names), strict=False)), at_ms)
            for track_id, name, ids, names, at_ms in rows
        ]

    def first_listen_ms(self) -> int | None:
        row = self._db.execute(f"SELECT min(at_ms) FROM ({LISTENS_SQL})", (self._margin,)).fetchone()
        return row[0]

    def summary(self, period: Period) -> Summary:
        start, end = self.bounds_ms(period)
        listens = self.listens(start, end)

        songs = Counter(listen.track_id for listen in listens)
        song_info = {listen.track_id: listen for listen in listens}
        artist_counts, artist_names = _artist_counts(listens)

        outcomes = dict(self._db.execute(
            "SELECT outcome, count(*) FROM events WHERE ended_at_ms >= ? AND ended_at_ms < ? GROUP BY outcome",
            (start, end),
        ).fetchall())
        first, last = period.first_day.isoformat(), period.end_day.isoformat()
        limit_results = dict(self._db.execute(
            "SELECT result, count(*) FROM daily_limit_events WHERE day >= ? AND day < ? GROUP BY result", (first, last)
        ).fetchall())
        limit_top = [
            Ranked(name, ", ".join(json.loads(artists)), count)
            for name, artists, count in self._db.execute(
                """SELECT track_name, artist_names, count(*) AS n FROM daily_limit_events
                    WHERE day >= ? AND day < ? GROUP BY track_id ORDER BY n DESC, track_name LIMIT ?""",
                (first, last, TOP_N),
            )
        ]

        top_songs = [
            Ranked(song_info[tid].track_name, ", ".join(n for _, n in song_info[tid].artists), count)
            for tid, count in _top(songs)
        ]
        top_artists = [Ranked(artist_names[aid], "", count) for aid, count in _top(artist_counts)]
        return Summary(
            listens=len(listens),
            songs=len(songs),
            artists=len(artist_counts),
            top_songs=top_songs,
            top_artists=top_artists,
            repetition=top_songs[0].count / len(listens) if listens else None,
            completed=outcomes.get("completed", 0),
            skipped=outcomes.get("skipped", 0),
            limit_results=limit_results,
            limit_top=limit_top,
        )

    def diversity(self, last_week: Period, weeks: int = 8) -> list[WeekDiversity]:
        """The `weeks` weeks ending with `last_week`, oldest first."""
        periods = [last_week]
        while len(periods) < weeks:
            periods.insert(0, periods[0].previous())
        first_seen: dict[str, int] = {}
        for listen in self.listens():  # all of them: "new" means never listened before
            for artist_id, _ in listen.artists:
                first_seen.setdefault(artist_id, listen.at_ms)
        result = []
        for week in periods:
            start, end = self.bounds_ms(week)
            listens = self.listens(start, end)
            counts, _ = _artist_counts(listens)
            result.append(WeekDiversity(
                first_day=week.first_day,
                listens=len(listens),
                artists=len(counts),
                new_artists=sum(1 for aid in counts if start <= first_seen[aid] < end),
                entropy_bits=shannon_entropy(counts),
            ))
        return result

    def events_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "track_id", "track_name", "artist_names", "album_name", "duration_ms", "started_at",
                         "ended_at", "started_at_ms", "ended_at_ms", "position_ms", "outcome", "is_repeat", "device",
                         "context_uri"])
        for row in self._db.execute(
            """SELECT id, track_id, track_name, artist_names, album_name, duration_ms, started_at_ms, ended_at_ms,
                      position_ms, outcome, is_repeat, device, context_uri FROM events ORDER BY id"""
        ):
            (id_, track_id, name, artists, album, duration, started, ended, position, outcome, repeat, device,
             context) = row
            writer.writerow([id_, track_id, name, "; ".join(json.loads(artists)), album, duration,
                             self._local(started), self._local(ended), started, ended, position, outcome, repeat,
                             device, context])
        return out.getvalue()

    def daily_limit_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "day", "track_id", "track_name", "artist_names", "play_started_at", "detected_at",
                         "resolved_at", "play_started_ms", "plays_today", "max_plays", "dry_run", "result", "error"])
        for row in self._db.execute(
            """SELECT id, day, track_id, track_name, artist_names, play_started_ms, detected_at_ms, resolved_at_ms,
                      plays_today, max_plays, dry_run, result, error FROM daily_limit_events ORDER BY id"""
        ):
            id_, day, track_id, name, artists, started, detected, resolved, plays, max_plays, dry, result, error = row
            writer.writerow([id_, day, track_id, name, "; ".join(json.loads(artists)), self._local(started),
                             self._local(detected), self._local(resolved), started, plays, max_plays, dry, result,
                             error])
        return out.getvalue()

    def _local(self, ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, self._tz).isoformat(timespec="seconds")


def _artist_counts(listens: list[Listen]) -> tuple[Counter, dict[str, str]]:
    """Listens per artist (a song with several artists counts for each), and their names."""
    counts: Counter = Counter()
    names: dict[str, str] = {}
    for listen in listens:
        for artist_id, name in listen.artists:
            counts[artist_id] += 1
            names[artist_id] = name
    return counts, names


def _top(counts: Counter) -> list[tuple[str, int]]:
    # Ties keep first-listened order (Counter preserves insertion order), so the list is stable.
    return sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_N]
