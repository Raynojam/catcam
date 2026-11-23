"""Cat Cam insights for mina: who was there, what they did, and a daily diary.

Runs beside catcam.py as catcam-insights.service. It reads the detector's results
(scan.json), looks at each likely-cat moment with a local vision model through
Ollama, finds where the cat is in the frame, writes a short diary for each day,
and can send Telegram alerts. Everything stays on mina.

  python3 insights.py                  run the service loop
  python3 insights.py --once           one pass, then exit
  python3 insights.py --status         show what it has done
  python3 insights.py --setup-telegram connect a Telegram bot for alerts
  python3 insights.py --test-telegram  send a test message
  python3 insights.py --diary [DAY]    rebuild and print a day's diary
  python3 insights.py --reanalyze      look at recent sightings again
"""

import base64
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOME = Path.home()
CLIPS = HOME / "Videos" / "CatCam" / "clips"
HIGHLIGHT_DATA = HOME / ".local" / "share" / "catcam" / "highlights"
SCAN_STATE = HIGHLIGHT_DATA / "scan.json"
SAVED_SIGHTINGS = HIGHLIGHT_DATA / "saved-sightings.json"
MODELS = [HIGHLIGHT_DATA / "yolox_s.onnx", HIGHLIGHT_DATA / "yolox_nano.onnx"]
DATA_DIR = HOME / ".local" / "share" / "catcam" / "insights"
DATA_FILE = DATA_DIR / "insights.json"
CONFIG_DIR = HOME / ".config" / "catcam"
CONFIG_FILE = CONFIG_DIR / "insights.json"
CATS_FILE = CONFIG_DIR / "cats.json"
GUARD_STATE = Path("/var/lib/catcam-guard/state.json")
MOTION_NAME = re.compile(r"(\d{8})-(\d{6})-\d+\.mp4")

LOOP_SECONDS = 60
PER_LOOP = 8                 # analyses per pass; new sightings always go first
REANALYZE_DAYS = 14          # re-identify this far back when the cat list changes
STATS_DAYS = 7
KEEP_DAYS = 60
TODAY_DIARY_EVERY = 45 * 60  # refresh today's diary at most this often
ACTIVITIES = ["sleeping", "resting", "grooming", "watching", "exploring", "playing",
              "eating", "drinking", "walking by", "other"]

DEFAULT_CONFIG = {
    "model": "gemma3:4b",
    "writer_model": "",          # a bigger text model for the diary and post copy; "" = use "model"
    "ollama_url": "http://127.0.0.1:11434",
    "site_url": "",
    "telegram": {"token": "", "chat_id": ""},
    "arrival_alerts": True,
    "arrival_gap_minutes": 45,
    "quiet_hours": "23-07",
    "morning_diary": True,
    "morning_time": "07:30",
    "telegram_api": "https://api.telegram.org",
}


def log(message):
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


# ---------------------------------------------------------------- files

def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}-", delete=False) as out:
        staging = Path(out.name)
    try:
        staging.write_bytes(data)
        os.chmod(staging, mode)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def load_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    stored = read_json(CONFIG_FILE, {})
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in config and isinstance(config[key], dict) and isinstance(value, dict):
                config[key].update(value)
            elif key in config:
                config[key] = value
    return config


def save_config(config):
    write_atomic(CONFIG_FILE, json.dumps(config, indent=2).encode())


def load_cats():
    """The cats Faure has named on the Diary page: [{"name", "description", "personality"}]."""
    data = read_json(CATS_FILE, {})
    cats = data.get("cats", []) if isinstance(data, dict) else []
    clean = []
    for cat in cats if isinstance(cats, list) else []:
        if isinstance(cat, dict) and isinstance(cat.get("name"), str) and cat["name"].strip():
            clean.append({"name": cat["name"].strip()[:40],
                          "description": str(cat.get("description", "")).strip()[:160],
                          "personality": str(cat.get("personality", "")).strip()[:200]})
    return clean[:8]


def cats_hash(cats):
    return hashlib.sha256(json.dumps(cats, sort_keys=True).encode()).hexdigest()[:12]


def load_data():
    data = read_json(DATA_FILE, {})
    if not isinstance(data, dict) or data.get("version") != 1:
        data = {"version": 1}
    for key in ("sightings", "diaries", "sent"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save_data(data):
    write_atomic(DATA_FILE, json.dumps(data, allow_nan=False).encode(), mode=0o644)


# ---------------------------------------------------------------- sightings

def clip_stamp(name):
    m = MOTION_NAME.fullmatch(name)
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None


def current_sightings():
    """Likely-cat moments the detector found, plus saved ones, whose files still match."""
    scan = read_json(SCAN_STATE, {})
    records = dict(scan.get("clips", {})) if isinstance(scan, dict) and isinstance(scan.get("clips"), dict) else {}
    saved = read_json(SAVED_SIGHTINGS, {})
    if isinstance(saved, dict) and isinstance(saved.get("items"), dict):
        for name, record in saved["items"].items():
            if name not in records or not isinstance(records[name], dict) or not records[name].get("moment"):
                records[name] = record
    found = {}
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get("moment"), dict):
            continue
        stamp = clip_stamp(name)
        if not stamp:
            continue
        try:
            info = (CLIPS / name).lstat()
        except OSError:
            continue
        signature = [info.st_size, info.st_mtime_ns]
        if record.get("signature") != signature:
            continue
        moment = record["moment"]
        at = moment.get("detected_at", moment.get("start", 0) + 2)
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            at = 0
        found[name] = {"stamp": stamp, "signature": signature, "at": float(at),
                       "score": float(moment.get("score") or 0)}
    return found


