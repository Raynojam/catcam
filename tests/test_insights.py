from datetime import datetime

import insights


def rec(hm, who, act, spot="cat bed", day="2026-11-22", **extra):
    return {"timestamp": f"{day}T{hm}:00", "is_cat": True, "cats": [{"name": w, "appearance": "x"} for w in who],
            "activity": act, "spot": spot, "caption": f"{who[0]} {act}", "seconds": 40, "quality": 3, **extra}


def data_with(*records):
    return {"version": 1, "sightings": {f"2026112{i}-000000-{i}.mp4": r for i, r in enumerate(records)},
            "diaries": {}, "sent": {}}


def test_story_brief_merges_repeats_and_finds_highlights():
    data = data_with(rec("15:07", ["Siya"], "sleeping"), rec("15:11", ["Siya"], "sleeping"),
                     rec("15:17", ["Siya"], "sleeping"), rec("16:00", ["Faff", "Thuli"], "playing", "windowsill"))
    cats = [{"name": n, "description": "", "personality": ""} for n in ("Faff", "Siya", "Thuli")]
    brief = insights.story_brief(data, "2026-11-22", cats, set())
    assert len(brief["timeline"]) == 2 and "3:07 PM to 3:17 PM" in brief["timeline"][0]
    assert any(h.startswith("Together: Faff & Thuli") for h in brief["highlights"])
    assert any("playing" in h for h in brief["highlights"])
    assert brief["missing"] == [] and brief["compare"] == ""


def test_quiet_hours_wrap_midnight():
    config = {"quiet_hours": "23-07"}
    assert insights.in_quiet_hours(config, datetime(2026, 11, 22, 2))
    assert not insights.in_quiet_hours(config, datetime(2026, 11, 22, 12))


def test_leaderboard_ranks_by_time_on_camera():
    data = data_with(rec("10:00", ["Siya"], "sleeping", day="2026-11-22", seconds=120),
                     rec("11:00", ["Faff"], "playing", day="2026-11-22", seconds=30))
    stats = insights.build_stats(data, datetime(2026, 11, 22, 20))
    assert [r["name"] for r in stats["leaderboard"]] == ["Siya", "Faff"]
