"""Who is it? Photo-based cat recognition for Cat Cam.

Each likely-cat moment gets an embedding of the cat's crop from DINOv2-small (a general
image model, run locally with onnxruntime). A small gallery of known examples per cat is
compared against it: the closest cat wins if it is clearly closer than the runner-up;
otherwise the answer is "unsure" rather than a guess.

The gallery fills itself: the vision model's name guesses are used as rough labels, then
cleaned by clustering, so each cat's examples are the ones that actually look alike.
Faure's one-tap fixes ("this is Siya") are added as trusted examples and always win.

Files (all under ~/.local/share/catcam/identity/):
  embeddings.json   sighting name -> embedding (384 numbers)
  gallery.json      examples: name, sighting, source ("auto" | "you")
  crops/NAME.jpg    the crop that was embedded, for the "Who's this?" check
  fixes/*.json      one-tap fixes dropped here by the website
"""

import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

HOME = Path.home()
CLIPS = HOME / "Videos" / "CatCam" / "clips"
MODEL = HOME / ".local" / "share" / "catcam" / "highlights" / "dinov2-small.onnx"
DIR = HOME / ".local" / "share" / "catcam" / "identity"
EMBEDDINGS = DIR / "embeddings.json"
GALLERY = DIR / "gallery.json"
CROPS = DIR / "crops"
FIXES = DIR / "fixes"
SIDE = 224
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
MIN_SIMILARITY = 0.55     # below this, the cat looks like none of the examples
MIN_MARGIN = 0.04         # the winner must beat the runner-up by this much
AUTO_PER_CAT = 15
TOP_K = 3


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}-", delete=False) as out:
        json.dump(data, out)
        staging = Path(out.name)
    os.chmod(staging, 0o644)
    staging.replace(path)


# ---------------------------------------------------------------- vectors (plain Python, no numpy needed)