def run(args, timeout=60):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()[-300:] or f"{args[0]} failed")
    return result.stdout


def grab_frame(name, at, width=1280):
    path = str(CLIPS / name)
    for offset in (at, max(0.0, at - 1.5), 0.0):
        data = run(["ffmpeg", "-v", "error", "-ss", f"{offset:.3f}", "-i", path, "-frames:v", "1",
                    "-vf", f"scale={width}:-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                   timeout=30)
        if data.startswith(b"\xff\xd8"):
            return data
    raise RuntimeError("could not read a frame")


def crop_jpeg(jpeg, box):
    """A tighter view of the cat helps the model tell cats apart."""
    cx, cy, w, h = box
    side = max(160.0, max(w * 1280, h * 720) * 1.5)
    x = max(0, min(1280 - side, cx * 1280 - side / 2))
    y = max(0, min(720 - side, cy * 720 - side / 2))
    side = min(side, 720)
    with tempfile.NamedTemporaryFile(suffix=".jpg") as source:
        source.write(jpeg)
        source.flush()
        return run(["ffmpeg", "-v", "error", "-i", source.name, "-vf",
                    f"scale=1280:720,crop={int(side)}:{int(side)}:{int(x)}:{int(y)},scale=512:512",
                    "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], timeout=30)


def clip_seconds(name):
    try:
        out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(CLIPS / name)], timeout=20)
        value = float(out)
        return round(value, 1) if math.isfinite(value) and value > 0 else None
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return None


class BoxFinder:
    """Where is the cat? Uses the same YOLOX models as the Cat Cam detector."""

    def __init__(self):
        self.session = None
        try:
            import numpy
            import onnxruntime
        except ImportError:
            return
        model = next((m for m in MODELS if m.is_file()), None)
        if not model:
            return
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.np = numpy
        self.session = onnxruntime.InferenceSession(str(model), sess_options=options,
                                                    providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.side = int(self.session.get_inputs()[0].shape[2])
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = self.side // stride
            ys, xs = numpy.meshgrid(numpy.arange(n), numpy.arange(n), indexing="ij")
            grid = numpy.stack((xs, ys), 2).reshape(-1, 2)
            grids.append(grid)
            strides.append(numpy.full((grid.shape[0], 1), stride))
        self.grid = numpy.concatenate(grids)
        self.stride = numpy.concatenate(strides)

    def find(self, name, at):
        """Best cat box as [cx, cy, w, h], fractions of the frame, or None."""
        return self.look(name, at)[0]

    def look(self, name, at):
        """(best cat box or None, highest person score 0-1)."""
        if not self.session:
            return None, 0.0
        side, np = self.side, self.np
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(CLIPS / name), "-frames:v", "1",
                   "-vf", f"scale={side}:{side}:force_original_aspect_ratio=decrease,"
                          f"pad={side}:{side}:0:0:color=0x727272",
                   "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"], timeout=30)
        if len(raw) != side * side * 3:
            return None, 0.0
        frame = np.frombuffer(raw, np.uint8).reshape(side, side, 3)
        pred = self.session.run(None, {self.input: frame.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
        classes = pred[:, 5:].argmax(axis=1)
        people = pred[:, 4] * pred[:, 5]
        person = float(people[classes == 0].max()) if (classes == 0).any() else 0.0
        cats = classes == 15
        if not cats.any():
            return None, round(person, 3)
        scores = pred[:, 4] * pred[:, 5 + 15]
        scores[~cats] = 0
        k = int(scores.argmax())
        if scores[k] < 0.2:
            return None, round(person, 3)
        cx, cy = (pred[k, :2] + self.grid[k]) * self.stride[k]
        w, h = np.exp(pred[k, 2:4]) * self.stride[k]
        scale = side / 1280.0          # frames are 1280x720, padded at the bottom
        box = [cx / scale / 1280, cy / scale / 720, w / scale / 1280, h / scale / 720]
        return [round(min(1.0, max(0.0, float(v))), 4) for v in box], round(person, 3)


def motion_energy(name, at):
    """How much is moving around the moment: mean frame-to-frame change, 0 (still) to ~0.2 (zoomies)."""
    try:
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at - 2):.2f}", "-t", "8", "-i", str(CLIPS / name),
                   "-vf", "fps=4,scale=64:36,format=gray", "-f", "rawvideo", "pipe:1"], timeout=40)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    size = 64 * 36
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    if len(frames) < 2:
        return None
    diffs = [sum(abs(a - b) for a, b in zip(f1, f2)) / size / 255 for f1, f2 in zip(frames, frames[1:])]
    return round(sum(diffs) / len(diffs), 4)


# ---------------------------------------------------------------- Ollama

class Model:
    def __init__(self, config, name=None):
        self.url = config["ollama_url"].rstrip("/")
        self.name = name or config["model"]
        self.state = "unknown"

    def ready(self):
        try:
            with urlopen(self.url + "/api/tags", timeout=5) as response:
                tags = json.load(response)
        except (OSError, ValueError) as error:
            self.state = f"Ollama is not answering ({error.__class__.__name__})"
            return False
        names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
        if self.name not in names and self.name + ":latest" not in names:
            self.state = f"model {self.name} is not downloaded yet"
            return False
        self.state = "ready"
        return True

    def ask(self, prompt, images=(), schema=None, timeout=240):
        message = {"role": "user", "content": prompt}
        if images:
            message["images"] = [base64.b64encode(i).decode() for i in images]
        body = {"model": self.name, "messages": [message], "stream": False, "keep_alive": "10m",
                "options": {"temperature": 0.3, "num_predict": 500}}
        if schema:
            # Plain JSON mode: schema-constrained decoding makes gemma3 loop on some frames.
            body["format"] = "json"
        request = Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                reply = json.load(response)
        except HTTPError as error:
            detail = error.read()[:200].decode(errors="replace")
            raise ValueError(f"Ollama error {error.code}: {detail}") from None
        content = reply.get("message", {}).get("content", "")
        if not schema:
            return content
        start, end = content.find("{"), content.rfind("}")
        answer = json.loads(content[start:end + 1] if start >= 0 and end > start else content)
        if not isinstance(answer, dict):
            raise ValueError("the model did not answer with a JSON object")
        return answer


