"""Checks that need ffmpeg (skipped where it isn't installed)."""
import shutil
import subprocess

import pytest

import identity
import insights

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
CLIP = "20261122-120000-1.mp4"


@pytest.fixture(scope="module")
def clip():
    path = insights.CLIPS / CLIP
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=15:d=6",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_identity_crop_gives_model_input_and_a_thumbnail(clip):
    rgb = identity.Embedder.crop(identity.Embedder.__new__(identity.Embedder), CLIP, 2.0, [0.5, 0.5, 0.2, 0.3])
    assert len(rgb) == identity.SIDE * identity.SIDE * 3
    assert (identity.CROPS / (CLIP + ".jpg")).stat().st_size > 1000


def test_motion_energy_sees_movement(clip):
    assert insights.motion_energy(CLIP, 2.0) > 0.001


def test_grab_frame_returns_a_jpeg(clip):
    assert insights.grab_frame(CLIP, 2.0).startswith(b"\xff\xd8")
