import json

from regulator.reconstructor import ListenEvent, Outcome
from regulator.storage import RecentPlay, Storage

SEC = 1_000


def event(track="A", ended_s=100, outcome=Outcome.COMPLETED, **overrides) -> ListenEvent:
    fields = dict(
        track_id=track,
        track_name=f"Song {track}",
        artist_ids=("ar1", "ar2"),
        artist_names=("Ling tosite sigure", "Ñandú"),
        album_id="al1",
        album_name="Album",
        duration_ms=200 * SEC,
        started_at_ms=(ended_s - 150) * SEC,
        ended_at_ms=ended_s * SEC,
        position_ms=150 * SEC,
        outcome=outcome,
        is_repeat=False,
        device="Spotifast",
        context_uri="spotify:album:al1",
    )
    fields.update(overrides)
    return ListenEvent(**fields)


def play(track="A", played_s=105) -> RecentPlay:
    return RecentPlay(
        track_id=track,
        played_at_ms=played_s * SEC,
        track_name=f"Song {track}",
        artist_ids=("ar1",),
        artist_names=("Ling tosite sigure",),
        album_id="al1",
        album_name="Album",
        duration_ms=200 * SEC,
        context_uri=None,
    )


def links(db):
    return dict(db.execute("SELECT played_at_ms, event_id FROM recent_plays ORDER BY played_at_ms"))


def test_event_round_trip(tmp_path):
    db = Storage(tmp_path / "data" / "regulator.db")
    event_id = db.add_event(event(outcome=Outcome.SKIPPED, is_repeat=True))
    row = db.execute("SELECT track_id, artist_names, outcome, is_repeat FROM events WHERE id = ?", (event_id,))[0]
    assert row[0] == "A"
    assert json.loads(row[1]) == ["Ling tosite sigure", "Ñandú"]
    assert row[2:] == ("skipped", 1)


def test_recent_plays_are_deduplicated(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    assert db.add_recent_plays([play("A", 105), play("B", 300)]) == 2
    assert db.add_recent_plays([play("A", 105), play("B", 300), play("C", 500)]) == 1
    assert db.execute("SELECT COUNT(*) FROM recent_plays")[0][0] == 3


def test_links_same_track_within_margin(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    a = db.add_event(event("A", ended_s=100))
    db.add_recent_plays([play("A", 106), play("B", 107), play("A", 400)])
    assert db.link_recent_plays(since_ms=0) == 1
    assert links(db) == {106 * SEC: a, 107 * SEC: None, 400 * SEC: None}


def test_entry_outside_margin_is_not_linked(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    db.add_event(event("A", ended_s=100))
    db.add_recent_plays([play("A", 125)])
    assert db.link_recent_plays(since_ms=0) == 0


def test_repeats_pair_one_to_one_with_the_closest(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    first = db.add_event(event("A", ended_s=100))
    second = db.add_event(event("A", ended_s=135, is_repeat=True))
    db.add_recent_plays([play("A", 101), play("A", 137)])
    assert db.link_recent_plays(since_ms=0) == 2
    assert links(db) == {101 * SEC: first, 137 * SEC: second}


def test_relinking_is_idempotent_and_late_events_get_paired(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    db.add_recent_plays([play("A", 105)])
    assert db.link_recent_plays(since_ms=0) == 0  # detector event not stored yet
    a = db.add_event(event("A", ended_s=100))
    assert db.link_recent_plays(since_ms=0) == 1
    assert db.link_recent_plays(since_ms=0) == 0
    assert links(db) == {105 * SEC: a}


def test_old_unpaired_entries_outside_window_are_left_alone(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    db.add_event(event("A", ended_s=100))
    db.add_recent_plays([play("A", 105)])
    assert db.link_recent_plays(since_ms=200 * SEC) == 0


def test_origin_is_queryable(tmp_path):
    # A seen by both, B only by Spotify (detector was down), D only by the detector (<30 s skip).
    db = Storage(tmp_path / "regulator.db")
    db.add_event(event("A", ended_s=100))
    db.add_event(event("D", ended_s=700, outcome=Outcome.SKIPPED, position_ms=20 * SEC))
    db.add_recent_plays([play("A", 103), play("C", 400)])
    db.link_recent_plays(since_ms=0)
    only_spotify = db.execute("SELECT track_id FROM recent_plays WHERE event_id IS NULL")
    only_detector = db.execute(
        "SELECT track_id FROM events e WHERE NOT EXISTS (SELECT 1 FROM recent_plays r WHERE r.event_id = e.id)"
    )
    assert only_spotify == [("C",)]
    assert only_detector == [("D",)]


def test_state_set_and_overwrite(tmp_path):
    db = Storage(tmp_path / "regulator.db")
    assert db.get_state("cursor") is None
    db.set_state("cursor", "1")
    db.set_state("cursor", "2")
    assert db.get_state("cursor") == "2"


def test_data_survives_reopening_and_uses_wal(tmp_path):
    path = tmp_path / "regulator.db"
    db = Storage(path)
    db.add_event(event())
    db.set_state("cursor", "42")
    assert db.execute("PRAGMA journal_mode")[0][0] == "wal"
    db.close()
    db = Storage(path)
    assert db.execute("SELECT COUNT(*) FROM events")[0][0] == 1
    assert db.get_state("cursor") == "42"


def test_delete_state(tmp_path):
    db = Storage(tmp_path / "r.db")
    db.set_state("k", "v")
    db.delete_state("k")
    db.delete_state("missing")
    assert db.get_state("k") is None