SIGHTING_SCHEMA = {
    "type": "object",
    "properties": {
        "is_cat": {"type": "boolean"},
        "cat_count": {"type": "integer"},
        "cats": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "appearance": {"type": "string"}},
            "required": ["name", "appearance"]}},
        "activity": {"type": "string", "enum": ACTIVITIES},
        "spot": {"type": "string"},
        "caption": {"type": "string"},
        "photo_quality": {"type": "integer"},
    },
    "required": ["is_cat", "cat_count", "cats", "activity", "spot", "caption", "photo_quality"],
}


def sighting_prompt(cats, stamp, cropped):
    if cats:
        known = "The household's cats:\n" + "\n".join(
            f"- {c['name']}: {c['description'] or 'no description yet'}" for c in cats)
        known += ("\nTell them apart by coat colour and pattern first, then body shape."
                  "\nIf you are not sure which cat it is, use \"unknown\" rather than guessing.")
        naming = ("For each cat you can see, set name to the matching name from that list, "
                  "or \"unknown\" if it does not clearly match one.")
    else:
        known = "The household's cats have not been named yet."
        naming = "Set every cat's name to \"unknown\"."
    images = ("The first image is the full camera view; the second is a close-up of the detected cat."
              if cropped else "The image is the full camera view.")
    return f"""You are looking at a frame from an indoor home camera watching a window, at {stamp:%I:%M %p}.
{images}
{known}

Answer about this frame only. First decide: can you actually see a cat? Many frames have no cat at all,
because motion is often caused by sunlight, curtains, people or other things. A cat-shaped cushion, a toy,
a pillow, a person or a shadow is not a cat. Only answer is_cat true if a real cat is visible.
{naming}
appearance: a short coat description (e.g. "black cat with white paws", "orange tabby").
activity: one of {", ".join(ACTIVITIES)}.
spot: where the cat is, 1-3 words, e.g. "windowsill", "cat bed", "bookshelf", "floor", "blanket".
caption: one specific, funny observation about what this cat is doing (max 16 words), present tense,
in the voice of a wry nature-documentary narrator; use the cat's name if known. Describe what you
actually see. Avoid the words peacefully, cozy, comfy, feline, furry, slumber.
photo_quality: 1-5, how nice this frame would be as a photo of the cat.
If there is no cat: is_cat false, cat_count 0, cats [], activity "other", spot "", caption "".

Reply with only this JSON object, all keys at the top level:
{{"is_cat": <true or false>, "cat_count": <number>, "cats": [{{"name": "...", "appearance": "..."}}], "activity": "...", "spot": "...", "caption": "...", "photo_quality": <1-5>}}"""


def analyze(model, finder, name, item, cats):
    box, person = finder.look(name, item["at"])
    frame = grab_frame(name, item["at"])
    images = [frame]
    if box and box[2] * box[3] < 0.35:
        try:
            images.append(crop_jpeg(frame, box))
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
    try:
        answer = model.ask(sighting_prompt(cats, item["stamp"], len(images) > 1), images, SIGHTING_SCHEMA)
    except (HTTPError, ValueError):
        images = images[:1]   # one retry with just the full frame
        answer = model.ask(sighting_prompt(cats, item["stamp"], False), images, SIGHTING_SCHEMA)
    first = answer.get("cats")[0] if isinstance(answer.get("cats"), list) and answer["cats"] \
        and isinstance(answer["cats"][0], dict) else {}
    for key in ("activity", "spot", "caption", "photo_quality"):
        if key not in answer and key in first:
            answer[key] = first[key]
    known = {c["name"].lower(): c["name"] for c in cats}
    found = []
    for cat in answer.get("cats", [])[:4] if isinstance(answer.get("cats"), list) else []:
        if not isinstance(cat, dict):
            continue
        label = known.get(str(cat.get("name", "")).strip().lower())
        found.append({"name": label, "appearance": str(cat.get("appearance", "")).strip()[:60] or "a cat"})
    is_cat = bool(answer.get("is_cat")) and (bool(found) or int(answer.get("cat_count") or 0) > 0)
    activity = answer.get("activity") if answer.get("activity") in ACTIVITIES else "other"
    quality = answer.get("photo_quality")
    return {
        "signature": item["signature"], "timestamp": item["stamp"].isoformat(timespec="seconds"),
        "analyzed_at": time.time(), "model": model.name, "cats_hash": cats_hash(cats),
        "is_cat": is_cat, "cats": found if is_cat else [],
        "activity": activity if is_cat else "other",
        "spot": str(answer.get("spot", "")).strip().lower()[:30] if is_cat else "",
        "caption": str(answer.get("caption", "")).strip()[:200] if is_cat else "",
        "quality": int(quality) if isinstance(quality, (int, float)) and 1 <= quality <= 5 else 3,
        "box": box if is_cat else None, "score": item["score"], "at": item["at"],
        "seconds": clip_seconds(name), "person": person, "motion": motion_energy(name, item["at"]),
    }


