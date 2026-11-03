import sqlite3
from collections import Counter
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from regulator.config import Config
from regulator.reconstructor import ListenEvent, Outcome
from regulator.stats import Period, Stats, shannon_entropy
from regulator.storage import DailyLimitEvent, RecentPlay, Storage
from regulator.web import create_app

TZ = "Atlantic/Canary"
FOUR = time(4, 0)
MIN = 60_000


def ms(day: int, hh: int, mm: int = 0, *, month: int = 11) -> int:
    return int(datetime(2026, month, day, hh, mm, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)


def listen(track, ended_at, outcome=Outcome.COMPLETED, *, artists=("ar1",), name=None) -> ListenEvent:
    return ListenEvent(
        track_id=track,
        track_name=name or f"Song {track}",
        artist_ids=tuple(artists),
        artist_names=tuple(f"Artist {a}" for a in artists),
        album_id="al",
        album_name="Album",
        duration_ms=4 * MIN,
        started_at_ms=ended_at - 3 * MIN,
        ended_at_ms=ended_at,
        position_ms=3 * MIN,
        outcome=outcome,
        is_repeat=False,
        device="iPhone",
        context_uri=None,
    )


def recent(track, played_at, *, artists=("ar1",)) -> RecentPlay:
    return RecentPlay(track, played_at, f"Song {track}", tuple(artists), tuple(f"Artist {a}" for a in artists),
                      "al", "Album", 4 * MIN, None)


def limit_event(track, day, result="would_skip") -> DailyLimitEvent:
    return DailyLimitEvent(track, f"Song {track}", ("Artist ar1",), day, ms(3, 12), ms(3, 12), ms(3, 12), 3, 3,
                           True, result)


@pytest.fixture
def db(tmp_path):
    """Tuesday 3 Nov 2026: A x3 (one only in recently-played), B x1 with two artists, a skip of C,
    an interruption of A. Plus one listen of A at 03:30 on the 4th, which still belongs to the 3rd."""
    storage = Storage(tmp_path / "regulator.db")
    storage.add_event(listen("A", ms(3, 10)))
    storage.add_event(listen("A", ms(3, 11)))
    storage.add_event(listen("B", ms(3, 12), artists=("ar1", "ar2")))
    storage.add_event(listen("C", ms(3, 13), Outcome.SKIPPED, artists=("ar3",)))
    storage.add_event(listen("A", ms(3, 14), Outcome.INTERRUPTED))
    storage.add_event(listen("A", ms(4, 3, 30)))
    storage.add_recent_plays([
        recent("A", ms(3, 9)),  # only recently-played: counts
        recent("A", ms(3, 10) + 5_000),  # the detector's 10:00 play: not twice
    ])
    storage.add_daily_limit_event(limit_event("A", "2026-11-03"))
    storage.add_daily_limit_event(DailyLimitEvent("A", "Song A", ("Artist ar1",), "2026-11-03", ms(3, 15), ms(3, 15),
                                                  ms(3, 15), 4, 3, True, "cancelled"))
    storage.add_daily_limit_event(limit_event("B", "2026-11-05"))
    storage.close()
    stats = Stats(tmp_path / "regulator.db", TZ, FOUR)
    yield stats
    stats.close()


# --- periods -------------------------------------------------------------------------------------


def test_periods_and_labels():
    tuesday = date(2026, 11, 3)
    week = Period.containing("week", tuesday)
    assert (week.first_day, week.end_day) == (date(2026, 11, 2), date(2026, 11, 9))
    assert week.label() == "del 2 al 8 de noviembre de 2026"
    assert week.previous().label() == "del 26 de octubre al 1 de noviembre de 2026"
    assert Period.containing("day", tuesday).label() == "martes 3 de noviembre de 2026"
    month = Period.containing("month", tuesday)
    assert (month.first_day, month.end_day, month.label()) == (date(2026, 11, 1), date(2026, 12, 1), "noviembre de 2026")
    assert Period.containing("month", date(2026, 12, 31)).next().first_day == date(2027, 1, 1)


def test_period_bounds_follow_the_04_00_day(db):
    start, end = db.bounds_ms(Period.containing("day", date(2026, 11, 3)))
    assert (start, end) == (ms(3, 4), ms(4, 4))


# --- summary -------------------------------------------------------------------------------------


def test_summary_counts_listens_like_the_daily_limit(db):
    s = db.summary(Period.containing("day", date(2026, 11, 3)))
    # A: 09:00 (recently-played only), 10:00, 11:00 and 03:30 of the 4th; B: 12:00. Not the skip or the interruption.
    assert s.listens == 5
    assert s.songs == 2
    assert [(r.name, r.count) for r in s.top_songs] == [("Song A", 4), ("Song B", 1)]
    assert s.top_songs[1].detail == "Artist ar1, Artist ar2"
    # A song with two artists counts for both.
    assert [(r.name, r.count) for r in s.top_artists] == [("Artist ar1", 5), ("Artist ar2", 1)]
    assert s.artists == 2
    assert s.repetition == pytest.approx(4 / 5)


def test_skip_rate_uses_detector_plays_only(db):
    s = db.summary(Period.containing("day", date(2026, 11, 3)))
    assert (s.completed, s.skipped) == (4, 1)  # the interruption is neither
    assert s.skip_rate == pytest.approx(1 / 5)


def test_daily_limit_events_by_result_and_song(db):
    s = db.summary(Period.containing("day", date(2026, 11, 3)))
    assert s.limit_results == {"would_skip": 1, "cancelled": 1}
    assert s.limit_total == 2
    assert [(r.name, r.count) for r in s.limit_top] == [("Song A", 2)]
    week = db.summary(Period.containing("week", date(2026, 11, 3)))
    assert week.limit_total == 3


def test_empty_period(db):
    s = db.summary(Period.containing("day", date(2026, 10, 1)))
    assert (s.listens, s.repetition, s.skip_rate, s.top_songs) == (0, None, None, [])


# --- diversity -----------------------------------------------------------------------------------


def test_shannon_entropy():
    assert shannon_entropy(Counter()) == 0
    assert shannon_entropy(Counter({"a": 5})) == 0
    assert shannon_entropy(Counter({"a": 1, "b": 1, "c": 1, "d": 1})) == pytest.approx(2.0)


def test_diversity_new_artists_and_entropy(tmp_path):
    storage = Storage(tmp_path / "regulator.db")
    # Week of 26 Oct: ar1 x2, ar2 x2. Week of 2 Nov: ar2, ar3, ar4 x2.
    for i, artist in enumerate(["ar1", "ar1", "ar2", "ar2"]):
        storage.add_event(listen(f"t{i}", ms(27, 12 + i, month=10), artists=(artist,)))
    for i, artist in enumerate(["ar2", "ar3", "ar4", "ar4"]):
        storage.add_event(listen(f"u{i}", ms(3, 12 + i), artists=(artist,)))
    storage.close()
    stats = Stats(tmp_path / "regulator.db", TZ, FOUR)
    weeks = stats.diversity(Period.containing("week", date(2026, 11, 3)), weeks=3)
    assert [w.first_day for w in weeks] == [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)]
    assert [(w.listens, w.artists, w.new_artists) for w in weeks] == [(0, 0, 0), (4, 2, 2), (4, 3, 2)]
    assert weeks[1].entropy_bits == pytest.approx(1.0)
    assert weeks[2].entropy_bits == pytest.approx(1.5)


