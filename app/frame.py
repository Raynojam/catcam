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

import json
import os
import re
import signal
import subprocess
import sys
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
DEFAULTS = {"enabled": True, "zone": None, "low_light": True, "strength": 0.5, "output": "1280x720",
            "capture": "1920x1080", "fps": 15, "camera": "auto"}
# Cameras the frame stage will use, best first. Never the laptop's own webcam: it films the room.
KNOWN_CAMERAS = ("zv-1f", "brave 7 le")
# v4l2 pixel formats ffmpeg can read, best first (MJPEG keeps USB bandwidth low).
INPUT_FORMATS = (("MJPG", "mjpeg"), ("YUYV", "yuyv422"), ("NV12", "nv12"))
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


def camera_order(setting):
    """The configured camera first, then the known ones. "auto" (and the old AKASO default) = known order."""
    setting = str(setting or "auto").strip().lower()
    if setting in ("auto", "brave 7 le"):
        return KNOWN_CAMERAS
    return (setting,) + tuple(c for c in KNOWN_CAMERAS if c != setting)


def pick_camera(listing, setting):
    for name in camera_order(setting):
        node = camera_node(listing, name)
        if node:
            return node, name
    return None, None


def find_camera(setting):
    result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
    return pick_camera(result.stdout, setting)


def parse_modes(listing):
    """v4l2-ctl --list-formats-ext -> {fourcc: {"WxH": [fps, ...]}}."""
    modes, fourcc, size = {}, None, None
    for line in listing.splitlines():
        if m := re.search(r"\[\d+\]: '(\w{4})'", line):
            fourcc, size = m.group(1), None
            modes.setdefault(fourcc, {})
        elif fourcc and (m := re.search(r"Size: Discrete (\d+x\d+)", line)):
            size = m.group(1)
            modes[fourcc].setdefault(size, [])
        elif fourcc and size and (m := re.search(r"\(([\d.]+) fps\)", line)):
            modes[fourcc][size].append(float(m.group(1)))
    return modes


def choose_mode(modes, config):
    """Best (input format, size, rate) the camera offers for this config, or None to use the config as is."""
    for fourcc, ffmpeg_format in INPUT_FORMATS:
        sizes = modes.get(fourcc)
        if not sizes:
            continue
        def area(size):
            w, h = (int(v) for v in size.split("x"))
            return w * h
        fits = [s for s in sizes if area(s) <= 1920 * 1080] or list(sizes)
        size = config["capture"] if config["capture"] in fits else max(fits, key=area)
        rates = sorted(sizes[size])
        rate = next((r for r in rates if r >= config["fps"] - 0.01), rates[-1] if rates else config["fps"])
        return ffmpeg_format, size, rate
    return None


def probe(node, config):
    """Fill in what this camera actually delivers: input format, capture size and capture rate."""
    result = subprocess.run(["v4l2-ctl", "-d", node, "--list-formats-ext"], capture_output=True, text=True)
    mode = choose_mode(parse_modes(result.stdout), config)
    if not mode:
        return {**config, "input_format": "mjpeg", "capture_fps": config["fps"]}
    ffmpeg_format, size, rate = mode
    return {**config, "input_format": ffmpeg_format, "capture": size, "capture_fps": rate}


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
    if config.get("capture_fps", config["fps"]) > config["fps"] + 0.5:
        steps.insert(0, f"fps={config['fps']}")  # e.g. the Sony only offers 30; the stream wants 15
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
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", config.get("input_format", "mjpeg"), "-video_size", config["capture"],
            "-framerate", f"{config.get('capture_fps', config['fps']):g}", "-i", node, "-filter_complex", graph,
            "-map", "[out]", "-f", "v4l2", str(LOOPBACK)]
    if has_snap:
        args += ["-map", "[snap]", "-f", "image2", "-update", "1", "-q:v", "5", str(PREVIEW)]
    return args


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
        node, camera = find_camera(config["camera"])
        if not node:
            write_status(state="waiting", message="Camera not found. Is the Sony on in USB Streaming mode "
                                                  "(or the AKASO in PCCAM mode)?")
            time.sleep(5)
            continue
        config = probe(node, config)
        preview = preview_wanted()
        stamp = config_stamp()
        log(f"frame stage: {camera} {node} {config['input_format']} {config['capture']}@{config['capture_fps']:g} -> zone {config['zone'] or 'whole frame'} -> "
            f"{config['output']}, low light {'on' if config['low_light'] else 'off'}"
            + (", preview on" if preview else ""))
        process = subprocess.Popen(command(node, config, preview), stdin=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
        started = time.time()
        write_status(state="running", device=node, camera=camera, capture=config["capture"], zone=config["zone"], output=config["output"],
                     low_light=config["low_light"], preview=preview, started=started)
        reason = ""
        while process.poll() is None and not stop["now"]:
            time.sleep(1)
            if config_stamp() != stamp:
                reason = "settings changed"
            elif preview_wanted() != preview:
                reason = "preview " + ("started" if not preview else "ended")
            if reason:
                process.terminate()
                break
        try:
            _, err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, err = process.communicate()
        if not preview_wanted():
            PREVIEW.unlink(missing_ok=True)
        if stop["now"]:
            break
        if reason:
            log(f"frame stage: restarting ({reason})")
            continue
        lines = [l for l in (err or "").strip().splitlines() if "Last message repeated" not in l] or ["ffmpeg stopped"]
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
        node, _ = find_camera(config["camera"])
        print(" ".join(command(node or "/dev/videoN", probe(node, config) if node else config, preview_wanted())))
    else:
        run()