# ---------------------------------------------------------------- facts, stats, diary

def label(record):
    names = [c["name"] or c["appearance"] for c in record.get("cats", [])]
    return " & ".join(names) if names else "a cat"


def verified(data, day=None, since=None):
    out = []
    for name, record in data["sightings"].items():
        if not record.get("is_cat"):
            continue
        stamp = datetime.fromisoformat(record["timestamp"])
        if day and stamp.date().isoformat() != day:
            continue
        if since and stamp < since:
            continue
        out.append((stamp, name, record))
    return sorted(out)


def day_facts(data, day):
    items = verified(data, day=day)
    false_alarms = sum(1 for r in data["sightings"].values()
                       if not r.get("is_cat") and r["timestamp"][:10] == day)
    per_cat = defaultdict(lambda: {"count": 0, "seconds": 0.0, "first": None, "last": None,
                                   "activities": Counter(), "spots": Counter()})
    hours = Counter()
    for stamp, name, record in items:
        hours[stamp.hour] += 1
        for cat in record["cats"] or [{"name": None, "appearance": "a cat"}]:
            who = cat["name"] or cat["appearance"]
            entry = per_cat[who]
            entry["count"] += 1
            entry["seconds"] += record.get("seconds") or 0
            entry["first"] = entry["first"] or stamp.strftime("%I:%M %p").lstrip("0")
            entry["last"] = stamp.strftime("%I:%M %p").lstrip("0")
            entry["activities"][record["activity"]] += 1
            if record.get("spot"):
                entry["spots"][record["spot"]] += 1
    last_week = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    return {
        "day": day, "weekday": date.fromisoformat(day).strftime("%A"),
        "sightings": len(items), "false_alarms": false_alarms,
        "same_day_last_week": len(verified(data, day=last_week)),
        "busiest_hour": hours.most_common(1)[0][0] if hours else None,
        "cats": {who: {"count": v["count"], "minutes_on_camera": round(v["seconds"] / 60, 1),
                       "first_seen": v["first"], "last_seen": v["last"],
                       "activities": dict(v["activities"].most_common(4)),
                       "spots": dict(v["spots"].most_common(3))} for who, v in per_cat.items()},
        "moments": [{"time": s.strftime("%I:%M %p").lstrip("0"), "who": label(r),
                     "activity": r["activity"], "spot": r.get("spot", ""), "caption": r.get("caption", "")}
                    for s, _, r in items][-40:],
    }


DIARY_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "story": {"type": "string"}},
                "required": ["title", "story"]}


DIARY_VERSION = 2
BANNED = ["furry friend", "feline", "purr-fect", "purrfect", "interestingly", "cozy", "cosy", "comfy",
          "peaceful", "in conclusion", "overall,", "sightings", "camera", "recorded", "clips"]


INVENTED = ["pigeon", "pigeons", "bird", "birds", "squirrel", "squirrels", "mouse", "mice", "dog", "dogs",
            "treat", "treats", "food", "dinner", "breakfast", "toy", "toys", "string", "laser", "human", "owner",
            "minutes", "hours", "hour", "seconds"]


def clock(stamp):
    return stamp.strftime("%I:%M %p").lstrip("0")


def roster_lines(cats):
    lines = []
    for c in cats:
        line = f"- {c['name']}: {c['description'] or 'no description yet'}"
        if c.get("personality"):
            line += f". Personality: {c['personality']}"
        lines.append(line)
    return "\n".join(lines) or "- The cats have not been named yet; call them by their coats."


