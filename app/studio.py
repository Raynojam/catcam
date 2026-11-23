"""Cat Cam Studio: turns the best cat moments into vertical videos ready to post.

Called from insights.py on every pass. It makes:
  - "moment"  one sighting as a 9:16 short (up to 12 s)
  - "recap"   the day's best 3-5 moments (about 20-25 s), automatically each evening
  - "week"    the week's best moments, one per cat first (about 30-40 s), on Sunday evenings
Each draft gets a burned-in hook line, captions, and ready-to-paste post text.
Requests from the Studio page arrive as small JSON files in studio/requests/.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
CLIPS = HOME / "Videos" / "CatCam" / "clips"
STUDIO = HOME / "Videos" / "CatCam" / "studio"
DRAFTS = STUDIO / "drafts.json"
REQUESTS = STUDIO / "requests"
SETTINGS = HOME / ".config" / "catcam" / "studio.json"
DRAFT_ID = re.compile(r"[0-9a-f]{16}")
KEEP_DAYS = 30
MAX_DRAFTS = 80
W, H = 1080, 1920
DEFAULT_SETTINGS = {"handle": "", "auto_daily": True, "auto_weekly": True, "daily_time": "20:30"}
RARE = {"playing": 4, "eating": 2, "drinking": 2, "exploring": 2, "grooming": 1}


def log(message):
    print(f"{datetime.now():%H:%M:%S} studio: {message}", flush=True)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}-", delete=False) as out:
        out.write(data)
        staging = Path(out.name)
    os.chmod(staging, 0o644)
    staging.replace(path)


def settings():
    stored = read_json(SETTINGS, {})
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    return merged


def load_drafts():
    data = read_json(DRAFTS, {})
    if not isinstance(data, dict) or not isinstance(data.get("drafts"), dict):
        data = {"version": 1, "drafts": {}, "auto": {}}
    data.setdefault("auto", {})
    return data


def save_drafts(data):
    write_atomic(DRAFTS, json.dumps(data, indent=1).encode())


# ---------------------------------------------------------------- choosing moments

def label(record):
    names = [c.get("name") or c.get("appearance") or "a cat" for c in record.get("cats", [])]
    return " & ".join(names) if names else "a cat"


def interest(record):
    """How post-worthy a moment is likely to be: movement first, then a nice frame."""
    score = 2 * record.get("quality", 3) + record.get("score", 0)
    score += min(8.0, 80 * (record.get("motion") or 0))   # 0.05 of movement = +4
    score += 3 if len(record.get("cats", [])) >= 2 else 0
    score += RARE.get(record.get("activity"), 0)
    score += 1 if any(c.get("name") for c in record.get("cats", [])) else 0
    score -= 2.5 if record.get("activity") in ("sleeping", "resting") else 0
    return score


def candidates(sightings, start, end):
    out = []
    for name, record in sightings.items():
        if not record.get("is_cat") or not (CLIPS / name).is_file() or (record.get("person") or 0) >= 0.5:
            continue   # never post a moment with a person in it
        stamp = datetime.fromisoformat(record["timestamp"])
        if start <= stamp < end:
            out.append((interest(record), stamp, name, record))
    return sorted(out, key=lambda c: c[0], reverse=True)


def pick(cands, count, spread=timedelta(minutes=12), per_cat_first=False):
    chosen, seen_cats = [], set()
    if per_cat_first:
        for c in cands:
            who = label(c[3])
            if who not in seen_cats and all(abs(c[1] - o[1]) >= spread for o in chosen):
                chosen.append(c)
                seen_cats.add(who)
    for c in cands:
        if len(chosen) >= count:
            break
        if c not in chosen and all(abs(c[1] - o[1]) >= spread for o in chosen):
            chosen.append(c)
    return sorted(chosen[:count], key=lambda c: c[1])


# ---------------------------------------------------------------- writing the post

COPY_SHAPE = '{"hook": "...", "caption": "...", "youtube_title": "...", "hashtags": ["#..."]}'


def post_copy(model, kind, moments, cats, day_label):
    roster = "\n".join(f"- {c['name']}: {c['description']}" + (f" ({c['personality']})" if c.get("personality") else "")
                       for c in cats) or "- (cats not named yet)"
    lines = "\n".join(f"- {m[1]:%I:%M %p}: {label(m[3])} {m[3]['activity']}"
                      + (f" on the {m[3]['spot']}" if m[3].get("spot") else "")
                      + (f' ("{m[3]["caption"]}")' if m[3].get("caption") else "") for m in moments)
    what = {"moment": "a single cat moment", "recap": f"a recap of {day_label}",
            "week": f"the week's best moments ({day_label})"}[kind]
    prompt = f"""You write social media copy for a cat account built around a home window camera.
