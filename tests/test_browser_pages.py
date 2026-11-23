"""Check the JavaScript the recorder actually serves, including Python-built pages."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import catcam


NODE = shutil.which("node")
SCRIPTS = re.compile(rb"<script>(.*?)</script>", re.DOTALL)


@unittest.skipUnless(NODE, "needs Node.js to check browser JavaScript")
class BrowserPagesTests(unittest.TestCase):
    def test_all_scripted_pages_parse_as_served(self):
        pages = {"home": catcam.PAGE, "sightings": catcam.SIGHTINGS_PAGE}
        page_dir = Path(catcam.__file__).parent / "pages"
        pages.update((name, (page_dir / name).read_bytes())
                     for name in ("camera.html", "studio.html", "insights.html"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "20261122-101112-1.mp4").touch()
            with patch.object(catcam, "RETENTION", root / "retention"), \
                    patch.object(catcam, "SAVED_SIGHTINGS", root / "saved.json"):
                pages["archive"] = catcam.archive_page(root, "")
        for name, page in pages.items():
            with self.subTest(page=name):
                scripts = SCRIPTS.findall(page)
                self.assertTrue(scripts, f"{name} did not include its script")
                for script in scripts:
                    result = subprocess.run([NODE, "--check", "-"], input=script,
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_disconnected_home_shows_a_notice_and_allows_refresh(self):
        # Run the real page's startup and Refresh handler with the camera hidden
        # and the journal offline. No browser packages, camera or network needed.
        setup = b"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  hidden: true,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {getContext() { return {}; }});
    return elements.get(id);
  },
  addEventListener() {}
};
const location = {search: ''};
const window = {addEventListener() {}};
const setInterval = () => 0;
const requests = [];
async function fetch(path) { requests.push(path); throw new Error('offline'); }
"""
        check = b"""
setImmediate(async () => {
  const refresh = elements.get('refresh'), notice = elements.get('notice');
  assert.equal(notice.hidden, false);
  assert.match(notice.textContent, /Refresh/);
  assert.equal(refresh.disabled, false);
  await refresh.onclick();
  assert.deepEqual(requests, ['/api/day', '/api/day']);
  assert.equal(refresh.disabled, false);
});
"""
        script, = SCRIPTS.findall(catcam.PAGE)
        result = subprocess.run([NODE, "--unhandled-rejections=strict", "-"],
                                input=setup + script + check, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
