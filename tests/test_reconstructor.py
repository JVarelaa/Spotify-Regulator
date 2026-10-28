import json

import pytest

from regulator.reconstructor import Outcome, Reconstructor, Sample, sample_from_playback

MIN = 60_000
SEC = 1_000


def s(track: str, progress_ms: int, at_s: float, *, duration_ms: int = 4 * MIN, playing: bool = True) -> Sample:
    return Sample(
        observed_at_ms=int(at_s * SEC),
        track_id=track,
        track_name=f"Song {track}",
        artist_ids=(f"artist-{track}",),
        artist_names=(f"Artist {track}",),
        album_id=f"album-{track}",
        album_name=f"Album {track}",
        duration_ms=duration_ms,
        progress_ms=progress_ms,
        is_playing=playing,
        device="Laptop",
        context_uri="spotify:playlist:x",
    )


def feed(samples):
    """Feed samples in order; return every closed event."""
    r = Reconstructor()
    events = []
    for sample in samples:
        events += r.observe(sample)
    return r, events


def outcomes(events):
    return [(e.track_id, e.outcome, e.is_repeat) for e in events]


# --- track changes ---------------------------------------------------------------------------


def test_played_to_the_end_then_next_track_is_completed():
    _, events = feed([s("A", 0, 0), s("A", 2 * MIN, 120), s("A", 3 * MIN + 57 * SEC, 237), s("B", 3 * SEC, 243)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]
    assert events[0].position_ms == 3 * MIN + 57 * SEC


def test_track_change_before_threshold_is_skip():
    _, events = feed([s("A", 0, 0), s("A", 40 * SEC, 40), s("B", 2 * SEC, 45)])
    assert outcomes(events) == [("A", Outcome.SKIPPED, False)]


def test_threshold_is_four_minutes_on_long_tracks():
    ten = 10 * MIN
    _, events = feed([s("A", 4 * MIN + 5 * SEC, 0, duration_ms=ten), s("B", 0, 5)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]
    _, events = feed([s("A", 3 * MIN + 55 * SEC, 0, duration_ms=ten), s("B", 0, 5)])
    assert outcomes(events) == [("A", Outcome.SKIPPED, False)]


def test_threshold_is_half_on_short_tracks():
    two = 2 * MIN
    _, events = feed([s("A", 61 * SEC, 0, duration_ms=two), s("B", 0, 5)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]


def test_seek_forward_counts_as_completed_by_position():
    # Decided rule: position, not listened time. 0:30 -> drag to 3:00 -> change at 3:05.
    _, events = feed([s("A", 30 * SEC, 0), s("A", 3 * MIN, 5), s("A", 3 * MIN + 5 * SEC, 10), s("B", 0, 15)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]


# --- repeats ---------------------------------------------------------------------------------


def test_repeat_one_closes_completed_and_flags_the_new_play():
    _, events = feed([s("A", 3 * MIN + 57 * SEC, 0), s("A", 2 * SEC, 5)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]
    # The repeat is classified on its own when it ends: here it plays past the threshold.
    _, events = feed([s("A", 3 * MIN + 57 * SEC, 0), s("A", 2 * SEC, 5), s("A", 3 * MIN, 183), s("B", 0, 190)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False), ("A", Outcome.COMPLETED, True)]


def test_restart_before_threshold_is_skip_then_repeat():
    _, events = feed([s("A", 40 * SEC, 0), s("A", 2 * SEC, 5), s("B", 0, 30)])
    assert outcomes(events) == [("A", Outcome.SKIPPED, False), ("A", Outcome.SKIPPED, True)]


def test_rewinding_the_bar_is_a_repeat_classified_on_its_own():
    # 3:00 -> drag back to 1:00 -> change at 1:30: first part completed, the fragment is a skip.
    _, events = feed([s("A", 3 * MIN, 0), s("A", 1 * MIN, 5), s("A", 1 * MIN + 30 * SEC, 35), s("B", 0, 40)])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False), ("A", Outcome.SKIPPED, True)]


def test_small_backwards_wobble_is_not_a_repeat():
    _, events = feed([s("A", 50 * SEC, 0), s("A", 48 * SEC, 5), s("A", 55 * SEC, 10)])
    assert events == []


def test_same_track_later_after_another_one_is_not_a_repeat():
    _, events = feed([s("A", 3 * MIN, 0), s("B", 3 * MIN, 180), s("A", 10 * SEC, 360), s("C", 0, 365)])
    assert [e.is_repeat for e in events] == [False, False, False]


# --- pauses and stops ------------------------------------------------------------------------


def test_pause_produces_no_events_and_resume_continues_the_play():
    samples = [s("A", 30 * SEC, 0)] + [s("A", 31 * SEC, t, playing=False) for t in range(5, 600, 5)]
    samples += [s("A", 3 * MIN, 750), s("B", 0, 755)]
    _, events = feed(samples)
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]


def test_stop_below_threshold_is_interrupted_not_skip():
    _, events = feed([s("A", 30 * SEC, 0), None])
    assert outcomes(events) == [("A", Outcome.INTERRUPTED, False)]


def test_stop_above_threshold_is_completed():
    _, events = feed([s("A", 3 * MIN, 0), None])
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]


def test_nothing_playing_repeatedly_emits_nothing():
    _, events = feed([None, None, s("A", 0, 10), None, None])
    assert len(events) == 1


# --- carrying a play across a restart -------------------------------------------------------


def restarted(samples_before):
    """Reconstructor of a new run that restored the play saved by `samples_before` (via JSON)."""
    r, _ = feed(samples_before)
    snapshot = json.loads(json.dumps(r.snapshot()))
    new = Reconstructor()
    new.restore(snapshot)
    return new


def test_snapshot_is_none_without_a_play():
    assert Reconstructor().snapshot() is None
    r, _ = feed([s("A", 0, 0), None])
    assert r.snapshot() is None


def test_same_play_carrying_on_is_continued_as_one_event():
    # Restart at 2:30 of a 3:30 song, back 10 s later: one play, with its real start.
    r = restarted([s("A", 0, 0), s("A", 2 * MIN + 30 * SEC, 150)])
    assert r.observe(s("A", 2 * MIN + 40 * SEC, 160, duration_ms=3 * MIN + 30 * SEC)) == []
    events = r.observe(s("B", 0, 215))
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]
    assert events[0].started_at_ms == 0
    assert events[0].ended_at_ms == 160 * SEC


