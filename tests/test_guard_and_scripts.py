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
