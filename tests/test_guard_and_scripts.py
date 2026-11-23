import subprocess
from pathlib import Path

import catcam_guard

ROOT = Path(__file__).resolve().parents[1]


def test_only_a_nearly_white_frame_counts_as_stuck():
    assert catcam_guard.is_washed_out({"mean": 242, "clipped": 0.9})
    assert not catcam_guard.is_washed_out({"mean": 226, "clipped": 0.61})   # a sunny afternoon
    assert not catcam_guard.is_washed_out(None)


def test_shell_scripts_parse():
    for script in ("deploy/install.sh", "deploy/catcam-update"):
        subprocess.run(["bash", "-n", str(ROOT / script)], check=True)
    subprocess.run(["sh", "-n", str(ROOT / "deploy/catcam-insights")], check=True)


def test_pages_have_no_broken_nav():
    for page in (ROOT / "app/pages").glob("*.html"):
        html = page.read_text()
        assert 'href="/camera/"' in html and 'href="/insights/"' in html, page.name


def test_stuck_blue_is_spotted_but_a_normal_room_is_not():
    assert catcam_guard.is_blue({"mean": 80, "clipped": 0.0, "cast": 0.24})      # this morning's frame
    assert not catcam_guard.is_blue({"mean": 125, "clipped": 0.0, "cast": -0.01})  # daylight, normal
    assert not catcam_guard.is_blue({"mean": 30, "clipped": 0.0, "cast": 0.2})     # dark night, blue LEDs


def test_reset_button_request_is_taken_once(tmp_path, monkeypatch):
    flag = tmp_path / "reset-request"
    monkeypatch.setattr(catcam_guard, "RESET_REQUEST", str(flag))
    flag.write_text("x")
    assert catcam_guard.reset_requested() and not flag.exists()
    assert not catcam_guard.reset_requested()


def test_guard_resets_the_camera_in_use(tmp_path, monkeypatch):
    import catcam_guard as guard
    for name, product in (("1-3", "ZV-1F"), ("1-4", "Brave 7 LE"), ("1-13", "HD Webcam")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "product").write_text(product + "\n")
    monkeypatch.setattr(guard.glob, "glob", lambda pattern: [str(p) for p in tmp_path.iterdir()])
    assert guard.find_camera() == str(tmp_path / "1-3") and not guard.camera_is_akaso()
    (tmp_path / "1-3" / "product").unlink()
    assert guard.find_camera() == str(tmp_path / "1-4") and guard.camera_is_akaso()
