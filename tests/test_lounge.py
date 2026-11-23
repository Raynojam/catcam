"""Family lounge live stream: a fake µStreamer in, the cropped multipart stream out."""
import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lounge"))
import lounge  # noqa: E402


def make_jpegs(folder, count=15):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=15",
                    "-frames:v", str(count), "-q:v", "5", str(folder / "f%03d.jpg")], check=True)
    return [p.read_bytes() for p in sorted(folder.glob("f*.jpg"))]


@pytest.fixture
def camera_source(tmp_path):
    frames = make_jpegs(tmp_path)
    online = {"value": True}

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/state"):
                body = json.dumps({"ok": True, "result": {"source": {"online": online["value"]}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=boundarydonotcross")
            self.end_headers()
            try:
                self.wfile.write(b"--boundarydonotcross\r\n")
                for i in range(100000):
                    frame = frames[i % len(frames)]
                    self.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame))
                    self.wfile.write(frame + b"\r\n--boundarydonotcross\r\n")
                    time.sleep(1 / 15)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", online
    server.shutdown()


@pytest.fixture
def lounge_server(tmp_path, camera_source):
    source, online = camera_source
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabled": True, "crop": [0.2, 0.2, 0.4, 0.6], "fps": 10}))
    server = lounge.make_server(settings, 0, source=source)
    threading.Thread(target=server.camera.run, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", settings, online
    server.camera.stop.set()
    server.shutdown()


def test_rate_limits():
    assert lounge.DEFAULT_CONFIG["fps"] == 10
    assert lounge.MAX_FPS == 12


def test_settings_accept_live_rates_and_refuse_silly_ones(tmp_path):
    path = tmp_path / "s.json"
    for fps, ok in ((10, True), (12, True), (0.5, True), (13, False), (0, False)):
        path.write_text(json.dumps({"enabled": True, "crop": [0, 0, 1, 1], "fps": fps}))
        if ok:
            assert lounge.read_settings(path)[0]["fps"] == fps
        else:
            with pytest.raises(ValueError):
                lounge.read_settings(path)


def test_read_parts_handles_ffmpeg_multipart():
    jpeg = b"\xff\xd8" + b"x" * 50 + b"\xff\xd9"
    body = b"".join(b"--lounge\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n%s\r\n" % (len(jpeg), jpeg)
                    for _ in range(3))
    assert list(lounge.read_parts(io.BytesIO(body))) == [jpeg] * 3
    with pytest.raises(ValueError):
        list(lounge.read_parts(io.BytesIO(b"--lounge\r\nContent-length: 999999999\r\n\r\n")))


def frame_times(url, seconds):
    times = []
    with urlopen(url + lounge.STREAM_PATH, timeout=15) as response:
        assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        for frame in lounge.read_parts(response):
            assert frame.startswith(b"\xff\xd8")
            times.append(time.monotonic())
            if times[-1] - times[0] > seconds:
                break
    return times


def test_stream_runs_near_the_configured_rate(lounge_server):
    url, _, _ = lounge_server
    times = frame_times(url, 4)
    fps = (len(times) - 1) / (times[-1] - times[0])
    assert 8 <= fps <= 12, fps
    status = json.loads(urlopen(url + "/status.json").read())
    assert status["state"] == "live"


def test_cropped_frames_are_smaller_than_the_camera_picture(lounge_server):
    url, _, _ = lounge_server
    frame_times(url, 0.5)
    with urlopen(url + "/frame.jpg", timeout=5) as response:
        jpeg = response.read()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"],
                           input=jpeg, capture_output=True, check=True).stdout.decode().strip()
    assert probe == "512,432"


def test_pause_ends_the_stream_and_refuses_new_ones(lounge_server):
    url, settings, _ = lounge_server
    with urlopen(url + lounge.STREAM_PATH, timeout=15) as response:
        parts = lounge.read_parts(response)
        next(parts)
        settings.write_text(json.dumps({"enabled": False, "crop": [0.2, 0.2, 0.4, 0.6], "fps": 10}))
        start = time.monotonic()
        for _ in parts:
            assert time.monotonic() - start < 3
    with pytest.raises(HTTPError) as error:
        urlopen(url + lounge.STREAM_PATH, timeout=15)
    assert error.value.code == 503
    assert json.loads(error.value.read())["state"] == "paused"


def test_camera_loss_ends_the_stream(lounge_server):
    url, _, online = lounge_server
    with urlopen(url + lounge.STREAM_PATH, timeout=15) as response:
        parts = lounge.read_parts(response)
        next(parts)
        online["value"] = False
        start = time.monotonic()
        for _ in parts:
            assert time.monotonic() - start < 10
    with pytest.raises(HTTPError) as error:
        urlopen(url + lounge.STREAM_PATH, timeout=15)
    assert error.value.code == 503
