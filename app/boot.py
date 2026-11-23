"""Install the existing Cat Cam on mina as two boot-time systemd services."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


CAMERA = "catcam-camera.service"
RECORDER = "catcam-recorder.service"
MARK = "# Managed by install_catcam.py"
APP = Path("/home/faure/catcam.py")
HELPER = Path("/home/faure/.local/share/catcam/boot.py")
UNITS = Path("/etc/systemd/system")


def camera_node(listing):
    for group in re.split(r"\n\s*\n", listing.strip()):
        lines = group.splitlines()
        if lines and "brave 7 le" in lines[0].lower():
            for line in lines[1:]:
                if re.fullmatch(r"/dev/video\d+", line.strip()):
                    return line.strip()
    return None


def find_camera():
    result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
    return camera_node(result.stdout)


def stream():
    # Cat Cam frame stage: when it is enabled, stream ONLY its windowsill view. If it is not
    # running, wait rather than fall back to the whole room (privacy fails closed).
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import frame
        config = frame.load_config()
    except Exception as error:  # a broken frame module must not take the camera down
        print(f"Frame stage unavailable ({error}); streaming the camera directly.", flush=True)
        config = {"enabled": False}
    if config.get("enabled"):
        if not frame.loopback_ready():
            raise SystemExit("Waiting for the frame stage's virtual camera (/dev/video10). Retrying.")
        width, height = frame.output_size(config)
        os.execvp("ustreamer", ["ustreamer", "--device=/dev/video10", "--format=YUYV",
                                f"--resolution={width}x{height}", f"--desired-fps={config['fps']}",
                                "--device-timeout=5", "--host=127.0.0.1", "--port=8085"])
    node = find_camera()
    if not node:
        raise SystemExit("Waiting for Brave 7 LE in PCCAM mode. Retrying in five seconds.")
    # A persistent link survives changes to the kernel's video device numbering.
    for link in sorted(Path("/dev/v4l/by-id").glob("*-video-index0")):
        if link.resolve() == Path(node):
            node = str(link)
            break
    os.execvp("ustreamer", ["ustreamer", "--device=" + node, "--format=MJPEG",
                            "--resolution=1280x720", "--desired-fps=15",
                            "--host=127.0.0.1", "--port=8085"])


def unit(description, command, dependency=""):
    return f"""{MARK}
[Unit]
Description={description}
{dependency}StartLimitIntervalSec=0
RequiresMountsFor=/home/faure

[Service]
Type=exec
User=faure
SupplementaryGroups=video
WorkingDirectory=/home/faure
ExecStart={command}
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=20
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def active(name, user=False):
    scope = ["--user"] if user else []
    return subprocess.run(["systemctl", *scope, "is-active", "--quiet", name]).returncode == 0


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def healthy():
    with urlopen("http://127.0.0.1:8085/state", timeout=2) as response:
        source = json.load(response)["result"]["source"]
    with urlopen("http://127.0.0.1:8086/", timeout=2) as response:
        page = response.read()
    return (source.get("online") and source.get("captured_fps", 0) > 0
            and b"Recorded visits" in page and active(RECORDER))


def install():
    import pwd
    if pwd.getpwuid(os.getuid()).pw_name != "faure" or Path.home() != APP.parent:
        raise SystemExit("Run this as faure on mina, without sudo. The installer requests sudo when needed.")
    for program in ("ustreamer", "motion", "v4l2-ctl", "systemctl", "systemd-analyze", "sudo"):
        if not shutil.which(program):
            raise SystemExit(f"Missing program: {program}")
    if not APP.is_file():
        raise SystemExit(f"Missing existing recorder: {APP}")
    if not find_camera():
        raise SystemExit("Select PCCAM on the Brave 7 LE, then run this installer again.")
    old_stream = active("catcam-stream.service", user=True)
    if listening(8086) and not active(RECORDER):
        raise SystemExit("Port 8086 is still in use on mina. Stop the old catcam.py session with Ctrl+C and rerun.")
    if listening(8085) and not (active(CAMERA) or old_stream):
        raise SystemExit("Port 8085 is occupied by an unmanaged process. Stop the old foreground uStreamer and rerun.")
    files = {
        CAMERA: unit("AKASO cat camera", f"/usr/bin/python3 {HELPER} --stream"),
        RECORDER: unit("Cat Cam recorder and viewer", f"/usr/bin/python3 -u {APP}",
                       f"Wants={CAMERA}\nAfter={CAMERA}\n"),
    }
    for name in files:
        dest = UNITS / name
        if dest.exists() and not dest.read_text().startswith(MARK + "\n"):
            raise SystemExit(f"Existing unmanaged service: {dest}")
    subprocess.run(["sudo", "-v"], check=True)
    HELPER.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=HELPER.parent, delete=False) as tmp:
        tmp.write(Path(__file__).read_bytes())
    os.replace(tmp.name, HELPER)
    with tempfile.TemporaryDirectory() as directory:
        staged = []
        for name, body in files.items():
            path = Path(directory, name)
            path.write_text(body)
            staged.append(str(path))
        subprocess.run(["systemd-analyze", "verify", *staged], check=True)
        for path in staged:
            subprocess.run(["sudo", "install", "-m", "0644", path, str(UNITS)], check=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", CAMERA, RECORDER], check=True)
    if old_stream:
        subprocess.run(["systemctl", "--user", "stop", "catcam-stream.service"], check=True)
    subprocess.run(["sudo", "systemctl", "start", CAMERA, RECORDER], check=True)
    end = time.monotonic() + 45
    while time.monotonic() < end:
        try:
            if healthy():
                print("Cat Cam is live. Camera and recorder are enabled at boot. You may close SSH.")
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    subprocess.run(["sudo", "journalctl", "-b", "-u", CAMERA, "-u", RECORDER, "-n", "30", "--no-pager"])
    raise SystemExit("Services installed. Live capture did not pass the startup check. See the journal above.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--stream"]:
        stream()
    else:
        install()
