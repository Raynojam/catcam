"""Cat Cam frame stage: camera -> windowsill crop -> low-light fix -> virtual camera.

Runs as catcam-frame.service. Everything downstream (µStreamer, Motion, the detector,
the diary and the Studio) reads /dev/video10, so it only ever sees the windowsill.

Settings live in ~/.config/catcam/frame.json and are changed from the site's Camera page:
  zone       [x, y, w, h] as fractions of the full frame (16:9), or null for the whole frame
  low_light  true to auto-brighten and denoise dark scenes
  strength   0.0-1.0, how hard to brighten
  output     "1280x720" or "1920x1080"

When the Camera page asks for a preview, a full-frame snapshot is written every
2 seconds for two minutes, so the zone can be drawn; it is deleted afterwards.
"""

from collections import deque
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
CONFIG = HOME / ".config" / "catcam" / "frame.json"
STATE_DIR = HOME / ".local" / "share" / "catcam" / "frame"
STATUS = STATE_DIR / "status.json"
PREVIEW = STATE_DIR / "full.jpg"
PREVIEW_REQUEST = STATE_DIR / "preview-request"
LOOPBACK = Path("/dev/video10")
LOOPBACK_LABEL = "CatCamFrame"
PREVIEW_SECONDS = 120
STARTUP_TIMEOUT = 30
FRAME_TIMEOUT = 15
STATUS_INTERVAL = 5
DEFAULTS = {"enabled": True, "zone": None, "low_light": True, "strength": 0.5, "output": "1280x720",
            "capture": "1920x1080", "fps": 15, "camera": "brave 7 le"}
OUTPUTS = {"1280x720", "1920x1080"}


def log(message):
    print(message, flush=True)


def load_config():
    config = dict(DEFAULTS)
    try:
        stored = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        config.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return validate(config)


def validate(config):
    """Clamp anything odd back to something safe. Raises nothing."""
    zone = config.get("zone")
    if zone is not None:
        try:
            x, y, w, h = (float(v) for v in zone)
            w, h = min(1.0, max(0.1, w)), min(1.0, max(0.1, h))
            x, y = min(1.0 - w, max(0.0, x)), min(1.0 - h, max(0.0, y))
            config["zone"] = [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]
        except (TypeError, ValueError):
            config["zone"] = None
    config["low_light"] = config.get("low_light") is True
    try:
        config["strength"] = min(1.0, max(0.0, float(config.get("strength", 0.5))))
    except (TypeError, ValueError):
        config["strength"] = 0.5
    if config.get("output") not in OUTPUTS:
        config["output"] = "1280x720"
    if not re.fullmatch(r"\d{3,4}x\d{3,4}", str(config.get("capture", ""))):
        config["capture"] = DEFAULTS["capture"]
    try:
        config["fps"] = min(30, max(5, int(config.get("fps", 15))))
    except (TypeError, ValueError):
        config["fps"] = 15
    config["enabled"] = config.get("enabled") is not False
    return config


def output_size(config=None):
    width, height = (config or load_config())["output"].split("x")
    return int(width), int(height)


def camera_node(listing, match):
    for group in re.split(r"\n\s*\n", listing.strip()):
        lines = group.splitlines()
        if lines and match in lines[0].lower():
            for line in lines[1:]:
                if re.fullmatch(r"/dev/video\d+", line.strip()):
                    return line.strip()
    return None


def find_camera(match):
    result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
    return camera_node(result.stdout, match)


def loopback_ready():
    try:
        name = Path("/sys/class/video4linux/video10/name").read_text().strip()
    except OSError:
        return False
    return LOOPBACK.exists() and name.startswith("CatCam")   # "CatCam" from the first manual setup


def crop_filter(zone, capture):
    """ffmpeg crop for a fractional zone, on even pixel bounds."""
    cw, ch = (int(v) for v in capture.split("x"))
    if not zone:
        return None
    x, y, w, h = zone
    pw, ph = int(w * cw) // 2 * 2, int(h * ch) // 2 * 2
    px, py = int(x * cw) // 2 * 2, int(y * ch) // 2 * 2
    pw, ph = min(pw, cw - px), min(ph, ch - py)
    return f"crop={pw}:{ph}:{px}:{py}"