def story_brief(data, day, cats, recorded):
    """Turn a day of labelled moments into a short brief a small model can write well from."""
    items = verified(data, day=day)
    runs = []
    for stamp, _, record in items:
        key = (label(record), record["activity"], record.get("spot", ""))
        if runs and runs[-1]["key"] == key and stamp - runs[-1]["end"] <= timedelta(minutes=25):
            runs[-1]["end"] = stamp
            runs[-1]["count"] += 1
        else:
            runs.append({"key": key, "start": stamp, "end": stamp, "count": 1, "note": record.get("caption", "")})
    timeline = []
    for run in runs[-24:]:
        who, activity, spot = run["key"]
        when = clock(run["start"]) if run["count"] == 1 else f"{clock(run['start'])} to {clock(run['end'])}"
        line = f"- {when}: {who} {activity}" + (f" on the {spot}" if spot else "")
        if run["note"]:
            line += f' (note: "{run["note"]}")'
        timeline.append(line)

    highlights = [f"First cat of the day: {label(items[0][2])} at {clock(items[0][0])}."]
    for stamp, _, record in [i for i in items if len(i[2]["cats"]) >= 2][:2]:
        highlights.append(f"Together: {label(record)} at {clock(stamp)}, {record['activity']}"
                          + (f" on the {record['spot']}" if record.get("spot") else "") + ".")
    night = [i for i in items if i[0].hour < 5]
    if night:
        highlights.append(f"Night shift: {label(night[0][2])} was up at {clock(night[0][0])}.")
    for stamp, _, record in [i for i in items if i[2]["activity"] in ("playing", "eating", "drinking", "exploring")][:2]:
        highlights.append(f"{label(record)} was {record['activity']} at {clock(stamp)}.")
    longest = max(items, key=lambda i: i[2].get("seconds") or 0)
    if (longest[2].get("seconds") or 0) >= 60:
        highlights.append(f"Longest stay: {label(longest[2])}, {longest[2]['activity']}, around {clock(longest[0])}.")
    if len(items) > 1:
        highlights.append(f"Last cat seen: {label(items[-1][2])} at {clock(items[-1][0])}.")

    seen, activities, spots = Counter(), defaultdict(Counter), defaultdict(Counter)
    for _, _, record in items:
        for cat in record["cats"] or [{"name": None, "appearance": "an unnamed cat"}]:
            who = cat["name"] or cat["appearance"]
            seen[who] += 1
            activities[who][record["activity"]] += 1
            if record.get("spot"):
                spots[who][record["spot"]] += 1
    cast = [f"- {who}: around {n} time{'s' if n != 1 else ''}, mostly {activities[who].most_common(1)[0][0]}"
            + (f", favourite spot the {spots[who].most_common(1)[0][0]}" if spots[who] else "")
            for who, n in seen.most_common()]
    missing = [c["name"] for c in cats if c["name"] not in seen]

    weekday = date.fromisoformat(day).strftime("%A")
    last_week = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    compare = ""
    if last_week in recorded:
        before, now_count = len(verified(data, day=last_week)), len(items)
        if before and abs(now_count - before) >= max(3, before // 2):
            compare = (f"Compared with last {weekday}: a much {'busier' if now_count > before else 'quieter'} day "
                       "at the window.")
    return {"day": day, "weekday": weekday, "roster": roster_lines(cats), "highlights": highlights,
            "cast": cast, "missing": missing, "timeline": timeline, "compare": compare}


def write_diary(model, brief):
    nice_date = date.fromisoformat(brief["day"]).strftime("%A, %B %d").replace(" 0", " ")
    prompt = f"""You write "The Window", a daily diary about some house cats, read by their human who adores
them and may share it online.

The cats:
{brief['roster']}

What happened on {nice_date}:
Highlights:
{chr(10).join('- ' + h for h in brief['highlights'])}
Who showed up:
{chr(10).join(brief['cast'])}
{('Not seen today: ' + ', '.join(brief['missing'])) if brief['missing'] else ''}
{brief['compare']}
Timeline (rough automatic notes; they can be bland or slightly wrong):
{chr(10).join(brief['timeline'])}

Write the entry:
- Voice: a wry nature-documentary narrator with a soft spot for these particular cats. Affectionate,
  specific, gently dramatic about small things. Keep each cat's personality consistent.
- 4 or 5 sentences, at most 100 words, past tense. Build it around the two or three most interesting
  moments; do not list everything.
- At most three clock times. No counts or statistics. Never mention cameras, clips, recordings,
  sightings or AI.
- Use only the events above. Never invent cats, places, sounds, food, people, birds, mice or visitors,
  and never invent how long something lasted.
- Do not use: furry friends, feline, purr-fect, interestingly, cozy, comfy, peacefully, in conclusion.
- End on a short, punchy line.
- Title: 3 to 7 words, witty, like a TV episode title. No colon, no quotes.

Example of the voice only (different cats; do not reuse its events or phrases):
{{"title": "The Great Sill Standoff", "story": "Biscuit claimed the windowsill at dawn and held it with the stillness of a museum exhibit. Pepper arrived mid-morning, studied the situation, and took the cat bed instead, as if that had been the plan all along. By afternoon both were asleep a metre apart, a ceasefire neither will admit to. The sill, for the record, remains contested."}}

Reply with only JSON: {{"title": "...", "story": "..."}}"""
    answer = model.ask(prompt, schema=DIARY_SCHEMA, timeout=420)
    draft = {"title": str(answer.get("title", "")).strip().strip('"').rstrip(".")[:80] or "A day at the window",
             "story": str(answer.get("story", "")).strip()[:1500]}
    # Only send it to an editing pass when it needs one: editing also flattens the voice.
    facts = "\n".join(brief["highlights"] + brief["cast"] + brief["timeline"])
    words = re.findall(r"[a-z']+", draft["story"].lower())
    invented = [w for w in INVENTED if w in words and w not in facts.lower()]
    banned = [w for w in BANNED if w in draft["story"].lower()]
    if len(words) <= 125 and not invented and not banned:
        return draft
    edit = f"""You are the editor of a cat diary. Here are the only true facts about the day:
{facts}

Draft entry:
{json.dumps(draft)}

Edit the draft:
1. Delete anything not supported by the facts: invented animals, birds, food, sounds, durations, people or places.
2. Cut it to at most 100 words and at most 5 sentences. Keep the best jokes and the voice.
3. Remove these words: {", ".join(BANNED[:9])}.
4. Keep cat names and pronouns consistent. Keep the title unless it is untrue.
Reply with only JSON: {{"title": "...", "story": "..."}}"""
    try:
        answer = model.ask(edit, schema=DIARY_SCHEMA, timeout=420)
        edited = {"title": str(answer.get("title", "")).strip().strip('"').rstrip(".")[:80] or draft["title"],
                  "story": str(answer.get("story", "")).strip()[:1200]}
        if 15 <= len(edited["story"].split()) <= 160:
            return edited
    except (OSError, ValueError):
        pass
    return {"title": draft["title"], "story": draft["story"][:1200]}


def best_moment(data, day):
    items = verified(data, day=day)
    if not items:
        return None
    return max(items, key=lambda i: (i[2].get("quality", 3), i[2].get("score", 0)))[1]


def note_recorded_days(data):
    days = set(data.get("recorded_days", []))
    for clip in CLIPS.glob("*.mp4"):
        if MOTION_NAME.fullmatch(clip.name):
            days.add(f"{clip.name[:4]}-{clip.name[4:6]}-{clip.name[6:8]}")
    data["recorded_days"] = sorted(days)[-120:]


def update_diaries(model, data, now, budget=2, writer=None):
    note_recorded_days(data)
    recorded = set(data["recorded_days"])
    for back in range(0, 8):
        if budget <= 0:
            return
        day = (now.date() - timedelta(days=back)).isoformat()
        facts = day_facts(data, day)
        if not facts["sightings"] and back and day not in recorded:
            data["diaries"].pop(day, None)   # the camera wasn't recording that day
            continue
        cats = load_cats()
        digest = hashlib.sha256(json.dumps([DIARY_VERSION, facts, cats], sort_keys=True).encode()).hexdigest()[:16]
        current = data["diaries"].get(day, {})
        if current.get("digest") == digest:
            continue
        if back == 0 and current and now.timestamp() - current.get("generated_at", 0) < TODAY_DIARY_EVERY:
            continue
        if not facts["sightings"]:
            entry = {"title": "A quiet day at the window" if back else "Waiting for the first visitor",
                     "story": ("No cats were spotted on camera this day." if back else
                               "No cats have been spotted on camera yet today. The window is ready when they are."),
                     "generated": "template"}
        else:
            author = writer if writer and writer.ready() else model
            entry = write_diary(author, story_brief(data, day, cats, recorded))
            entry["generated"] = author.name
            budget -= 1
        entry.update({"digest": digest, "generated_at": now.timestamp(), "best": best_moment(data, day),
                      "facts": {k: facts[k] for k in ("sightings", "false_alarms", "same_day_last_week",
                                                      "busiest_hour", "cats")}})
        data["diaries"][day] = entry
        log(f"diary for {day}: {entry['title']}")


def build_stats(data, now):
    since = now - timedelta(days=STATS_DAYS)
    items = verified(data, since=since)
    board = defaultdict(lambda: {"sightings": 0, "seconds": 0.0, "spots": Counter(), "activities": Counter(),
                                 "hours": Counter(), "last_seen": None, "best": None, "best_q": -1, "named": False})
    heat, spots, activities, appearances = [], Counter(), Counter(), defaultdict(lambda: [0, None])
    days_with = set()
    for stamp, name, record in items:
        days_with.add(stamp.date())
        spots.update([record["spot"]] if record.get("spot") else [])
        activities[record["activity"]] += 1
        cats = record["cats"] or [{"name": None, "appearance": "a cat"}]
        for cat in cats:
            who = cat["name"] or "Unnamed"
            entry = board[who]
            entry["named"] = bool(cat["name"])
            entry["sightings"] += 1
            entry["seconds"] += (record.get("seconds") or 0) / len(cats)
            if record.get("spot"):
                entry["spots"][record["spot"]] += 1
            entry["activities"][record["activity"]] += 1
            entry["hours"][stamp.hour] += 1
            entry["last_seen"] = record["timestamp"]
            if record.get("quality", 3) >= entry["best_q"]:
                entry["best"], entry["best_q"] = name, record.get("quality", 3)
            if not cat["name"]:
                group = appearances[cat["appearance"].lower()]
                group[0] += 1
                group[1] = name
            if record.get("box"):
                heat.append(record["box"] + [cat["name"] or "Unnamed"])
    leaderboard = sorted(({
        "name": who, "named": v["named"], "sightings": v["sightings"], "minutes": round(v["seconds"] / 60, 1),
        "favorite_spot": v["spots"].most_common(1)[0][0] if v["spots"] else None,
        "favorite_activity": v["activities"].most_common(1)[0][0] if v["activities"] else None,
        "favorite_hour": v["hours"].most_common(1)[0][0] if v["hours"] else None,
        "last_seen": v["last_seen"], "best": v["best"]} for who, v in board.items()),
        key=lambda e: (e["minutes"], e["sightings"]), reverse=True)
    streak, day = 0, now.date()
    seen_days = {datetime.fromisoformat(r["timestamp"]).date() for r in data["sightings"].values() if r.get("is_cat")}
    if day not in seen_days:
        day -= timedelta(days=1)
    while day in seen_days:
        streak += 1
        day -= timedelta(days=1)
    previous = len(verified(data, since=since - timedelta(days=STATS_DAYS))) - len(items)
    return {
        "days": STATS_DAYS, "sightings": len(items), "previous_period": previous, "streak_days": streak,
        "leaderboard": leaderboard, "heat": heat[-500:],
        "spots": spots.most_common(8), "activities": activities.most_common(),
        "unnamed": sorted(({"appearance": a, "sightings": c, "example": ex} for a, (c, ex) in appearances.items()),
                          key=lambda g: g["sightings"], reverse=True)[:8],
    }


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, config):
        self.token = config["telegram"].get("token", "")
        self.chat = str(config["telegram"].get("chat_id", ""))
        self.api = config.get("telegram_api", DEFAULT_CONFIG["telegram_api"]).rstrip("/")

    @property
    def ready(self):
        return bool(self.token and self.chat)

    def call(self, method, fields=None, photo=None, timeout=30):
        url = f"{self.api}/bot{self.token}/{method}"
        if photo is None:
            request = Request(url, data=json.dumps(fields or {}).encode(),
                              headers={"Content-Type": "application/json"})
        else:
            boundary = uuid.uuid4().hex
            parts = []
            for key, value in (fields or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="cat.jpg"\r\n'
                         f'Content-Type: image/jpeg\r\n\r\n'.encode() + photo + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            request = Request(url, data=b"".join(parts),
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("description", "Telegram refused the message"))
        return reply.get("result")

    def send(self, text, photo=None):
        if photo:
            if len(text) <= 1000:
                return self.call("sendPhoto", {"chat_id": self.chat, "caption": text}, photo=photo)
            self.call("sendPhoto", {"chat_id": self.chat}, photo=photo)
        return self.call("sendMessage", {"chat_id": self.chat, "text": text[:4000],
                                         "disable_web_page_preview": True})


def in_quiet_hours(config, when):
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", str(config.get("quiet_hours", "")).strip())
    if not match:
        return False
    start, end = int(match.group(1)) % 24, int(match.group(2)) % 24
    hour = when.hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def link(config, name=None):
    base = str(config.get("site_url", "")).rstrip("/")
    if not base:
        return ""
    if not name:
        return "\n" + base + "/insights/"
    return "\n" + base + "/sightings/?day=" + name[:4] + "-" + name[4:6] + "-" + name[6:8] + "&clip=" + name


def maybe_alert(config, telegram, data, fresh, now):
    if not (telegram.ready and config.get("arrival_alerts")):
        return
    gap = timedelta(minutes=float(config.get("arrival_gap_minutes", 45)))
    for name in sorted(fresh):
        record = data["sightings"][name]
        stamp = datetime.fromisoformat(record["timestamp"])
        if not record.get("is_cat") or now - stamp > timedelta(minutes=30) or name in data["sent"]:
            continue
        earlier = [s for s, n, r in verified(data, since=stamp - gap) if s < stamp and n != name]
        if earlier or in_quiet_hours(config, stamp):
            continue
        who = label(record)
        where = f" on the {record['spot']}" if record.get("spot") else ""
        text = f"🐾 {who} at the window{where}, {stamp.strftime('%I:%M %p').lstrip('0')}"
        if record.get("caption"):
            text += f"\n{record['caption']}"
        try:
            telegram.send(text + link(config, name), grab_frame(name, record["at"]))
            data["sent"][name] = now.timestamp()
            log(f"arrival alert sent for {name}")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            log(f"arrival alert failed: {error}")


def maybe_morning(config, telegram, data, now):
    if not (telegram.ready and config.get("morning_diary")):
        return
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(config.get("morning_time", "07:30")))
    if not match or (now.hour, now.minute) < (int(match.group(1)), int(match.group(2))):
        return
    day = (now.date() - timedelta(days=1)).isoformat()
    key = "diary-" + day
    entry = data["diaries"].get(day)
    if key in data["sent"] or not entry:
        return
    text = f"📔 {entry['title']}\n\n{entry['story']}"
    facts = entry.get("facts", {})
    if facts.get("sightings"):
        text += f"\n\n{facts['sightings']} cat sightings yesterday"
        if facts.get("same_day_last_week") is not None:
            text += f" ({facts['same_day_last_week']} the week before)"
    photo = None
    best = entry.get("best")
    if best and best in data["sightings"]:
        try:
            photo = grab_frame(best, data["sightings"][best]["at"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            photo = None
    try:
        telegram.send(text + link(config), photo)
        data["sent"][key] = now.timestamp()
        log(f"morning diary sent for {day}")
    except (OSError, RuntimeError) as error:
        log(f"morning diary failed: {error}")


# ---------------------------------------------------------------- health

def guard_status():
    state = read_json(GUARD_STATE, None)
    if not isinstance(state, dict):
        return {"installed": GUARD_STATE.parent.exists(), "events": []}
    events = state.get("events", [])
    return {"installed": True, "events": events[-5:] if isinstance(events, list) else []}


# ---------------------------------------------------------------- main loop

def one_pass(finder, data, config, ident=None):
    now = datetime.now()
    cats = load_cats()
    chash = cats_hash(cats)
    model = Model(config)
    telegram = Telegram(config)
    sightings = current_sightings()
    cutoff = now - timedelta(days=REANALYZE_DAYS)
    todo_new = [n for n in sightings if n not in data["sightings"]
                or data["sightings"][n].get("signature") != sightings[n]["signature"]]
    todo_old = [n for n in sightings if n not in todo_new
                and (data["sightings"][n].get("cats_hash") != chash or data["sightings"][n].get("model") != model.name)
                and sightings[n]["stamp"] >= cutoff]
    queue = sorted(todo_new, reverse=True) + sorted(todo_old, reverse=True)
    fresh = []
    if queue and model.ready():
        for name in queue[:PER_LOOP]:
            try:
                data["sightings"][name] = analyze(model, finder, name, sightings[name], cats)
                fresh.append(name)
                r = data["sightings"][name]
                log(f"{name}: {'cat' if r['is_cat'] else 'not a cat'} · {label(r)} · {r['activity']} · {r['caption']}")
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, HTTPError) as error:
                log(f"could not analyze {name}: {error}")
                model.state = f"last analysis failed: {str(error)[:120]}"
                break
    elif queue:
        log(f"{len(queue)} sightings waiting: {model.state}")
    if ident is not None and ident.ready:
        try:
            for name in sorted(data["sightings"], reverse=True):
                ident.add(name, data["sightings"][name])
            day_key = now.date().isoformat() + "|" + chash
            reseed = data.get("identity_seed") != day_key
            changed = ident.relabel(data["sightings"], cats, reseed=reseed)
            data["identity_seed"] = day_key
            data["identity_unsure"] = ident.unsure(data["sightings"])
            if changed:
                log(f"identity: relabelled {len(changed)} sightings")
        except Exception as error:  # identity is a bonus; never stop the diary
            log(f"identity pass failed: {error.__class__.__name__}: {error}")
    keep_after = (now - timedelta(days=KEEP_DAYS)).isoformat()
    for name in list(data["sightings"]):
        if data["sightings"][name].get("timestamp", "") < keep_after:
            del data["sightings"][name]
    for day in list(data["diaries"]):
        if day < keep_after[:10]:
            del data["diaries"][day]
    if model.state == "ready" or model.ready():
        try:
            writer = Model(config, config.get("writer_model")) if config.get("writer_model") else None
            update_diaries(model, data, now, writer=writer)
        except (OSError, RuntimeError, ValueError, HTTPError) as error:
            log(f"diary failed: {error}")
    data["stats"] = build_stats(data, now)
    try:
        import studio
        author = model if model.state == "ready" else None
        if author and config.get("writer_model"):
            writer = Model(config, config.get("writer_model"))
            author = writer if writer.ready() else author
        studio.tick(data["sightings"], author, cats)
    except Exception as error:  # the studio must never stop the diary
        log(f"studio pass failed: {error.__class__.__name__}: {error}")
    maybe_alert(config, telegram, data, fresh, now)
    maybe_morning(config, telegram, data, now)
    data["sent"] = {k: v for k, v in data["sent"].items() if now.timestamp() - v < 40 * 86400}
    data["status"] = {
        "updated_at": time.time(), "model": model.name, "model_state": model.state,
        "waiting": max(0, len(queue) - len(fresh)), "analyzed": len(data["sightings"]),
        "telegram": telegram.ready, "arrival_alerts": bool(config.get("arrival_alerts")),
        "morning_diary": bool(config.get("morning_diary")), "morning_time": config.get("morning_time"),
        "boxes": bool(finder.session), "cats_named": len(cats), "guard": guard_status(),
        "identity": bool(ident is not None and ident.ready),
    }
    save_data(data)
    return len(queue) - len(fresh)


def serve():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    finder = BoxFinder()
    try:
        import identity
        ident = identity.Identity()
    except Exception as error:
        log(f"photo identity unavailable: {error}")
        ident = None
    log(f"Cat Cam insights started · model {load_config()['model']} · "
        f"cat boxes {'on' if finder.session else 'off'} · "
        f"photo identity {'on' if ident is not None and ident.ready else 'off'}")
    while True:
        try:
            waiting = one_pass(finder, load_data(), load_config(), ident)
        except Exception as error:  # keep the service alive; the next pass retries
            log(f"pass failed: {error.__class__.__name__}: {error}")
            waiting = 0
        time.sleep(10 if waiting > 0 else LOOP_SECONDS)


# ---------------------------------------------------------------- command line

def setup_telegram():
    config = load_config()
    print("Telegram setup for Cat Cam alerts.\n"
          "1. In Telegram, open @BotFather, send /newbot, and copy the token it gives you\n"
          "   (or reuse an existing bot's token).")
    import getpass
    token = getpass.getpass("Paste the bot token (input is hidden): ").strip()
    trial = Telegram({"telegram": {"token": token, "chat_id": ""}, "telegram_api": config["telegram_api"]})
    try:
        bot = trial.call("getMe")
    except (OSError, RuntimeError, HTTPError) as error:
        raise SystemExit(f"That token didn't work: {error}")
    print(f"2. Now open Telegram, find @{bot.get('username')}, and send it any message. Waiting up to 2 minutes…")
    chat, deadline, offset = None, time.time() + 120, None
    while not chat and time.time() < deadline:
        fields = {"timeout": 20}
        if offset:
            fields["offset"] = offset
        for update in trial.call("getUpdates", fields, timeout=30) or []:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("channel_post") or {}
            if message.get("chat", {}).get("id"):
                chat = message["chat"]["id"]
    if not chat:
        raise SystemExit("No message arrived. Run --setup-telegram again and message the bot while it waits.")
    config["telegram"] = {"token": token, "chat_id": str(chat)}
    save_config(config)
    Telegram(config).send("🐾 Cat Cam is connected. You'll get arrival alerts and a morning cat diary here.")
    print("Connected. A test message is on its way. Settings live in", CONFIG_FILE)


def main():
    args = sys.argv[1:]
    os.umask(0o077)
    if not args:
        serve()
    elif args == ["--once"]:
        try:
            import identity
            ident = identity.Identity()
        except Exception:
            ident = None
        one_pass(BoxFinder(), load_data(), load_config(), ident)
    elif args == ["--status"]:
        data = load_data()
        print(json.dumps(data.get("status", {}), indent=2))
        for day in sorted(data["diaries"], reverse=True)[:3]:
            print(f"\n{day}: {data['diaries'][day]['title']}\n  {data['diaries'][day]['story']}")
    elif args == ["--setup-telegram"]:
        setup_telegram()
    elif args == ["--test-telegram"]:
        telegram = Telegram(load_config())
        if not telegram.ready:
            raise SystemExit("Telegram isn't set up yet. Run with --setup-telegram first.")
        telegram.send("🐾 Test from Cat Cam on mina.")
        print("Sent.")
    elif args and args[0] == "--diary":
        day = args[1] if len(args) > 1 else date.today().isoformat()
        data = load_data()
        data["diaries"].pop(day, None)
        model = Model(load_config())
        if not model.ready():
            raise SystemExit(model.state)
        config = load_config()
        writer = Model(config, config.get("writer_model")) if config.get("writer_model") else None
        update_diaries(model, data, datetime.combine(date.fromisoformat(day), datetime.now().time())
                       if day != date.today().isoformat() else datetime.now(), budget=1, writer=writer)
        save_data(data)
        entry = data["diaries"].get(day, {})
        print(f"{entry.get('title')}\n\n{entry.get('story')}")
    elif args == ["--reanalyze"]:
        data = load_data()
        for record in data["sightings"].values():
            record["cats_hash"] = "redo"
        save_data(data)
        print("Recent sightings will be looked at again over the next few minutes.")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
