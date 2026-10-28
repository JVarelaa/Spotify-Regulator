import asyncio
import json
import os
import time

import pytest

from regulator.auth import ReauthRequired
from regulator.cli import main
from regulator.config import PollingConfig
from regulator.service import PLAY_STATE_KEY, Service, heartbeat_age
from regulator.spotify import QuotaExceeded, SpotifyUnavailable
from regulator.storage import Storage

MIN = 60_000
SEC = 1_000
SONG = 3 * MIN + 30 * SEC
POLLING = PollingConfig(playing_interval_s=3, idle_interval_s=30, recently_played_interval_min=60)


def playback(track="A", progress_ms=0, *, playing=True):
    return {
        "currently_playing_type": "track",
        "is_playing": playing,
        "progress_ms": progress_ms,
        "device": {"name": "Laptop"},
        "context": {"uri": "spotify:playlist:x"},
        "item": {
            "type": "track",
            "id": track,
            "name": f"Song {track}",
            "duration_ms": SONG,
            "album": {"id": "al", "name": "Album"},
            "artists": [{"id": "ar", "name": "Artist"}],
        },
    }


class FakeSpotify:
    """Answers /me/player from a script (a payload or an exception per call); the last answer repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [None]
        self.player_calls = 0
        self.recent_calls = 0

    async def playback_state(self):
        self.player_calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def recently_played(self, *, after_ms=None, limit=50):
        self.recent_calls += 1
        return {"items": []}


class FakeTokens:
    def __init__(self, *, dead=False):
        self.token = None
        self.dead_token = object() if dead else None
        self.new_login = False

    def check_for_new_login(self):
        return self.new_login


class FakeNotices:
    def __init__(self, *, delay_s=0.0, error=None):
        self.calls = 0
        self.delay_s = delay_s
        self.error = error

    async def check(self, token, dead_token):
        self.calls += 1
        if self.error:
            raise self.error
        await asyncio.sleep(self.delay_s)  # real time: an email that takes long to give up


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


async def fast_sleep(seconds):
    """Service time runs 1000x faster than real time: a 3 s interval takes 3 ms."""
    await asyncio.sleep(seconds / 1000)


def make(tmp_path, client, *, tokens=None, notices=None, clock=time.time, db=None):
    db = db or Storage(tmp_path / "r.db")
    service = Service(
        client, tokens or FakeTokens(), db, notices or FakeNotices(), POLLING, tmp_path / "heartbeat",
        clock=clock, sleep=fast_sleep,
    )
    return service, db


async def polls(service, clock, *times_s):
    for t in times_s:
        clock.t = t
        await service.poll_once()


def events(db):
    return db.execute("SELECT track_id, outcome, started_at_ms FROM events ORDER BY id")


async def run_for(service, real_s):
    """Run the whole service for `real_s` real seconds, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await asyncio.sleep(real_s)
    stop.set()
    await asyncio.wait_for(task, 1)


# --- player ----------------------------------------------------------------------------------


def test_interval_is_short_while_playing_and_long_otherwise(tmp_path):
    service, _ = make(tmp_path, FakeSpotify(playback(), playback(playing=False), None))

    async def scenario():
        return [await service.poll_once() for _ in range(3)]

    assert asyncio.run(scenario()) == [3, 30, 30]  # playing, paused, nothing playing


def test_events_are_stored_and_the_play_in_progress_is_saved(tmp_path):
    clock = Clock()
    service, db = make(tmp_path, FakeSpotify(playback("A", 0), playback("A", 3 * SEC), playback("B", 0), None), clock=clock)

    asyncio.run(polls(service, clock, 0, 3, 6))
    assert events(db) == [("A", "skipped", 0)]
    assert json.loads(db.get_state(PLAY_STATE_KEY))["last"]["track_id"] == "B"

    asyncio.run(polls(service, clock, 36))
    assert [e[:2] for e in events(db)] == [("A", "skipped"), ("B", "interrupted")]
    assert db.get_state(PLAY_STATE_KEY) is None


def test_restart_in_the_middle_of_a_song_counts_one_listen(tmp_path):
    clock = Clock()
    db = Storage(tmp_path / "r.db")
    first, _ = make(tmp_path, FakeSpotify(playback("A", 0), playback("A", 150 * SEC)), clock=clock, db=db)
    asyncio.run(polls(first, clock, 0, 150))
    # The service stops at 2:30 without storing anything, and a new run starts 10 s later.
    assert events(db) == []
    second, _ = make(tmp_path, FakeSpotify(playback("A", 160 * SEC), playback("B", 0)), clock=clock, db=db)
    asyncio.run(polls(second, clock, 160, 215))
    assert events(db) == [("A", "completed", 0)]