The cats:
{roster}

This vertical video ({what}) shows:
{lines}

Write:
- hook: 2 to 7 words burned onto the top of the video. Funny, specific, scroll-stopping; uses a cat name
  when known. Like "Siya has entered nap mode" or "Faff is on patrol". No hashtags, no emoji.
- caption: 1 or 2 short sentences for Instagram/TikTok in the cats' voice or a wry narrator; may end
  with one emoji.
- youtube_title: under 70 characters, ends with " #Shorts".
- hashtags: 6 to 10 relevant hashtags, mixing broad (#cats, #catsofinstagram) and specific ones.
Only describe what is listed above. Use a cat's name only where the list above names that cat; a cat
listed by its coat ("a grey cat") stays unnamed. Reply with only JSON: {COPY_SHAPE}"""
    answer = model.ask(prompt, schema=True, timeout=300)
    hook = clean(str(answer.get("hook", "")))[:60] or fallback_hook(moments)
    tags = [t if t.startswith("#") else "#" + t for t in answer.get("hashtags", []) if isinstance(t, str)]
    tags = [re.sub(r"[^#\w]", "", t) for t in tags][:10] or ["#cats", "#catsofinstagram", "#catvideos"]
    title = str(answer.get("youtube_title", "")).strip()[:90] or hook
    if "#shorts" not in title.lower():
        title = title[:80] + " #Shorts"
    return {"hook": hook, "caption": str(answer.get("caption", "")).strip()[:300],
            "youtube_title": title, "hashtags": tags}


def fallback_hook(moments):
    record = moments[0][3]
    return f"{label(record)}, {record['activity']}".capitalize()


def fallback_copy(moments):
    hook = fallback_hook(moments)
    return {"hook": hook, "caption": moments[0][3].get("caption", ""), "youtube_title": hook + " #Shorts",
            "hashtags": ["#cats", "#catsofinstagram", "#catvideos", "#catlife", "#windowcat", "#shorts"]}


# ---------------------------------------------------------------- rendering

def run(args, timeout=600):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()[-400:] or f"{args[0]} failed")
    return result.stdout


_FONT = None


def font():
    global _FONT
    if _FONT is None:
        _FONT = ""
        for pattern in ("DejaVu Sans:bold", "Noto Sans:bold", "Liberation Sans:bold", "Sans:bold"):
            try:
                path = run(["fc-match", "-f", "%{file}", pattern], timeout=10).decode().strip()
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                continue
            if path and Path(path).is_file():
                _FONT = path
                break
    return _FONT


def clean(text):
    text = "".join(ch for ch in text if ch.isprintable() and ord(ch) < 0x2000 or ch in "’‘“”—–…")
    return re.sub(r"\s+", " ", text).strip().strip('"')


def wrap(text, width):
    lines, line = [], ""
    for word in clean(text).split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return "\n".join(lines[:4])


def probe_seconds(path):
    try:
        return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def text_filter(textfile, size, y, alpha=1.0, box=True):
    face = f"fontfile='{font()}'" if font() else "font='Sans'"
    parts = [face, f"textfile='{textfile}'", "expansion=none", f"fontsize={size}", f"fontcolor=white@{alpha}",
             f"line_spacing={int(size * 0.22)}", "x=(w-text_w)/2", f"y={y}", "shadowcolor=black@0.6", "shadowx=2",
             "shadowy=2"]
    if box:
        parts += ["box=1", "boxcolor=black@0.42", f"boxborderw={int(size * 0.38)}"]
    return "drawtext=" + ":".join(parts)


def render_segment(work, index, name, record, start, seconds, hook, caption, handle):
    """One 1080x1920 piece: blurred full view behind a 4:5 window centred on the cat."""
    box = record.get("box")
    cx = box[0] if box else 0.5
    crop_w = 576   # 4:5 from a 1280x720 frame
    x = int(min(1280 - crop_w, max(0, cx * 1280 - crop_w / 2)))
    hook_file, cap_file, handle_file = (work / f"{index}-{n}.txt" for n in ("hook", "cap", "handle"))
    hook_file.write_text(wrap(hook, 18))
    cap_file.write_text(wrap(caption, 30))
    handle_file.write_text(handle)
    fade_out = max(0.1, seconds - 0.3)
    graph = (f"[0:v]scale=1280:720,setsar=1,fps=30,split[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,eq=brightness=-0.13[bg];"
             f"[b]crop={crop_w}:720:{x}:0,scale={W}:1350[fg];"
             f"[bg][fg]overlay=0:285,"
             # Text stays inside the platforms' safe zone (clear of the bottom and right-hand buttons).
             + (text_filter(handle_file, 32, 74, alpha=0.8, box=False) + "," if handle else "")
             + (text_filter(hook_file, 72, 150) + "," if hook else "")
             + (text_filter(cap_file, 46, "h-text_h-480") + "," if caption else "")
             + f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.2f}:d=0.3[v]")
    out = work / f"part-{index:02d}.mp4"
    run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
         "-i", str(CLIPS / name), "-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=r=44100:cl=stereo",
         "-filter_complex", graph, "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "21", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "96k", "-shortest",
         "-threads", "2", str(out)], timeout=900)
    return out


def render(draft_id, kind, moments, copy, handle):
    work = Path(tempfile.mkdtemp(prefix="studio-", dir=STUDIO))
    try:
        parts, per = [], {"moment": 12.0, "recap": 5.0, "week": 4.5}[kind]
        for i, (_, stamp, name, record) in enumerate(moments):
            length = probe_seconds(CLIPS / name)
            at = float(record.get("at") or 0)
            start = max(0.0, min(at - (2.5 if kind == "moment" else 1.0), length - per))
            seconds = max(2.0, min(per, length - start))
            who = label(record)
            if kind == "moment":
                caption = record.get("caption") or ""
            else:
                caption = f"{who} · {stamp:%I:%M %p}".replace(" 0", " ") if kind == "recap" else \
                    f"{who} · {stamp:%a %I:%M %p}".replace(" 0", " ")
            parts.append(render_segment(work, i, name, record, start, seconds, copy["hook"], caption, handle))
        target = STUDIO / f"{draft_id}.mp4"
        if len(parts) == 1:
            shutil.move(str(parts[0]), target)
        else:
            listing = work / "list.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in parts))
            run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                 "-movflags", "+faststart", str(work / "joined.mp4")], timeout=300)
            shutil.move(str(work / "joined.mp4"), target)
        run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(target), "-frames:v", "1", "-vf", "scale=540:-2",
             "-q:v", "4", str(STUDIO / f"{draft_id}.jpg")], timeout=60)
        return round(probe_seconds(target), 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- drafts

def make_draft(store, model, kind, moments, cats, day_label, origin):
    draft_id = secrets.token_hex(8)
    try:
        copy = post_copy(model, kind, moments, cats, day_label) if model else fallback_copy(moments)
    except (OSError, ValueError, RuntimeError) as error:
        log(f"post text fell back to a template: {error}")
        copy = fallback_copy(moments)
    draft = {"id": draft_id, "kind": kind, "created": time.time(), "origin": origin, "day": day_label,
             "sources": [m[2] for m in moments], "who": sorted({label(m[3]) for m in moments}),
             "status": "rendering", "keep": False, **copy}
    store["drafts"][draft_id] = draft
    save_drafts(store)
    try:
        draft["duration"] = render(draft_id, kind, moments, copy, settings()["handle"])
        draft["status"] = "ready"
        log(f"{kind} ready: {copy['hook']} ({draft['duration']} s)")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        draft["status"] = "failed"
        draft["error"] = str(error)[-300:]
        log(f"{kind} failed: {error}")
    save_drafts(store)
    return draft


def remove_draft(store, draft_id):
    store["drafts"].pop(draft_id, None)
    for ext in ("mp4", "jpg"):
        (STUDIO / f"{draft_id}.{ext}").unlink(missing_ok=True)


def handle_requests(store, sightings, model, cats):
    """At most one render per call; deletes and keeps are instant."""
    if not REQUESTS.is_dir():
        return
    queued = [(path, read_json(path, {})) for path in sorted(REQUESTS.glob("*.json"))]
    queued.sort(key=lambda q: q[1].get("action") == "make")   # quick changes first, then one render
    for path, request in queued:
        if not isinstance(request, dict):
            request = {}
        path.unlink(missing_ok=True)
        action, draft_id = request.get("action"), str(request.get("id", ""))
        if action == "delete" and DRAFT_ID.fullmatch(draft_id):
            remove_draft(store, draft_id)
            save_drafts(store)
        elif action == "keep" and draft_id in store["drafts"]:
            store["drafts"][draft_id]["keep"] = bool(request.get("keep"))
            save_drafts(store)
        elif action == "make":
            name = str(request.get("name", ""))
            record = sightings.get(name)
            if not record or not (CLIPS / name).is_file():
                log(f"can't make a short from {name}: not an analysed cat sighting")
                continue
            stamp = datetime.fromisoformat(record["timestamp"])
            make_draft(store, model, "moment", [(interest(record), stamp, name, record)], cats,
                       f"{stamp:%a %b %d}", "you")
            return   # one render per pass


def auto_drafts(store, sightings, model, cats, now):
    conf = settings()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(conf.get("daily_time", "20:30")))
    if not match or (now.hour, now.minute) < (int(match.group(1)), int(match.group(2))):
        return
    today = now.date()
    day_start = datetime.combine(today, datetime.min.time())
    if conf.get("auto_daily"):
        key = "day-" + today.isoformat()
        if key not in store["auto"]:
            store["auto"][key] = time.time()
            save_drafts(store)
            cands = candidates(sightings, day_start, day_start + timedelta(days=1))
            label_day = f"{today:%A}"
            if cands:
                make_draft(store, model, "moment", pick(cands, 1), cats, label_day, "moment of the day")
            if len(cands) >= 3:
                make_draft(store, model, "recap", pick(cands, 5), cats, label_day, "daily recap")
            return
    if conf.get("auto_weekly") and today.weekday() == 6:
        key = "week-" + today.isoformat()
        if key not in store["auto"]:
            store["auto"][key] = time.time()
            save_drafts(store)
            cands = candidates(sightings, day_start - timedelta(days=6), day_start + timedelta(days=1))
            if len(cands) >= 3:
                make_draft(store, model, "week", pick(cands, 8, spread=timedelta(hours=2), per_cat_first=True),
                           cats, f"week of {today - timedelta(days=6):%b %d}", "week at the window")


def tidy(store, now):
    cutoff = now.timestamp() - KEEP_DAYS * 86400
    for draft_id, draft in list(store["drafts"].items()):
        if not draft.get("keep") and draft.get("created", 0) < cutoff:
            remove_draft(store, draft_id)
        elif draft.get("status") == "rendering" and now.timestamp() - draft.get("created", 0) > 3600:
            draft["status"], draft["error"] = "failed", "Rendering was interrupted."
    unkept = sorted((d for d in store["drafts"].values() if not d.get("keep")), key=lambda d: d["created"])
    for draft in unkept[:max(0, len(store["drafts"]) - MAX_DRAFTS)]:
        remove_draft(store, draft["id"])
    store["auto"] = {k: v for k, v in store["auto"].items() if now.timestamp() - v < 40 * 86400}
    for leftover in STUDIO.glob("studio-*"):
        if leftover.is_dir() and now.timestamp() - leftover.stat().st_mtime > 3600:
            shutil.rmtree(leftover, ignore_errors=True)


def tick(sightings, model, cats, now=None):
    """Called by insights.py each pass. model may be None when Ollama is down."""
    now = now or datetime.now()
    STUDIO.mkdir(parents=True, exist_ok=True)
    REQUESTS.mkdir(parents=True, exist_ok=True)
    store = load_drafts()
    tidy(store, now)
    save_drafts(store)
    handle_requests(store, sightings, model, cats)
    auto_drafts(store, sightings, model, cats, now)