def normalise(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def mean(vectors):
    if not vectors:
        return None
    return normalise([sum(col) / len(vectors) for col in zip(*vectors)])


# ---------------------------------------------------------------- embedding

class Embedder:
    def __init__(self):
        self.session = None
        try:
            import numpy
            import onnxruntime
        except ImportError:
            return
        if not MODEL.is_file():
            return
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.np = numpy
        self.session = onnxruntime.InferenceSession(str(MODEL), sess_options=options,
                                                    providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    @property
    def ready(self):
        return self.session is not None

    def crop(self, name, at, box):
        """A square crop around the cat as 224x224 RGB bytes, plus a small JPEG for the website."""
        cx, cy, w, h = box
        side = max(w * 1280, h * 720) * 1.25
        side = max(96.0, min(side, 720.0))
        x = max(0.0, min(1280 - side, cx * 1280 - side / 2))
        y = max(0.0, min(720 - side, cy * 720 - side / 2))
        crop = f"scale=1280:720,crop={int(side)}:{int(side)}:{int(x)}:{int(y)}"
        args = ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(CLIPS / name), "-frames:v", "1",
                "-filter_complex", f"[0:v]{crop},split=2[a][b];[a]scale={SIDE}:{SIDE}[raw];[b]scale=256:256[jpg]",
                "-map", "[raw]", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        CROPS.mkdir(parents=True, exist_ok=True)
        args += ["-map", "[jpg]", "-q:v", "4", "-y", str(CROPS / (name + ".jpg"))]
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        if result.returncode or len(result.stdout) != SIDE * SIDE * 3:
            raise RuntimeError("could not crop the cat")
        return result.stdout

    def embed(self, rgb):
        np = self.np
        pixels = np.frombuffer(rgb, np.uint8).reshape(SIDE, SIDE, 3).astype(np.float32) / 255.0
        pixels = (pixels - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
        out = self.session.run(None, {self.input: pixels.transpose(2, 0, 1)[None]})[0][0]
        cls = out[0]
        return normalise([round(float(v), 5) for v in cls])


# ---------------------------------------------------------------- gallery and classification

def load_gallery():
    data = read_json(GALLERY, {})
    examples = data.get("examples") if isinstance(data, dict) else None
    return [e for e in examples if isinstance(e, dict) and e.get("name") and e.get("sighting")] \
        if isinstance(examples, list) else []


def classify(vector, gallery, embeddings, names):
    """-> (name or None, best similarity, margin)."""
    scores = []
    for cat in names:
        sims = []
        for example in gallery:
            if example["name"] != cat:
                continue
            other = embeddings.get(example["sighting"])
            if other:
                sim = cosine(vector, other)
                sims.extend([sim, sim] if example.get("source") == "you" else [sim])
        if sims:
            top = sorted(sims, reverse=True)[:TOP_K]
            scores.append((sum(top) / len(top), cat))
    if not scores:
        return None, 0.0, 0.0
    scores.sort(reverse=True)
    best, cat = scores[0]
    margin = best - scores[1][0] if len(scores) > 1 else best
    if best < MIN_SIMILARITY or margin < MIN_MARGIN:
        return None, round(best, 3), round(margin, 3)
    return cat, round(best, 3), round(margin, 3)


def seed_gallery(records, embeddings, names, gallery):
    """Rebuild the automatic examples from the vision model's guesses, cleaned by clustering.
    Faure's own fixes are kept as they are."""
    mine = [e for e in gallery if e.get("source") == "you"]
    weak = {cat: [] for cat in names}
    for sighting, record in records.items():
        vector = embeddings.get(sighting)
        guessed = [c.get("name") for c in record.get("vlm_cats", record.get("cats", [])) if isinstance(c, dict)]
        if vector and len(guessed) == 1 and guessed[0] in weak:
            weak[guessed[0]].append((sighting, vector))
    for example in mine:
        if example["name"] in weak and embeddings.get(example["sighting"]):
            weak[example["name"]] += [(example["sighting"], embeddings[example["sighting"]])] * 3
    centres = {cat: mean([v for _, v in items]) for cat, items in weak.items() if items}
    if len(centres) < 2:
        return mine + [{"name": cat, "sighting": s, "source": "auto"} for cat, items in weak.items()
                       for s, _ in items[:AUTO_PER_CAT]]
    # A few rounds of k-means, starting from each cat's guessed centre.
    pool = [(s, v) for items in weak.values() for s, v in items]
    pool = list({s: (s, v) for s, v in pool}.values())
    for _ in range(8):
        members = {cat: [] for cat in centres}
        for s, v in pool:
            cat = max(centres, key=lambda c: cosine(v, centres[c]))
            members[cat].append((s, v))
        centres = {cat: mean([v for _, v in items]) or centres[cat] for cat, items in members.items()}
    auto, used = [], {e["sighting"] for e in mine}
    for cat, items in members.items():
        ranked = sorted(items, key=lambda sv: cosine(sv[1], centres[cat]), reverse=True)
        for s, _ in ranked[:AUTO_PER_CAT]:
            if s not in used:
                auto.append({"name": cat, "sighting": s, "source": "auto"})
                used.add(s)
    return mine + auto


def take_fixes(gallery):
    """One-tap fixes from the website: {"sighting": NAME, "cat": "Siya" | "none"}."""
    changed = {}
    if not FIXES.is_dir():
        return gallery, changed
    for path in sorted(FIXES.glob("*.json")):
        fix = read_json(path, {})
        path.unlink(missing_ok=True)
        sighting, cat = str(fix.get("sighting", "")), str(fix.get("cat", ""))
        if not sighting:
            continue
        gallery = [e for e in gallery if e["sighting"] != sighting]
        if cat and cat != "none":
            gallery.append({"name": cat, "sighting": sighting, "source": "you", "t": time.time()})
        changed[sighting] = cat
    return gallery, changed


class Identity:
    """Used by insights.py: embed new sightings, then (re)label all recent ones."""

    def __init__(self):
        self.embedder = Embedder()
        self.embeddings = read_json(EMBEDDINGS, {})
        if not isinstance(self.embeddings, dict):
            self.embeddings = {}

    @property
    def ready(self):
        return self.embedder.ready

    def add(self, name, record):
        """Embed one analysed sighting (needs its cat box). Quietly skips what it can't do."""
        if not self.ready or not record.get("box") or name in self.embeddings:
            return
        try:
            self.embeddings[name] = self.embedder.embed(self.embedder.crop(name, record["at"], record["box"]))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass

    def relabel(self, records, cats, reseed=False):
        """Apply photo identity to every single-cat sighting. Returns the names whose label changed."""
        names = [c["name"] for c in cats]
        keep = set(records)
        self.embeddings = {k: v for k, v in self.embeddings.items() if k in keep}
        for crop in CROPS.glob("*.jpg") if CROPS.is_dir() else []:
            if crop.stem not in keep:
                crop.unlink(missing_ok=True)
        write_json(EMBEDDINGS, self.embeddings)
        gallery = [e for e in load_gallery() if e["name"] in names and e["sighting"] in keep]
        gallery, fixed = take_fixes(gallery)
        if reseed or fixed or not any(e.get("source") == "auto" for e in gallery):
            gallery = seed_gallery(records, self.embeddings, names, gallery)
        write_json(GALLERY, {"version": 1, "examples": gallery, "updated": time.time()})
        changed = []
        for sighting, record in records.items():
            if not record.get("is_cat"):
                continue
            vlm = record.setdefault("vlm_cats", record.get("cats", []))
            if sighting in fixed:
                cat = fixed[sighting]
                record["cats"] = [{"name": cat, "appearance": (vlm[0]["appearance"] if vlm else "a cat")}] \
                    if cat != "none" else []
                record["is_cat"] = cat != "none"
                record["identity"] = {"name": cat, "method": "you"}
                changed.append(sighting)
                continue
            if record.get("identity", {}).get("method") == "you" or len(vlm) != 1:
                continue
            vector = self.embeddings.get(sighting)
            if not vector:
                continue
            cat, best, margin = classify(vector, gallery, self.embeddings, names)
            appearance = vlm[0].get("appearance") or "a cat"
            new = [{"name": cat, "appearance": appearance}]
            record["identity"] = {"name": cat, "similarity": best, "margin": margin, "method": "photo"}
            if new != record.get("cats"):
                record["cats"] = new
                changed.append(sighting)
        return changed

    def unsure(self, records, limit=8):
        """The single-cat sightings the model is least sure about, for the "Who's this?" check."""
        out = []
        for sighting, record in records.items():
            ident = record.get("identity", {})
            if record.get("is_cat") and ident.get("method") == "photo" and (CROPS / (sighting + ".jpg")).is_file():
                out.append((ident.get("margin", 0), sighting))
        return [s for _, s in sorted(out)[:limit]]
