"""Cat Lounge: a loopback-only, read-only derivative of the private Cat Cam.

This module provides the cropped camera and legacy viewer. New installations
run pin_lounge.py, which requires a session before serving media. Never point
Funnel at this port or at the private Cat Cam.
Only cropped JPEGs leave this process. Missing/invalid settings fail closed.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_CROP = [0.225, 0.25, 0.30, 0.68]  # x,y,width,height; November 21 camera angle
DEFAULT_CONFIG = {"enabled": True, "crop": DEFAULT_CROP, "fps": 10}
MAX_FPS = 12
STREAM_PATH = "/perch.mjpg"
STREAM_SECONDS = 600  # streams end and the viewer reconnects, which re-checks the session
MAX_STREAMS = 8
MAX_FRAME = 4 * 1024 * 1024
ASSETS = {"/": ("lounge.html", "text/html; charset=utf-8"),
          "/lounge.css": ("lounge.css", "text/css; charset=utf-8"),
          "/lounge.js": ("lounge.js", "application/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


def read_settings(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"enabled", "crop", "fps"} or type(data["enabled"]) is not bool:
        raise ValueError("Invalid settings")
    crop = data["crop"]
    if not isinstance(crop, list) or len(crop) != 4:
        raise ValueError("A four-value crop is required")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in crop):
        raise ValueError("Crop must contain finite numbers")
    x, y, w, h = crop
    if not (0 <= x < 1 and 0 <= y < 1 and .08 <= w <= 1 and .08 <= h <= 1
            and x + w <= 1.000001 and y + h <= 1.000001):
        raise ValueError("Crop is outside the picture")
    if type(data["fps"]) not in (int, float) or not .5 <= data["fps"] <= MAX_FPS:
        raise ValueError(f"Choose 0.5 to {MAX_FPS} frames per second")
    token = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data, token


def crop_jpeg(data, crop):
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_FRAME:
        raise ValueError("Invalid source image")
    x, y, w, h = crop
    filt = (f"crop=trunc(iw*{w:.6f}/2)*2:trunc(ih*{h:.6f}/2)*2:"
            f"trunc(iw*{x:.6f}/2)*2:trunc(ih*{y:.6f}/2)*2,"
            "scale=w='min(640,iw)':h=-2")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-f", "image2pipe", "-i", "pipe:0", "-vf", filt, "-frames:v", "1", "-an",
        "-threads", "1", "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=6, check=True)
    if not result.stdout.startswith(b"\xff\xd8") or len(result.stdout) > MAX_FRAME:
        raise ValueError("Crop did not produce a JPEG")
    return result.stdout


def ffmpeg_command(source, crop):
    """One long-running ffmpeg: µStreamer MJPEG in, cropped multipart JPEGs out."""
    x, y, w, h = crop
    # Pacing happens in Python: ffmpeg's fps filter misreads multipart timestamps.
    filt = (f"crop=trunc(iw*{w:.6f}/2)*2:trunc(ih*{h:.6f}/2)*2:"
            f"trunc(iw*{x:.6f}/2)*2:trunc(ih*{y:.6f}/2)*2,"
            "scale=w='min(640,iw)':h=-2")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1",
            "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "65536", "-analyzeduration", "0",
            "-f", "mpjpeg", "-i", source.rstrip("/") + "/stream",
            "-an", "-vf", filt, "-q:v", "6", "-flush_packets", "1", "-f", "mpjpeg", "-boundary_tag", "lounge", "pipe:1"]


def read_parts(stream):
    """Yield JPEGs from ffmpeg's multipart output. Stops at end of stream."""
    while True:
        line = stream.readline(256)
        if not line:
            return
        if not line.startswith(b"--"):
            continue
        length = None
        while True:
            header = stream.readline(512)
            if not header:
                return
            if header in (b"\r\n", b"\n"):
                break
            name, _, value = header.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        if length is None or not 0 < length <= MAX_FRAME:
            raise ValueError("Bad frame from ffmpeg")
        data = stream.read(length)
        if len(data) != length:
            return
        if data.startswith(b"\xff\xd8"):
            yield data


