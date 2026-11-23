#!/usr/bin/env bash
# Install this checkout of Cat Cam onto mina. catcam-update runs it; it is safe to re-run.
# Every run backs up what is installed, installs, checks the site and the camera, and puts
# the backup back by itself if a check fails.
#
#   bash deploy/install.sh            install this checkout
#   bash deploy/install.sh --force    install even if catcam.py was edited by hand on mina
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$REPO/VERSION")"
APP="$HOME/catcam.py"
SHARE="$HOME/.local/share/catcam"
HD="$SHARE/highlights"
VENV_PY="$HD/venv/bin/python"
ROLLBACK="$SHARE/rollback"
RELEASE="$SHARE/release"
UNITS=/etc/systemd/system
HELPERS=(boot.py frame.py insights.py studio.py identity.py)
DINO_URL="https://huggingface.co/Xenova/dinov2-small/resolve/main/onnx/model_quantized.onnx"
DINO_SHA="3afdc8bc63b50558d6e5770f5b799bb82455c2311183a2de43803f343a29d917"

say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mStopped:\033[0m %s\n' "$*"; exit 1; }
sha()  { sha256sum "$1" | cut -d' ' -f1; }

[ "$(id -u)" -ne 0 ] || die "run as faure without sudo; it asks for sudo when it needs it."
[ "$(id -un)" = faure ] || die "run this as faure on mina."
[ -x "$VENV_PY" ] || die "the Cat Cam detector environment ($VENV_PY) is missing."
[ "$(systemctl show -p LoadState --value catcam-recorder.service)" = loaded ] || die "catcam-recorder.service is not installed."

say "Cat Cam $VERSION"
if [ -f "$APP" ] && [ "${1:-}" != "--force" ]; then
  CURRENT=$(sha "$APP")
  if ! grep -qx "$CURRENT" "$REPO/deploy/known-catcam-shas.txt" && \
     [ "$CURRENT" != "$(cat "$RELEASE/catcam.sha" 2>/dev/null)" ]; then
    die "catcam.py in your home folder was changed on mina outside the repo. Nothing was touched. Run with --force to overwrite it."
  fi
fi
sudo -v

say "1/5 System pieces"
dpkg -s v4l2loopback-dkms >/dev/null 2>&1 || sudo apt-get install -y v4l2loopback-dkms v4l2loopback-utils
echo 'options v4l2loopback video_nr=10 card_label=CatCamFrame exclusive_caps=1' | sudo tee /etc/modprobe.d/catcam-v4l2loopback.conf >/dev/null
echo 'v4l2loopback' | sudo tee /etc/modules-load.d/catcam.conf >/dev/null
if [ ! -e "${CATCAM_LOOPBACK_DEV:-/dev/video10}" ]; then sudo modprobe v4l2loopback; sleep 1; fi
grep -q '^CatCam' "${CATCAM_LOOPBACK_NAME:-/sys/class/video4linux/video10/name}" 2>/dev/null || die "/dev/video10 is not the Cat Cam virtual camera; check: cat /sys/class/video4linux/video10/name"
for f in "$REPO"/deploy/units/*.service; do systemd-analyze verify "$f" 2>&1 | grep -v -e 'ollama.service' -e '^$' || true; done
sudo install -m 0644 "$REPO"/deploy/units/catcam-frame.service "$REPO"/deploy/units/catcam-insights.service "$UNITS/"
sudo install -d "$UNITS/catcam-camera.service.d"
sudo install -m 0644 "$REPO/deploy/units/catcam-camera-frame.conf" "$UNITS/catcam-camera.service.d/20-frame.conf"
sudo install -m 0755 "$REPO/deploy/catcam-update" /usr/local/bin/catcam-update
sudo install -m 0755 "$REPO/deploy/catcam-insights" /usr/local/bin/catcam-insights
sudo systemctl daemon-reload
if [ ! -f "$HD/dinov2-small.onnx" ]; then
  echo "  Downloading the photo-matching model for cat identity (about 25 MB)…"
  curl -fsSL -o "$HD/dinov2-small.onnx.part" "$DINO_URL"
  [ "$(sha "$HD/dinov2-small.onnx.part")" = "$DINO_SHA" ] || { rm -f "$HD/dinov2-small.onnx.part"; die "the identity model download didn't match its checksum."; }
  mv "$HD/dinov2-small.onnx.part" "$HD/dinov2-small.onnx"
fi
[ -f "$HD/yolox_s.onnx" ] || warn "YOLOX-S isn't installed; the detector uses YOLOX-Nano (fine, just less sharp)."

say "2/5 Backing up what is installed now"
rm -rf "$ROLLBACK" && mkdir -p "$ROLLBACK/helpers"
[ -f "$APP" ] && cp -p "$APP" "$ROLLBACK/catcam.py"
for h in "${HELPERS[@]}"; do [ -f "$SHARE/$h" ] && cp -p "$SHARE/$h" "$ROLLBACK/helpers/$h"; done
[ -d "$SHARE/pages" ] && cp -rp "$SHARE/pages" "$ROLLBACK/pages"
[ -f /usr/local/bin/catcam-guard ] && cp -p /usr/local/bin/catcam-guard "$ROLLBACK/catcam-guard"
[ -f "$RELEASE/version" ] && cp -p "$RELEASE/version" "$ROLLBACK/version"
echo "  saved in $ROLLBACK"

restore() {
  warn "Putting the previous version back."
  [ -f "$ROLLBACK/catcam.py" ] && cp -p "$ROLLBACK/catcam.py" "$APP"
  for h in "${HELPERS[@]}"; do
    if [ -f "$ROLLBACK/helpers/$h" ]; then cp -p "$ROLLBACK/helpers/$h" "$SHARE/$h"; else rm -f "$SHARE/$h"; fi
  done
  rm -rf "$SHARE/pages"; [ -d "$ROLLBACK/pages" ] && cp -rp "$ROLLBACK/pages" "$SHARE/pages"
  [ -f "$ROLLBACK/catcam-guard" ] && sudo install -m 0755 "$ROLLBACK/catcam-guard" /usr/local/bin/catcam-guard
  if [ ! -f "$ROLLBACK/helpers/frame.py" ]; then
    # The old version reads the camera directly; the frame stage must let go of it.
    sudo systemctl disable --now catcam-frame.service 2>/dev/null || true
    sudo rm -f "$UNITS/catcam-camera.service.d/20-frame.conf"
    sudo systemctl daemon-reload
  fi
  sudo systemctl restart catcam-camera.service catcam-recorder.service
  sudo systemctl restart catcam-insights.service catcam-guard.service 2>/dev/null || true
  [ -f "$ROLLBACK/version" ] && cp -p "$ROLLBACK/version" "$RELEASE/version"
}

say "3/5 Installing files"
mkdir -p "$SHARE" "$RELEASE"
for h in "${HELPERS[@]}"; do install -m 0644 "$REPO/app/$h" "$SHARE/$h"; done
rm -rf "$SHARE/pages.new" && cp -r "$REPO/app/pages" "$SHARE/pages.new" && rm -rf "$SHARE/pages" && mv "$SHARE/pages.new" "$SHARE/pages"
install -m 0644 "$REPO/app/catcam.py" "$APP.new" && mv "$APP.new" "$APP"
sudo install -m 0755 "$REPO/guard/catcam_guard.py" /usr/local/bin/catcam-guard
"$VENV_PY" -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" "$APP" "${HELPERS[@]/#/$SHARE/}"

say "4/5 Restarting Cat Cam"
sudo systemctl enable catcam-frame.service >/dev/null 2>&1
sudo systemctl restart catcam-frame.service
sleep 3
sudo systemctl restart catcam-camera.service catcam-recorder.service
sudo systemctl enable catcam-insights.service >/dev/null 2>&1
sudo systemctl restart catcam-insights.service
sudo systemctl restart catcam-guard.service 2>/dev/null || true

say "5/5 Checking"
check_site() {
  python3 - "$VERSION" <<'PY'
import json, sys, urllib.request
want = sys.argv[1]
day = json.load(urllib.request.urlopen("http://127.0.0.1:8086/api/day", timeout=5))
assert day.get("version") == want, day.get("version")
for path in ("/", "/insights/", "/studio/", "/camera/", "/sightings/"):
    assert urllib.request.urlopen("http://127.0.0.1:8086" + path, timeout=8).status == 200, path
json.load(urllib.request.urlopen("http://127.0.0.1:8086/api/camera", timeout=8))
PY
}
check_stream() {
  python3 - <<'PY'
import json, urllib.request
state = json.load(urllib.request.urlopen("http://127.0.0.1:8085/state", timeout=4))
assert state["result"]["source"]["online"]
PY
}
SITE=0; STREAM=0
for _ in $(seq 1 60); do
  [ "$SITE" = 1 ] || { check_site 2>/dev/null && SITE=1; }
  [ "$STREAM" = 1 ] || { check_stream 2>/dev/null && STREAM=1; }
  [ "$SITE" = 1 ] && [ "$STREAM" = 1 ] && break
  sleep 2
done
if [ "$SITE" = 1 ] && [ "$STREAM" = 0 ] && ! v4l2-ctl --list-devices 2>/dev/null | grep -qi "brave 7 le"; then
  warn "The site is fine but the camera isn't connected (or isn't in PCCAM mode), so the picture couldn't be checked."
  STREAM=1
fi
if [ "$SITE" != 1 ] || [ "$STREAM" != 1 ]; then
  [ "$SITE" = 1 ] || warn "The site did not come up with version $VERSION."
  [ "$STREAM" = 1 ] || { warn "The camera stream did not come back."; journalctl -u catcam-frame -n 8 --no-pager -o cat 2>/dev/null | sed 's/^/    /' || true; }
  restore
  die "Cat Cam $VERSION failed its checks and the previous version is back. Send Claude the lines above."
fi
echo "$VERSION" > "$RELEASE/version"
sha "$APP" > "$RELEASE/catcam.sha"
echo "  Site, camera and stream are up."
cat <<EOF

Cat Cam $VERSION is installed.
  Camera settings (draw the windowsill):  https://mina-mint.tail8e9cb1.ts.net:8443/camera/
  Undo this update:                        catcam-update --previous
EOF
