import asyncio
import logging
from datetime import UTC, datetime

from regulator.reconstructor import ListenEvent, Outcome
from regulator.recent import CURSOR_KEY, recent_play_from_item, sync_recently_played
from regulator.storage import Storage


def item(track_id: str, played_at: str) -> dict:
    return {
        "played_at": played_at,
        "context": {"uri": "spotify:playlist:p"},
        "track": {
            "id": track_id,
            "name": f"Song {track_id}",
            "duration_ms": 200_000,
            "album": {"id": "al1", "name": "Album"},
            "artists": [{"id": "ar1", "name": "Artist"}],
        },
    }


class FakeSpotify:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def recently_played(self, *, after_ms=None, limit=50):
        self.calls.append(after_ms)
        return self.responses.pop(0)


def ms(iso: str) -> int:
    return recent_play_from_item(item("x", iso)).played_at_ms


def test_parse_item_with_millis_and_z():
    p = recent_play_from_item(item("t1", "2026-10-19T11:31:41.968Z"))
    assert p.track_id == "t1"
    assert p.played_at_ms == int(datetime(2026, 10, 19, 11, 31, 41, 968_000, tzinfo=UTC).timestamp() * 1000)
    assert p.artist_names == ("Artist",)
    assert p.context_uri == "spotify:playlist:p"


def test_parse_skips_broken_items():
    assert recent_play_from_item({"played_at": "2026-10-19T11:31:41Z", "track": None}) is None
    assert recent_play_from_item({"track": {"id": "t"}}) is None


def run_sync(db, fake, now_ms=1_760_900_000_000):
    return asyncio.run(sync_recently_played(fake, db, now_ms=now_ms))


def test_first_sync_without_cursor_then_uses_cursor(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    first = {"items": [item("B", "2026-10-19T11:35:00.000Z"), item("A", "2026-10-19T11:31:41.968Z")]}
    second = {"items": [item("B", "2026-10-19T11:35:00.000Z"), item("C", "2026-10-19T11:40:00.000Z")]}
    fake = FakeSpotify(first, second)

    result = run_sync(db, fake)
    assert (result.fetched, result.added) == (2, 2)
    assert db.get_state(CURSOR_KEY) == str(ms("2026-10-19T11:35:00.000Z"))

    result = run_sync(db, fake)
    assert (result.fetched, result.added) == (2, 1)  # B was already stored
    assert fake.calls == [None, ms("2026-10-19T11:35:00.000Z")]
    assert db.get_state(CURSOR_KEY) == str(ms("2026-10-19T11:40:00.000Z"))


def test_empty_or_missing_response_keeps_cursor(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    db.set_state(CURSOR_KEY, "123")
    run_sync(db, FakeSpotify({"items": []}, None))
    run_sync(db, FakeSpotify(None))
    assert db.get_state(CURSOR_KEY) == "123"


def test_sync_pairs_with_detector_events(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    played = ms("2026-10-19T11:31:41.968Z")
    event_id = db.add_event(
        ListenEvent(
            track_id="A",
            track_name="Song A",
            artist_ids=("ar1",),
            artist_names=("Artist",),
            album_id="al1",
            album_name="Album",
            duration_ms=200_000,
            started_at_ms=played - 200_000,
            ended_at_ms=played - 4_000,  # last sample a few seconds before Spotify's played_at
            position_ms=196_000,
            outcome=Outcome.COMPLETED,
            is_repeat=False,
            device="Spotifast",
            context_uri=None,
        )
    )
    result = run_sync(db, FakeSpotify({"items": [item("A", "2026-10-19T11:31:41.968Z")]}), now_ms=played)
    assert result.linked == 1
    assert db.execute("SELECT event_id FROM recent_plays") == [(event_id,)]


def test_full_page_after_cursor_warns_about_possible_gap(tmp_path, caplog):
    db = Storage(tmp_path / "regulator.db")
    db.set_state(CURSOR_KEY, "0")
    page = {"items": [item(f"T{i}", f"2026-10-19T10:{i:02d}:00.000Z") for i in range(50)]}
    with caplog.at_level(logging.WARNING):
        run_sync(db, FakeSpotify(page))
    assert any(r.getMessage() == "recently_played_possible_gap" for r in caplog.records)