def test_stats_cannot_write(db):
    with pytest.raises(sqlite3.OperationalError):
        db._db.execute("DELETE FROM events")


# --- web -----------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    config = Config.model_validate({"data_dir": str(tmp_path)})
    return TestClient(create_app(config)), config


def test_web_without_database(client):
    web, _ = client
    page = web.get("/")
    assert page.status_code == 200 and "Todavía no hay base de datos" in page.text
    assert web.get("/health").status_code == 503
    assert web.get("/export/escuchas.csv").status_code == 404


def test_web_page_shows_the_period(client, db):
    web, _ = client
    page = web.get("/", params={"p": "day", "d": "2026-11-03"})
    assert page.status_code == 200
    assert "martes 3 de noviembre de 2026" in page.text
    assert "Song A" in page.text and "Artist ar2" in page.text
    assert "80 %" in page.text  # repetition
    assert web.get("/health").json() == {"ok": True}


def test_web_escapes_names_from_spotify(tmp_path, client):
    web, _ = client
    storage = Storage(tmp_path / "regulator.db")
    storage.add_event(listen("X", ms(3, 10), name="<script>alert(1)</script>"))
    storage.close()
    page = web.get("/", params={"p": "day", "d": "2026-11-03"})
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_web_never_shows_the_future(client, db):
    web, _ = client
    page = web.get("/", params={"p": "day", "d": "2999-01-01"})
    assert page.status_code == 200 and "2999" not in page.text
    assert 'aria-label="Periodo siguiente"' not in page.text


def test_csv_exports(client, db):
    web, _ = client
    events = web.get("/export/escuchas.csv")
    assert events.headers["content-type"].startswith("text/csv")
    assert events.content.startswith(b"\xef\xbb\xbf")  # BOM, for accents in Excel/LibreOffice
    lines = events.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("id,track_id,track_name,artist_names")
    assert len(lines) == 1 + 6
    assert "2026-11-03T10:00:00+00:00" in lines[1]  # local time (Canarias is UTC+0 in winter)
    limits = web.get("/export/limite-diario.csv").content.decode("utf-8-sig").splitlines()
    assert limits[0].startswith("id,day,track_id") and len(limits) == 1 + 3