def filter_graph(config, preview):
    out_w, out_h = output_size(config)
    steps = [f"scale={config['capture'].replace('x', ':')}"]
    crop = crop_filter(config["zone"], config["capture"])
    if crop:
        steps.append(crop)
    # Fit inside the output frame without stretching; pad keeps a fixed size for µStreamer and Motion.
    steps.append(f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease")
    steps.append(f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    if config["low_light"] and config["strength"] > 0:
        s = config["strength"]
        # normalize stretches dark, flat frames over ~3 s; on a bright, full-range frame it does little.
        steps.append(f"normalize=smoothing={config['fps'] * 3}:independence=0:strength={s:.2f}")
        steps.append(f"hqdn3d={1 + 2 * s:.1f}:{1 + 1.5 * s:.1f}:{3 + 3 * s:.1f}:{3 + 3 * s:.1f}")
    steps.append("format=yuyv422")
    main = ",".join(steps)
    if not preview:
        return f"[0:v]{main}[out]", False
    return f"[0:v]split=2[a][b];[a]{main}[out];[b]fps=1/2,scale=960:-2[snap]", True


def command(node, config, preview):
    graph, has_snap = filter_graph(config, preview)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
            "-stats_period", "1", "-progress", "pipe:1",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", config["capture"],
            "-framerate", str(config["fps"]), "-i", node, "-filter_complex", graph,
            "-map", "[out]", "-f", "v4l2", str(LOOPBACK)]
    if has_snap:
        args += ["-map", "[snap]", "-f", "image2", "-update", "1", "-q:v", "5", str(PREVIEW)]
    return args


class FrameProgress:
    """Track frames submitted to the first output (the virtual camera), using monotonic time."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.frames = 0
        self.last_advance = clock()

    def read(self, stream):
        for line in stream:
            key, _, value = line.strip().partition("=")
            value = value.strip()
            if key != "frame" or not value.isascii() or not value.isdecimal():
                continue
            frames = int(value)
            with self.lock:
                if frames > self.frames:
                    self.frames = frames
                    self.last_advance = self.clock()

    def status(self):
        with self.lock:
            frames = self.frames
            age = self.clock() - self.last_advance
        deadline = FRAME_TIMEOUT if frames else STARTUP_TIMEOUT
        return frames, max(0, age), age >= deadline


def read_errors(stream, tail):
    # Read while the camera runs so a noisy MJPEG source cannot fill FFmpeg's pipe.
    for line in stream:
        message = line.strip()
        if message and "Last message repeated" not in message:
            tail.append(message[-500:])


def write_status(**fields):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": time.time(), **fields}))
    tmp.replace(STATUS)


def preview_wanted():
    try:
        return time.time() - PREVIEW_REQUEST.stat().st_mtime < PREVIEW_SECONDS
    except OSError:
        return False


def config_stamp():
    try:
        return CONFIG.stat().st_mtime_ns
    except OSError:
        return 0


def run():
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    while not stop["now"]:
        config = load_config()
        if not loopback_ready():
            write_status(state="error", message="The virtual camera (/dev/video10) is missing. Run catcam-update.")
            time.sleep(10)
            continue
        node = find_camera(config["camera"])
        if not node:
            write_status(state="waiting", message="Camera not found. Is it on and in webcam (PCCAM) mode?")
            time.sleep(5)
            continue
        preview = preview_wanted()
        stamp = config_stamp()
        log(f"frame stage: {node} {config['capture']} -> zone {config['zone'] or 'whole frame'} -> "
            f"{config['output']}, low light {'on' if config['low_light'] else 'off'}"
            + (", preview on" if preview else ""))
        process = subprocess.Popen(command(node, config, preview), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace")
        progress = FrameProgress()
        errors = deque(maxlen=10)
        readers = [threading.Thread(target=progress.read, args=(process.stdout,), daemon=True),
                   threading.Thread(target=read_errors, args=(process.stderr, errors), daemon=True)]
        for reader in readers:
            reader.start()
        started = time.time()
        fields = dict(device=node, zone=config["zone"], output=config["output"],
                      low_light=config["low_light"], preview=preview, started=started)
        write_status(state="starting", message="Waiting for the first camera frame.", frames=0, **fields)
        last_status = None
        reason = ""
        stalled = False
        while process.poll() is None and not stop["now"]:
            time.sleep(1)
            if config_stamp() != stamp:
                reason = "settings changed"
            elif preview_wanted() != preview:
                reason = "preview " + ("started" if not preview else "ended")
            else:
                frames, age, stalled = progress.status()
                if stalled:
                    reason = f"No new camera frames for {int(age)} seconds. Restarting the frame stage."
                    write_status(state="error", message=reason, frames=frames, **fields)
                elif frames and (last_status is None or time.monotonic() - last_status >= STATUS_INTERVAL):
                    write_status(state="running", frames=frames, frame_age_seconds=round(age, 1), **fields)
                    last_status = time.monotonic()
            if reason:
                break
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
        if not preview_wanted():
            PREVIEW.unlink(missing_ok=True)
        if stop["now"]:
            break
        if reason:
            log(f"frame stage: restarting ({reason})")
            if stalled:
                time.sleep(5)
            continue
        lines = list(errors) or ["ffmpeg stopped"]
        log(f"frame stage: ffmpeg exited with {process.returncode}:")
        for line in lines[-6:]:
            log(f"  {line}")
        useful = next((l for l in reversed(lines) if "Nothing was written" not in l and "Conversion failed" not in l), lines[-1])
        write_status(state="error", message=useful[:200], device=node)
        time.sleep(5 if time.time() - started > 30 else 15)
    PREVIEW.unlink(missing_ok=True)
    write_status(state="stopped")


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-command"]:
        config = load_config()
        print(" ".join(command(find_camera(config["camera"]) or "/dev/videoN", config, preview_wanted())))
    else:
        run()
