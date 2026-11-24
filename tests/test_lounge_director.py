import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Browser logic checks require Node.js")
def test_lounge_view_controls_and_stream_lifecycle():
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("lounge_director.test.cjs"))],
        check=True, timeout=30,
    )
