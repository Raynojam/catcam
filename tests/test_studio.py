from datetime import datetime

import studio


def test_movement_beats_a_pretty_nap():
    nap = {"quality": 5, "score": 0.9, "activity": "sleeping", "cats": [{"name": "Siya"}], "motion": 0.0}
    zoom = {"quality": 3, "score": 0.6, "activity": "playing", "cats": [{"name": "Faff"}], "motion": 0.06}
    assert studio.interest(zoom) > studio.interest(nap)


def test_people_are_never_candidates():
    for name in ("20261122-100000-1.mp4", "20261122-110000-2.mp4"):
        (studio.CLIPS / name).write_bytes(b"x")
    sightings = {
        "20261122-100000-1.mp4": {"is_cat": True, "timestamp": "2026-11-22T10:00:00", "cats": [], "person": 0.9},
        "20261122-110000-2.mp4": {"is_cat": True, "timestamp": "2026-11-22T11:00:00", "cats": [], "person": 0.1},
    }
    found = studio.candidates(sightings, datetime(2026, 11, 22), datetime(2026, 11, 23))
    assert [c[2] for c in found] == ["20261122-110000-2.mp4"]


def test_pick_spreads_moments_out():
    t = lambda h, m: datetime(2026, 11, 22, h, m)
    cands = [(9, t(10, 0), "a", {"cats": []}), (8, t(10, 5), "b", {"cats": []}), (7, t(11, 0), "c", {"cats": []})]
    assert [c[2] for c in studio.pick(cands, 3)] == ["a", "c"]


def test_text_is_cleaned_and_wrapped_for_video():
    assert studio.clean("Hi 🐾  there") == "Hi there"
    assert studio.wrap("Siya has entered nap mode for the afternoon", 18).count("\n") == 2
