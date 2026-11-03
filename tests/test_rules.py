import asyncio
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from regulator.config import DailyLimitConfig
from regulator.reconstructor import ListenEvent, Outcome, Sample
from regulator.rules import DailyLimitRule, regulator_day
from regulator.spotify import SpotifyError
from regulator.storage import RecentPlay, Storage

TZ = "Atlantic/Canary"
SEC = 1_000
MIN = 60 * SEC


def ms(day: int, hh: int, mm: int = 0, ss: int = 0, *, month: int = 11, year: int = 2026) -> int:
    return int(datetime(year, month, day, hh, mm, ss, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)


def sample(track="A", progress_s=0.0, at=None, *, playing=True, repeat="off") -> Sample:
    return Sample(
        observed_at_ms=at if at is not None else ms(3, 12),
        track_id=track,
        track_name=f"Song {track}",
        artist_ids=("ar",),
        artist_names=("Artist",),
        album_id="al",
        album_name="Album",
        duration_ms=4 * MIN,
        progress_ms=int(progress_s * SEC),
        is_playing=playing,
        device="iPhone",
        context_uri=None,
        repeat_state=repeat,
    )


def listen(track="A", ended_at=None, outcome=Outcome.COMPLETED) -> ListenEvent:
    ended_at = ended_at if ended_at is not None else ms(3, 10)
    return ListenEvent(
        track_id=track,
        track_name=f"Song {track}",
        artist_ids=("ar",),
        artist_names=("Artist",),
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


def recent(track="A", played_at=None) -> RecentPlay:
    return RecentPlay(
        track_id=track,
        played_at_ms=played_at if played_at is not None else ms(3, 9),
        track_name=f"Song {track}",
        artist_ids=("ar",),
        artist_names=("Artist",),
        album_id="al",
        album_name="Album",
        duration_ms=4 * MIN,
        context_uri=None,
    )


class FakePlayer:
    def __init__(self, error=None):
        self.commands = []
        self.error = error

    async def skip_to_next(self, device_id=None):
        if self.error:
            raise self.error
        self.commands.append("next")

    async def set_repeat(self, state, device_id=None):
        if self.error:
            raise self.error
        self.commands.append(f"repeat:{state}")


def make(tmp_path, *, listens=3, player=None, **config):
    db = Storage(tmp_path / "r.db")
    for i in range(listens):
        db.add_event(listen(ended_at=ms(3, 10, i)))
    player = player or FakePlayer()
    rule = DailyLimitRule(DailyLimitConfig(**config), TZ, db, player)
    return rule, db, player


def run(rule, *steps):
    """Feed (sample, play_started_ms, now_ms) steps; return every notice."""

    async def scenario():
        notices = []
        for s, started, now in steps:
            notices += await rule.check(s, started, now)
        return notices

    return asyncio.run(scenario())


def rows(db):
    return db.execute("SELECT track_id, day, plays_today, max_plays, dry_run, result, error FROM daily_limit_events ORDER BY id")


START = ms(3, 12)  # when the 4th play of song A starts


def playing(progress_s, track="A", started=START, **kw):
    """Step for a play that started at `started` and is now at `progress_s`."""
    return (sample(track, progress_s, started + int(progress_s * SEC), **kw), started, started + int(progress_s * SEC))


# --- the day -------------------------------------------------------------------------------------


def test_day_starts_at_four_in_the_morning():
    tz, four = ZoneInfo(TZ), time(4, 0)
    assert regulator_day(ms(3, 3, 59), tz, four) == (date(2026, 11, 2), ms(2, 4))
    assert regulator_day(ms(3, 4, 0), tz, four) == (date(2026, 11, 3), ms(3, 4))
    assert regulator_day(ms(3, 23, 30), tz, four) == (date(2026, 11, 3), ms(3, 4))


def test_day_across_the_spring_clock_change_is_23_hours():
    tz, four = ZoneInfo(TZ), time(4, 0)
    _, before = regulator_day(ms(27, 12, month=3, year=2027), tz, four)
    _, after = regulator_day(ms(28, 12, month=3, year=2027), tz, four)
    assert after - before == 23 * 60 * MIN


# --- what counts ---------------------------------------------------------------------------------


def test_only_completed_listens_since_the_day_started_count(tmp_path):
    db = Storage(tmp_path / "r.db")
    db.add_event(listen(ended_at=ms(3, 10)))
    db.add_event(listen(ended_at=ms(3, 11), outcome=Outcome.SKIPPED))
    db.add_event(listen(ended_at=ms(3, 11, 30), outcome=Outcome.INTERRUPTED))
    db.add_event(listen(ended_at=ms(3, 3, 50)))  # yesterday: before 04:00
    db.add_event(listen("B", ended_at=ms(3, 10)))
    assert db.count_listens("A", since_ms=ms(3, 4)) == 1


def test_recently_played_only_listens_count_once(tmp_path):
    db = Storage(tmp_path / "r.db")
    db.add_event(listen(ended_at=ms(3, 10)))
    db.add_recent_plays([
        recent(played_at=ms(3, 9)),  # the detector never saw it: counts
        recent(played_at=ms(3, 10, 0, 5)),  # the detector's play, not paired yet: already counted
    ])
    assert db.count_listens("A", since_ms=ms(3, 4)) == 2
    db.link_recent_plays(since_ms=ms(3, 4))
    assert db.count_listens("A", since_ms=ms(3, 4)) == 2


def test_detector_outcome_wins_when_both_saw_the_play(tmp_path):
    db = Storage(tmp_path / "r.db")
    db.add_event(listen(ended_at=ms(3, 10), outcome=Outcome.SKIPPED))  # skipped at 40 s
    db.add_recent_plays([recent(played_at=ms(3, 10, 0, 2))])  # Spotify lists it (> 30 s)
    assert db.count_listens("A", since_ms=ms(3, 4)) == 0


# --- when it acts --------------------------------------------------------------------------------


def test_under_the_limit_nothing_happens(tmp_path):
    rule, db, player = make(tmp_path, listens=2)
    assert run(rule, playing(0), playing(6), playing(60)) == []
    assert rows(db) == [] and player.commands == []


def test_dry_run_records_would_skip_after_five_seconds_and_sends_no_commands(tmp_path):
    rule, db, player = make(tmp_path)
    assert run(rule, playing(2)) == []
    assert rows(db) == []  # still within the margin
    notices = run(rule, playing(5))
    assert rows(db) == [("A", "2026-11-03", 3, 3, 1, "would_skip", None)]
    assert player.commands == []
    assert len(notices) == 1 and "3/3 hoy" in notices[0].subject and "simulación" in notices[0].body


def test_acts_once_per_play(tmp_path):
    rule, db, _ = make(tmp_path)
    run(rule, playing(0), playing(6), playing(9), playing(60))
    assert len(rows(db)) == 1


def test_waits_while_paused(tmp_path):
    rule, db, _ = make(tmp_path)
    run(rule, playing(0), playing(7, playing=False))
    assert rows(db) == []
    run(rule, playing(8))
    assert [r[5] for r in rows(db)] == ["would_skip"]


def test_skipping_it_yourself_first_is_recorded_as_cancelled(tmp_path):
    rule, db, _ = make(tmp_path)
    notices = run(rule, playing(0), playing(3), playing(1, track="B", started=START + 4 * SEC))
    assert [r[5] for r in rows(db)] == ["cancelled"]
    assert notices == []


def test_music_stopping_first_is_also_cancelled(tmp_path):
    rule, db, _ = make(tmp_path)
    run(rule, playing(0), (None, None, START + 3 * SEC))
    assert [r[5] for r in rows(db)] == ["cancelled"]


def test_real_mode_turns_repeat_one_off_before_skipping(tmp_path):
    rule, db, player = make(tmp_path, dry_run=False)
    notices = run(rule, playing(0, repeat="track"), playing(6, repeat="track"))
    assert player.commands == ["repeat:off", "next"]
    assert [r[4:6] for r in rows(db)] == [(0, "skipped")]
    assert "se ha saltado" in notices[0].body


def test_real_mode_leaves_other_repeat_modes_alone(tmp_path):
    rule, _, player = make(tmp_path, dry_run=False)
    run(rule, playing(0, repeat="context"), playing(6, repeat="context"))
    assert player.commands == ["next"]


def test_failed_skip_is_recorded_and_notified(tmp_path):
    error = SpotifyError(404, "Player command failed: No active device found")
    rule, db, _ = make(tmp_path, dry_run=False, player=FakePlayer(error=error))
    notices = run(rule, playing(0), playing(6))
    assert [r[5:] for r in rows(db)] == [("failed", str(error))]
    assert "no se pudo saltar" in notices[0].subject


def test_only_the_first_action_per_song_and_day_is_notified(tmp_path):
    rule, db, _ = make(tmp_path)
    second = START + 5 * MIN
    notices = run(rule, playing(0), playing(6), playing(0, started=second), playing(6, started=second))
    assert len(rows(db)) == 2 and len(notices) == 1
    tomorrow = ms(4, 12)
    for i in range(3):
        db.add_event(listen(ended_at=ms(4, 10, i)))
    assert len(run(rule, playing(0, started=tomorrow), playing(6, started=tomorrow))) == 1


def test_a_play_carried_across_a_restart_is_not_handled_twice(tmp_path):
    rule, db, _ = make(tmp_path)
    run(rule, playing(0), playing(6))
    again = DailyLimitRule(DailyLimitConfig(), TZ, db, FakePlayer())
    assert run(again, playing(40)) == []
    assert len(rows(db)) == 1


def test_overrides_change_the_limit_per_song(tmp_path):
    rule, db, _ = make(tmp_path, overrides={"spotify:track:" + "A" * 22: 1})
    db.add_event(listen("A" * 22, ended_at=ms(3, 9)))
    run(rule, playing(0, track="A" * 22), playing(6, track="A" * 22))
    run(rule, playing(0, started=START + MIN), playing(6, started=START + MIN))  # A keeps the default 3
    assert [(r[0], r[3]) for r in rows(db)] == [("A" * 22, 1), ("A", 3)]


@pytest.mark.parametrize(
    "key",
    [
        "4uLU6hMCjMI75M1A2tKUQC",
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/intl-es/track/4uLU6hMCjMI75M1A2tKUQC?si=abc123",
    ],
)
def test_overrides_accept_id_uri_or_link(key):
    assert DailyLimitConfig(overrides={key: 5}).limit_for("4uLU6hMCjMI75M1A2tKUQC") == 5


def test_overrides_reject_what_is_not_a_track():
    with pytest.raises(ValueError):
        DailyLimitConfig(overrides={"https://open.spotify.com/album/4uLU6hMCjMI75M1A2tKUQC": 5})
