#!/usr/bin/env python3
"""catcam-guard: keeps the AKASO cat cam from getting stuck overexposed.

Every CHECK_SECONDS it grabs a frame from µStreamer, measures how bright it is,
and if the picture stays washed out (or the camera stays offline) for
BAD_CHECKS checks in a row, it resets the camera over USB and restarts the
camera service -- the software equivalent of switching the AKASO off and on.

Usage:
  catcam_guard.py            run the watchdog (what the systemd service does)
  catcam_guard.py --check    measure the current frame once and print it
  catcam_guard.py --reset    reset the camera now
  catcam_guard.py --status   show the last events
Add --dry-run to log what would happen without touching the camera.
"""
import argparse
import fcntl
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

def env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default

STREAM_URL      = env("CATCAM_STREAM_URL", "http://127.0.0.1:8085")
CAMERA_PRODUCT  = env("CATCAM_USB_PRODUCT", "Brave 7 LE")
CAMERA_SERVICE  = env("CATCAM_SERVICE", "catcam-camera.service")
CHECK_SECONDS   = env("CATCAM_CHECK_SECONDS", 30, int)
BAD_CHECKS      = env("CATCAM_BAD_CHECKS", 4, int)          # 4 x 30s = 2 minutes
MEAN_MAX        = env("CATCAM_MEAN_MAX", 235.0, float)      # 0-255 average brightness
CLIPPED_MAX     = env("CATCAM_CLIPPED_MAX", 0.80, float)    # share of pure-white pixels
COOLDOWN        = env("CATCAM_COOLDOWN_SECONDS", 600, int)  # wait after a reset
MAX_RESETS      = env("CATCAM_MAX_RESETS", 3, int)          # per RESET_WINDOW
RESET_WINDOW    = env("CATCAM_RESET_WINDOW_SECONDS", 6 * 3600, int)
STATE_FILE      = env("CATCAM_STATE_FILE", "/var/lib/catcam-guard/state.json")

USBDEVFS_RESET = 0x5514  # _IO('U', 20)


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"events": [], "resets": []}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def record(state, kind, detail):
    state["events"] = (state.get("events", []) + [
        {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "detail": detail}
    ])[-50:]
    save_state(state)


# ---------- measuring ----------
def source_online():
    """True/False from µStreamer's /state, or None if it can't tell."""
    try:
        with urllib.request.urlopen(STREAM_URL + "/state", timeout=5) as r:
            data = json.load(r)
        online = data.get("result", {}).get("source", {}).get("online")
        return online if isinstance(online, bool) else None
    except Exception:
        return None

def measure():
    """Return {'mean': 0-255, 'clipped': 0-1} for the current frame, or None."""
    try:
        with urllib.request.urlopen(STREAM_URL + "/snapshot", timeout=10) as r:
            jpeg = r.read()
    except Exception as e:
        log(f"snapshot failed: {e}")
        return None
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0",
         "-frames:v", "1", "-vf", "scale=96:54,format=gray", "-f", "rawvideo", "pipe:1"],
        input=jpeg, capture_output=True, timeout=20)
    px = p.stdout
    if p.returncode != 0 or not px:
        log(f"could not decode snapshot: {p.stderr.decode(errors='replace').strip()[:200]}")
        return None
    return {"mean": round(sum(px) / len(px), 1),
            "clipped": round(sum(1 for v in px if v >= 245) / len(px), 3)}

def is_washed_out(m):
    return m is not None and m["mean"] >= MEAN_MAX and m["clipped"] >= CLIPPED_MAX


# ---------- recovering ----------
def find_camera():
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(d, "product")) as f:
                if f.read().strip() == CAMERA_PRODUCT:
                    return d
        except OSError:
            continue
    return None

def usb_reset(sysdir):
    with open(os.path.join(sysdir, "busnum")) as f:
        bus = int(f.read())
    with open(os.path.join(sysdir, "devnum")) as f:
        dev = int(f.read())
    node = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return node

def reauthorize(sysdir):
    path = os.path.join(sysdir, "authorized")
    with open(path, "w") as f:
        f.write("0")
    time.sleep(3)
    with open(path, "w") as f:
        f.write("1")