def test_unreadable_saved_play_is_discarded(tmp_path):
    db = Storage(tmp_path / "r.db")
    db.set_state(PLAY_STATE_KEY, "{not json")
    make(tmp_path, FakeSpotify(), db=db)
    assert db.get_state(PLAY_STATE_KEY) is None


def test_failed_poll_is_a_gap_not_the_end_of_the_play(tmp_path):
    clock = Clock()
    client = FakeSpotify(playback("A", 0), SpotifyUnavailable("red caída"), playback("A", 40 * SEC), playback("B", 0))
    service, db = make(tmp_path, client, clock=clock)

    async def scenario():
        clock.t = 0
        assert await service.poll_once() == 3
        clock.t = 3
        assert await service.poll_once() == 30  # no point asking every 3 s while the network is down
        await polls(service, clock, 40, 43)

    asyncio.run(scenario())
    assert events(db) == [("A", "skipped", 0)]


def test_exhausted_quota_waits_what_spotify_says(tmp_path):
    service, _ = make(tmp_path, FakeSpotify(QuotaExceeded("quota", retry_after=3600)))
    assert asyncio.run(service.poll_once()) == 3600


def test_heartbeat_is_written_every_round(tmp_path):
    service, _ = make(tmp_path, FakeSpotify(playback()))
    path = tmp_path / "heartbeat"
    assert heartbeat_age(path) is None

    asyncio.run(run_for(service, 0.05))
    mtime = path.stat().st_mtime
    assert heartbeat_age(path, now=mtime + 5) == pytest.approx(5)


# --- tasks working together --------------------------------------------------------------------


def test_slow_email_does_not_hold_up_the_player(tmp_path):
    client = FakeSpotify(playback())
    notices = FakeNotices(delay_s=0.5)
    service, _ = make(tmp_path, client, notices=notices)

    asyncio.run(run_for(service, 0.2))
    assert notices.calls == 1  # still waiting for the mail server
    assert client.player_calls > 20  # 3 s rounds (3 ms here) kept going meanwhile


def test_sync_runs_at_start_then_waits_its_interval(tmp_path):
    client = FakeSpotify(playback())
    service, _ = make(tmp_path, client)
    asyncio.run(run_for(service, 0.2))  # 200 s of service time, well under an hour
    assert client.recent_calls == 1


def test_lost_access_stops_requests_until_a_new_login(tmp_path):
    client = FakeSpotify(ReauthRequired("token rechazado"), playback())
    tokens = FakeTokens(dead=True)
    service, _ = make(tmp_path, client, tokens=tokens)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop))
        await asyncio.sleep(0.1)  # 100 s of waiting: token file checked every 2 s, no requests
        assert (client.player_calls, client.recent_calls) == (1, 0)

        tokens.new_login, tokens.dead_token = True, None  # `regulator login` wrote a new token
        await asyncio.sleep(0.1)
        assert client.player_calls > 1
        assert client.recent_calls == 1  # the sync waited for access and then ran
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_a_task_dying_stops_the_whole_service(tmp_path):
    service, _ = make(tmp_path, FakeSpotify(playback()), notices=FakeNotices(error=RuntimeError("bug")))

    async def scenario():
        await asyncio.wait_for(service.run(asyncio.Event()), 1)

    with pytest.raises(ExceptionGroup) as info:
        asyncio.run(scenario())
    assert isinstance(info.value.exceptions[0], RuntimeError)


def test_stop_ends_cleanly_and_keeps_the_play_for_the_next_run(tmp_path):
    service, db = make(tmp_path, FakeSpotify(playback("A", 30 * SEC)))
    asyncio.run(run_for(service, 0.05))
    assert events(db) == []
    assert json.loads(db.get_state(PLAY_STATE_KEY))["last"]["track_id"] == "A"


# --- healthcheck command -----------------------------------------------------------------------


def test_healthcheck_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("REGULATOR_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("REGULATOR_DATA_DIR", str(tmp_path))
    assert main(["healthcheck"]) == 1  # never beat

    beat = tmp_path / "heartbeat"
    beat.touch()
    assert main(["healthcheck"]) == 0

    old = time.time() - 300
    os.utime(beat, (old, old))
    assert main(["healthcheck"]) == 1
