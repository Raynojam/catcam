import json

import pytest

import catcam


def test_camera_settings_validate_and_save():
    catcam.camera_request({"action": "save", "zone": [0.1, 0.1, 0.5, 0.5], "low_light": True,
                           "strength": 0.4, "output": "1920x1080"})
    assert catcam.frame_config()["zone"] == [0.1, 0.1, 0.5, 0.5]
    assert catcam.recording_size() == (1920, 1080)
    for bad in ({"action": "save", "zone": [0.9, 0, 0.5, 0.5]}, {"action": "save", "zone": "x"},
                {"action": "save", "strength": 3}, {"action": "save", "output": "4k"}, {"action": "nope"}):
        with pytest.raises(ValueError):
            catcam.camera_request(bad)
    catcam.camera_request({"action": "save", "zone": None, "output": "1280x720"})
    assert catcam.recording_size() == (1280, 720)


def test_preview_request_is_recorded():
    catcam.camera_request({"action": "preview"})
    assert (catcam.FRAME_DIR / "preview-request").is_file()


def test_cat_names_are_checked():
    saved = catcam.set_cats([{"name": "Faff", "description": "grey tabby with dark stripes and a long tail, very round face",
                              "personality": "chaotic"}])
    assert saved["cats"][0]["personality"] == "chaotic"
    for bad in ([{"name": "A"}, {"name": "a"}], [{"name": "<b>"}], [{"name": "unknown"}], [{"name": "x" * 41}],
                [{"name": "ok", "description": "x" * 161}], [{"name": f"c{i}"} for i in range(9)]):
        with pytest.raises(ValueError):
            catcam.set_cats(bad)


def test_fixes_need_a_real_sighting_and_cat():
    catcam.set_cats([{"name": "Siya", "description": ""}])
    assert catcam.fix_cat({"sighting": "20261122-150700-3.mp4", "cat": "Siya"}) == {"queued": True}
    assert catcam.fix_cat({"sighting": "20261122-150700-3.mp4", "cat": "none"}) == {"queued": True}
    for bad in ({"sighting": "../x.mp4", "cat": "Siya"}, {"sighting": "20261122-150700-3.mp4", "cat": "Bob"}):
        with pytest.raises(ValueError):
            catcam.fix_cat(bad)


def test_studio_requests_are_checked():
    assert catcam.studio_request({"action": "settings", "handle": "@minas.window", "daily_time": "20:30",
                                  "auto_daily": True, "auto_weekly": False}) == {"saved": True}
    for bad in ({"action": "settings", "handle": "<x>", "daily_time": "20:30"},
                {"action": "settings", "handle": "", "daily_time": "25:00"},
                {"action": "delete", "id": "../../etc"}, {"action": "make", "name": "nope.mp4"}):
        with pytest.raises(ValueError):
            catcam.studio_request(bad)


def test_insights_overview_reads_the_diary_and_unsure_list():
    data = {"version": 1, "sightings": {"20261122-150700-3.mp4": {
        "timestamp": "2026-11-22T15:07:00", "is_cat": True, "cats": [{"name": "Siya", "appearance": "grey"}],
        "activity": "sleeping", "spot": "cat bed", "caption": "Siya naps.", "identity": {"method": "photo"}}},
        "diaries": {"2026-11-22": {"title": "Nap Day", "story": "Siya slept."}},
        "identity_unsure": ["20261122-150700-3.mp4", "gone.mp4"]}
    catcam.INSIGHTS_DATA.parent.mkdir(parents=True, exist_ok=True)
    catcam.INSIGHTS_DATA.write_text(json.dumps(data))
    view = catcam.insights_overview("2026-11-22")
    assert view["diary"]["title"] == "Nap Day"
    assert view["sightings"][0]["label"] == "Siya" and view["sightings"][0]["identity"] == "photo"
    assert [u["name"] for u in view["unsure"]] == ["20261122-150700-3.mp4"]
    with pytest.raises(ValueError):
        catcam.insights_overview("yesterday")


def test_missing_pages_explain_themselves():
    assert b"catcam-update" in catcam.page("does-not-exist.html")


def test_reset_button_leaves_a_request_for_the_guard():
    assert catcam.camera_request({"action": "reset"}) == {"reset": True}
    assert (catcam.FRAME_DIR / "reset-request").is_file()