def restart_service():
    # The frame stage reads the camera directly, so it restarts first; then the stream.
    for unit in ("catcam-frame.service", CAMERA_SERVICE):
        subprocess.run(["systemctl", "restart", unit], check=False, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def recover(state, reason, dry_run=False, step=None):
    """Reset the camera. step 1 = USB reset, step 2 = unplug/replug in software."""
    now = time.time()
    recent = [t for t in state.get("resets", []) if now - t < RESET_WINDOW]
    if step is None:
        step = 2 if recent and now - recent[-1] < COOLDOWN * 3 else 1
    sysdir = find_camera()
    if not sysdir:
        log(f"camera '{CAMERA_PRODUCT}' not found on USB -- is it unplugged or switched off?")
        record(state, "camera-missing", reason)
        return False
    action = "USB reset" if step == 1 else "software unplug/replug"
    log(f"{reason} -> {action} of {sysdir}" + (" (dry run)" if dry_run else ""))
    if dry_run:
        return True
    try:
        usb_reset(sysdir) if step == 1 else reauthorize(sysdir)
    except OSError as e:
        log(f"{action} failed: {e}")
        if step == 1:
            return recover(state, reason, dry_run, step=2)
        record(state, "reset-failed", f"{reason}; {action}: {e}")
        return False
    time.sleep(8)  # let the AKASO re-enumerate
    restart_service()
    state["resets"] = recent + [now]
    record(state, "reset", f"{reason}; {action}")
    return True


# ---------- main loop ----------
def watch(dry_run=False):
    state = load_state()
    log(f"watching {STREAM_URL} every {CHECK_SECONDS}s; reset after {BAD_CHECKS} bad checks "
        f"(mean>={MEAN_MAX:g} and clipped>={CLIPPED_MAX:g}, or source offline)")
    time.sleep(min(60, CHECK_SECONDS * 2))  # let the camera service settle after boot
    bad = 0
    gave_up_logged = False
    while True:
        online = source_online()
        m = measure() if online is not False else None
        if online is not False and m is None:
            time.sleep(CHECK_SECONDS)  # stream unreachable; systemd restarts µStreamer itself
            continue
        if online is False:
            bad += 1; why = "camera offline"
        elif is_washed_out(m):
            bad += 1; why = f"picture washed out (mean {m['mean']}, {m['clipped']:.0%} white)"
        else:
            if bad:
                log(f"picture back to normal (mean {m['mean'] if m else '?'})")
            bad = 0; why = ""; gave_up_logged = False
        if bad and not gave_up_logged:
            log(f"{why} [{bad}/{BAD_CHECKS}]")
        if bad >= BAD_CHECKS:
            now = time.time()
            recent = [t for t in state.get("resets", []) if now - t < RESET_WINDOW]
            if len(recent) >= MAX_RESETS:
                if not gave_up_logged:
                    log(f"already reset {len(recent)} times in {RESET_WINDOW // 3600}h; "
                        "not resetting again -- the camera needs a real power cycle")
                    record(state, "gave-up", why)
                    gave_up_logged = True
            else:
                gave_up_logged = False
                recover(state, why, dry_run)
                bad = 0
                time.sleep(COOLDOWN if not dry_run else CHECK_SECONDS)
                continue
        time.sleep(CHECK_SECONDS)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="measure the current frame once")
    g.add_argument("--reset", action="store_true", help="reset the camera now")
    g.add_argument("--status", action="store_true", help="show recent events")
    ap.add_argument("--dry-run", action="store_true", help="don't touch the camera")
    a = ap.parse_args()

    if a.check:
        m, online = measure(), source_online()
        cam = find_camera()
        print(json.dumps({"source_online": online, "frame": m,
                          "washed_out": is_washed_out(m),
                          "thresholds": {"mean": MEAN_MAX, "clipped": CLIPPED_MAX},
                          "camera_usb": cam}, indent=2))
        return 0
    if a.status:
        s = load_state()
        for e in s.get("events", [])[-15:]:
            print(f"{e['time']}  {e['kind']:<15} {e['detail']}")
        if not s.get("events"):
            print("no events yet -- the camera hasn't needed a reset")
        return 0
    if a.reset:
        return 0 if recover(load_state(), "manual reset", a.dry_run) else 1
    watch(a.dry_run)

if __name__ == "__main__":
    sys.exit(main() or 0)
