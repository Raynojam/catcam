"""Every test runs against a throwaway home folder, so nothing touches a real mina."""
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="catcam-home-"))
os.environ["HOME"] = str(HOME)
if os.name == "nt":
    os.environ["USERPROFILE"] = str(HOME)  # Path.home() reads this on Windows.
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "guard")]
(HOME / "Videos" / "CatCam" / "clips").mkdir(parents=True)
(HOME / ".config" / "catcam").mkdir(parents=True)
