#!/usr/bin/env bash
# Upgrade the family lounge on mina to the live stream (about 10 frames per second).
#   bash ~/catcam-repo/lounge/apply.sh
# Builds a new lounge release from the one installed now plus the files in this folder,
# then runs the lounge's own installer, which checks everything and puts the old
# release back by itself if a check fails. The invitation link and PIN do not change.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HOME/.config/catcam-lounge"
APP="$HOME/catcam-lounge"
SOURCE="$APP/pin-source"
CHANGED=(lounge.py pin_lounge.py pin_access.py gate.js lounge.js pin_setup.py lounge.html lounge.css gate.html gate.css favicon.svg storage.html)

[ "$(id -un)" = faure ] || { echo "Run as faure: bash ~/catcam-repo/lounge/apply.sh"; exit 1; }
CURRENT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$CONFIG/pin-installed.json")"
[ -f "$CURRENT/pin_setup.py" ] || { echo "Could not find the installed lounge release ($CURRENT)."; exit 1; }
echo "Current lounge release: $CURRENT"

rm -rf "$SOURCE"
mkdir -m 700 "$SOURCE"
find "$CURRENT" -maxdepth 1 -type f -exec cp -p {} "$SOURCE"/ \;
for name in "${CHANGED[@]}"; do install -m 600 "$HERE/$name" "$SOURCE/$name"; done
python3 -m py_compile "$SOURCE"/*.py

echo "Installing the lounge update (asks for your mina password once, for sudo)..."
python3 "$SOURCE/pin_setup.py" --install

# Only after the new release passed its checks: raise the lounge rate from 2 to 10 fps,
# and move an AKASO-era crop onto the beds as the Sony sees them.
# (The old release refuses rates above 2, so this must not happen before the install.)
python3 - "$CONFIG/settings.json" "$SOURCE" <<'PY'
import json, os, sys
path = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from lounge import DEFAULT_CROP, OLD_CROPS
data = json.load(open(path))
changed = False
if data.get("fps", 2) <= 2:
    data["fps"] = 10
    changed = True
if data.get("crop") in [list(c) for c in OLD_CROPS]:
    data["crop"] = list(DEFAULT_CROP)
    changed = True
    print("Lounge view moved to the cat beds for the Sony camera.")
if changed:
    temp = path + ".new"
    with open(temp, "w") as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, indent=2)
    os.replace(temp, path)
print(f"Lounge rate: {data['fps']} frames per second")
PY
echo
echo "Done. Reload the lounge page on your phone; it should now move smoothly."
echo "To go back: python3 $CURRENT/pin_setup.py --install"
echo "  (and first set \"fps\" back to 2 in $CONFIG/settings.json)"