def test_restored_play_keeps_its_repeat_flag():
    r = restarted([s("A", 3 * MIN + 57 * SEC, 0), s("A", 2 * SEC, 5), s("A", 1 * MIN, 63)])
    assert r.observe(s("A", 2 * MIN + 10 * SEC, 133)) == []
    assert outcomes(r.observe(None)) == [("A", Outcome.COMPLETED, True)]


def test_paused_play_still_paused_is_continued():
    r = restarted([s("A", 0, 0), s("A", 1 * MIN, 60, playing=False)])
    assert r.observe(s("A", 1 * MIN, 600, playing=False)) == []
    assert r.observe(s("B", 0, 610))[0].started_at_ms == 0


def test_different_track_after_restart_drops_the_restored_play():
    # It ended while the service was down: no event with guessed data (recently-played has it).
    r = restarted([s("A", 0, 0), s("A", 2 * MIN, 120)])
    assert r.observe(s("B", 30 * SEC, 300)) == []
    assert outcomes(r.observe(s("C", 0, 400))) == [("B", Outcome.SKIPPED, False)]


def test_same_track_that_restarted_meanwhile_is_a_new_play():
    # Saved at 2:30, down 4 min: with repeat-one it started again and is at 3:00, not at 6:30.
    r = restarted([s("A", 0, 0), s("A", 2 * MIN + 30 * SEC, 150)])
    assert r.observe(s("A", 3 * MIN, 390)) == []
    events = r.observe(s("B", 0, 400))
    assert outcomes(events) == [("A", Outcome.COMPLETED, False)]
    assert events[0].started_at_ms == (390 - 180) * SEC


def test_nothing_playing_after_restart_drops_the_restored_play():
    r = restarted([s("A", 0, 0), s("A", 2 * MIN, 120)])
    assert r.observe(None) == []
    assert r.snapshot() is None


def test_restore_rejects_malformed_snapshots():
    with pytest.raises(KeyError):
        Reconstructor().restore({"first": {}})
    r, _ = feed([s("A", 0, 0)])
    snapshot = r.snapshot()
    snapshot["last"]["unexpected"] = 1
    with pytest.raises(TypeError):
        Reconstructor().restore(snapshot)


# --- event fields ----------------------------------------------------------------------------


def test_event_times_and_metadata():
    _, events = feed([s("A", 10 * SEC, 100), s("A", 2 * MIN + 10 * SEC, 220), s("B", 0, 225)])
    e = events[0]
    assert e.started_at_ms == 90 * SEC  # first seen at 100 s with 10 s already played
    assert e.ended_at_ms == 220 * SEC
    assert e.artist_names == ("Artist A",)
    assert e.album_name == "Album A"
    assert e.device == "Laptop"
    assert e.context_uri == "spotify:playlist:x"


# --- parsing /me/player ----------------------------------------------------------------------


def track_payload(**overrides):
    payload = {
        "currently_playing_type": "track",
        "is_playing": True,
        "progress_ms": 12_345,
        "device": {"name": "Spotifast"},
        "context": {"uri": "spotify:album:1"},
        "item": {
            "type": "track",
            "id": "t1",
            "uri": "spotify:track:t1",
            "name": "a symmetry",
            "duration_ms": 200_000,
            "album": {"id": "al1", "name": "Album"},
            "artists": [{"id": "ar1", "name": "Ling tosite sigure"}, {"id": "ar2", "name": "Guest"}],
        },
    }
    payload.update(overrides)
    return payload


def test_parse_track_payload():
    sample = sample_from_playback(track_payload(), 1_000)
    assert sample.track_id == "t1"
    assert sample.artist_ids == ("ar1", "ar2")
    assert sample.progress_ms == 12_345
    assert sample.duration_ms == 200_000
    assert sample.device == "Spotifast"
    assert sample.context_uri == "spotify:album:1"


def test_parse_non_music_returns_none():
    assert sample_from_playback(None, 0) is None  # 204
    assert sample_from_playback(track_payload(currently_playing_type="episode"), 0) is None
    assert sample_from_playback(track_payload(currently_playing_type="ad", item=None), 0) is None
    assert sample_from_playback(track_payload(item=None), 0) is None


def test_parse_local_file_uses_uri_and_tolerates_missing_context():
    item = dict(track_payload()["item"], id=None, uri="spotify:local:x")
    sample = sample_from_playback(track_payload(item=item, context=None, device=None), 0)
    assert sample.track_id == "spotify:local:x"
    assert sample.context_uri is None
    assert sample.device is None