class Camera:
    """Keeps the newest cropped frame. Runs ffmpeg only while someone is watching."""

    def __init__(self, settings, source="http://127.0.0.1:8085"):
        self.settings, self.source = Path(settings), source
        self.lock = threading.Lock()
        self.fresh = threading.Condition(self.lock)
        self.data, self.token, self.updated, self.last_viewer, self.sequence = None, None, 0, 0, 0
        self.stop = threading.Event()
        self.max_age = 3
        self.idle_after = 15
        self.stall_after = 6

    def touch(self):
        self.last_viewer = time.monotonic()

    def snapshot(self, touch=True):
        if touch:
            self.touch()
        try:
            settings, token = read_settings(self.settings)
        except (OSError, ValueError, TypeError):
            return None, {"state": "paused", "fps": DEFAULT_CONFIG["fps"]}
        if not settings["enabled"]:
            return None, {"state": "paused", "fps": settings["fps"]}
        with self.lock:
            age = time.monotonic() - self.updated
            if self.data and self.token == token and age < self.max_age:
                return self.data, {"state": "live", "fps": settings["fps"], "age_seconds": round(age, 2)}
        return None, {"state": "offline", "fps": settings["fps"]}

    def next_frame(self, after, timeout=2.0):
        """Wait for a frame newer than `after`. Returns (sequence, jpeg, token)."""
        self.touch()
        with self.fresh:
            self.fresh.wait_for(lambda: self.sequence != after or self.stop.is_set(), timeout)
            fresh = self.data and time.monotonic() - self.updated < self.max_age
            return self.sequence, (self.data if fresh else None), self.token

    def publish(self, data, token):
        with self.fresh:
            self.data, self.token, self.updated = data, token, time.monotonic()
            self.sequence += 1
            self.fresh.notify_all()

    def clear(self):
        with self.fresh:
            self.data = None
            self.fresh.notify_all()

    def camera_online(self):
        try:
            with urlopen(self.source.rstrip("/") + "/state", timeout=2) as response:
                state = json.loads(response.read(65536))
            return state["result"]["source"]["online"] is True
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def wanted(self):
        return time.monotonic() - self.last_viewer < self.idle_after

    def run(self):
        while not self.stop.is_set():
            pause = .5
            try:
                settings, token = read_settings(self.settings)
                if settings["enabled"] and self.wanted():
                    if self.camera_online():
                        self.pump(settings, token)
                        pause = 1
                    else:
                        pause = 2
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                pause = 2
            self.clear()
            self.stop.wait(pause)

    def pump(self, settings, token):
        """Run ffmpeg until the viewer leaves, settings change, the camera drops or ffmpeg stalls."""
        process = subprocess.Popen(ffmpeg_command(self.source, settings["crop"]),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        last = {"frame": time.monotonic()}

        gap = 1 / settings["fps"]

        def reader():
            due = 0
            try:
                for frame in read_parts(process.stdout):
                    now = last["frame"] = time.monotonic()
                    if now + gap / 4 < due:
                        continue  # faster than the lounge rate; skip this one
                    due = max(due + gap, now)
                    self.publish(frame, token)
            except (OSError, ValueError):
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        checked = time.monotonic()
        try:
            while not self.stop.is_set() and process.poll() is None and thread.is_alive():
                self.stop.wait(.5)
                now = time.monotonic()
                if not self.wanted() or now - last["frame"] > self.stall_after:
                    break
                # Never keep publishing the previous crop after a settings change.
                if read_settings(self.settings)[1] != token:
                    break
                if now - checked > 3:
                    checked = now
                    if not self.camera_online():
                        break
        finally:
            process.kill()
            process.wait(timeout=5)
            thread.join(timeout=2)
            self.clear()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream_lock = threading.Lock()
        self.streams = 0


class Handler(BaseHTTPRequestHandler):
    server_version = "CatLounge"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, data, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (OSError, ConnectionError):
                pass

    def do_GET(self):
        # Deliberately no URL decoding, filesystem lookup, user-selected upstream or proxy.
        path = urlsplit(self.path).path
        if path in ASSETS:
            name, mime = ASSETS[path]
            self.reply(200, (ROOT / name).read_bytes(), mime)
        elif path == STREAM_PATH:
            self.stream()
        elif path in ("/frame.jpg", "/status.json"):
            frame, status = self.server.camera.snapshot()
            if path == "/status.json":
                self.reply(200, json.dumps(status).encode(), "application/json")
            elif frame:
                self.reply(200, frame, "image/jpeg")
            else:
                self.reply(503, json.dumps(status).encode(), "application/json")
        else:
            self.reply(404, b"Not found", "text/plain")

    do_HEAD = do_GET

    def still_allowed(self):
        return True  # the PIN handler re-checks the session here

    def stream(self):
        """multipart/x-mixed-replace of cropped JPEGs. Ends on pause, crop change,
        camera loss, sign-out or after STREAM_SECONDS; the viewer reconnects."""
        server = self.server
        with server.stream_lock:
            if server.streams >= MAX_STREAMS:
                return self.reply(503, b'{"state":"busy"}', "application/json")
            server.streams += 1
        try:
            camera = server.camera
            frame, status = camera.snapshot()
            sequence = camera.sequence
            if not frame and status["state"] == "offline" and camera.camera_online():
                # Give a just-woken ffmpeg a moment before declaring the camera offline.
                for _ in range(16):
                    sequence, frame, _ = camera.next_frame(sequence, timeout=.5)
                    if frame or camera.snapshot(touch=False)[1]["state"] == "paused":
                        break
                frame, status = camera.snapshot()
            if not frame:
                return self.reply(503, json.dumps(status).encode(), "application/json")
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            if self.command == "HEAD":
                return
            self.connection.settimeout(15)
            started = checked = time.monotonic()
            while True:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
                now = time.monotonic()
                if now - started > STREAM_SECONDS:
                    return
                if now - checked > 20:
                    checked = now
                    if not self.still_allowed():
                        return
                for _ in range(4):
                    sequence, frame, token = camera.next_frame(sequence, timeout=1)
                    if frame:
                        break
                if not frame:
                    return  # camera stalled or went offline; the viewer shows the curtain
                try:
                    settings, current = read_settings(camera.settings)
                except (OSError, ValueError, TypeError):
                    return
                if not settings["enabled"] or token != current:
                    return
        except (OSError, ConnectionError):
            pass
        finally:
            with server.stream_lock:
                server.streams -= 1

    def reject(self):
        self.close_connection = True
        self.reply(405, b"Read-only viewer", "text/plain")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_CONNECT = do_TRACE = reject

    def log_message(self, *_args):
        pass  # no request paths, usernames, or credentials in logs


def make_server(settings, port=8088, source="http://127.0.0.1:8085"):
    server = Server(("127.0.0.1", port), Handler)
    server.camera = Camera(settings, source)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home()/".config/catcam-lounge/settings.json")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    server = make_server(args.config, args.port)
    threading.Thread(target=server.camera.run, daemon=True).start()
    print(f"Cat Lounge is listening on loopback port {args.port}; authentication is required at Caddy.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.camera.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
