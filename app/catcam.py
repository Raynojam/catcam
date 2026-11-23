"""Private cat-camera viewer and Motion recorder for mina. Run with python3."""

import json
import importlib.util
import hashlib
import math
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from functools import partial, lru_cache
from html import escape
from http.client import HTTPException
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import urlopen


SOURCE = "http://127.0.0.1:8085"
VIEW_PORT = 8086
CLIPS = Path.home() / "Videos" / "CatCam" / "clips"
RETENTION = Path.home() / ".config" / "catcam" / "retention-days"
MOTION_NAME = re.compile(r"\d{8}-\d{6}-\d+\.mp4")
REEL_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.mp4")
REELS = CLIPS.parent / "highlights"
HIGHLIGHT_DATA = Path.home() / ".local" / "share" / "catcam" / "highlights"
MODEL = HIGHLIGHT_DATA / "yolox_nano.onnx"
MODEL_URL = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/"
    "0.1.1rc0/yolox_nano.onnx"
)
WORKER_PYTHON = HIGHLIGHT_DATA / "venv" / "bin" / "python"
SCAN_STATE = HIGHLIGHT_DATA / "scan.json"
SAMPLE_SIZE = 416
CAT_CLASS = 15
CAT_THRESHOLD = 0.35
SCAN_INTERVAL = 180
SCAN_BATCH = 32
SCAN_DAYS = 7
REEL_MOMENTS = 10
MOMENT_SECONDS = 6
THUMBNAILS = HIGHLIGHT_DATA / "thumbnails"
THUMBNAIL_LOCK = threading.Lock()
SAVED_SIGHTINGS = HIGHLIGHT_DATA / "saved-sightings.json"
SAVED_LOCK = threading.RLock()
SHORTS = HIGHLIGHT_DATA / "short-clips"
SHORT_NAME = re.compile(r"[0-9a-f]{24}")
SHORT_SECONDS = 12
SHORT_QUEUE = queue.Queue(maxsize=8)
SHORT_LOCK = threading.RLock()
SHORT_JOBS = {}
SHORT_WORKER = None

APP_VERSION = "2026.11.23.3"

# Cat Cam Plus: a stronger detector when present, and the diary written by insights.py.
STRONG_MODEL = HIGHLIGHT_DATA / "yolox_s.onnx"
INSIGHTS_DATA = Path.home() / ".local" / "share" / "catcam" / "insights" / "insights.json"
CATS_CONFIG = Path.home() / ".config" / "catcam" / "cats.json"
CAT_NAME = re.compile(r"[^\x00-\x1f\x7f<>]{1,40}")
CAT_TEXT = re.compile(r"[^\x00-\x1f\x7f<>]{0,200}")
STUDIO = Path.home() / "Videos" / "CatCam" / "studio"
STUDIO_SETTINGS = Path.home() / ".config" / "catcam" / "studio.json"
STUDIO_ID = re.compile(r"[0-9a-f]{16}")

# Frame stage (frame.py): the windowsill zone and picture settings, and its status.
FRAME_CONFIG = Path.home() / ".config" / "catcam" / "frame.json"
FRAME_DIR = Path.home() / ".local" / "share" / "catcam" / "frame"
PAGES = Path.home() / ".local" / "share" / "catcam" / "pages"


def page(name):
    try:
        return (PAGES / name).read_bytes()
    except OSError:
        return b"<!doctype html><title>Cat Cam</title><p>This page is not installed yet. Run catcam-update on mina."


def frame_config():
    config = {"enabled": True, "zone": None, "low_light": True, "strength": 0.5, "output": "1280x720"}
    try:
        stored = json.loads(FRAME_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        config.update({k: stored[k] for k in config if k in stored})
    return config


def recording_size():
    """Motion records what the frame stage outputs (1280x720 unless changed on the Camera page)."""
    match = re.fullmatch(r"(1280)x(720)|(1920)x(1080)", str(frame_config().get("output")))
    if not match:
        return 1280, 720
    return (1920, 1080) if match.group(3) else (1280, 720)


def camera_settings():
    try:
        status = json.loads((FRAME_DIR / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    config = frame_config()
    return {"config": config, "status": status if isinstance(status, dict) else {},
            "recording": "x".join(map(str, recording_size()))}


def camera_request(body):
    action = body.get("action")
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    if action == "preview":
        (FRAME_DIR / "preview-request").write_text(str(time.time()))
        os.utime(FRAME_DIR / "preview-request")
        return {"preview": True}
    if action == "reset":
        # catcam-guard (root) picks this up within 30 s and resets the camera over USB.
        (FRAME_DIR / "reset-request").write_text(str(time.time()))
        return {"reset": True}
    if action != "save":
        raise ValueError("Unknown camera action")
    zone = body.get("zone")
    if zone is not None:
        if not isinstance(zone, list) or len(zone) != 4 or not all(type(v) in (int, float) for v in zone):
            raise ValueError("Invalid zone")
        x, y, w, h = (float(v) for v in zone)
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0.1 <= w <= 1 and 0.1 <= h <= 1 and x + w <= 1.001 and y + h <= 1.001):
            raise ValueError("Invalid zone")
        zone = [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]
    strength = body.get("strength", 0.5)
    if type(strength) not in (int, float) or not 0 <= strength <= 1:
        raise ValueError("Invalid strength")
    output = body.get("output", "1280x720")
    if output not in ("1280x720", "1920x1080"):
        raise ValueError("Invalid output size")
    try:
        stored = json.loads(FRAME_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    stored.update({"enabled": True, "zone": zone, "low_light": body.get("low_light") is True,
                   "strength": round(float(strength), 2), "output": output})
    FRAME_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(FRAME_CONFIG, json.dumps(stored, indent=1).encode())
    return {"saved": True}

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#17251e"><meta name="color-scheme" content="dark">
<title>Cat Cam · The window</title>
<style>
:root{--bg:#101c16;--panel:#1b2b21;--line:#374a3c;--ink:#f0f0df;--muted:#b1bfae;--lime:#d4ec9a;--sand:#e6dfc7;--dark:#233527;font:15px/1.5 system-ui,sans-serif;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}[hidden]{display:none!important}a{color:inherit;text-underline-offset:4px}button,select{font:inherit}button,a,select{outline-offset:4px}:focus-visible{outline:2px solid var(--lime)}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}.wrap{width:min(1460px,100%);padding-inline:clamp(18px,4.3vw,66px);margin:auto}.top{min-height:77px;display:flex;justify-content:space-between;align-items:center;gap:24px;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:10px;font-weight:750;font-size:1.15rem;text-decoration:none;letter-spacing:-.035em}.catmark{color:var(--lime);width:26px;height:26px}.nav{display:flex;gap:24px;align-items:center}.nav a{color:var(--muted);font-size:.85rem;text-decoration:none}.nav a:hover,.nav a[aria-current]{color:var(--ink)}.nav a[aria-current]{border-bottom:2px solid var(--lime);padding-block:26px}.heading{display:flex;align-items:end;justify-content:space-between;gap:22px;padding:36px 0 24px}.eyebrow{font:600 .68rem/1.5 ui-monospace,monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--lime)}h1{font:400 clamp(2.8rem,5.4vw,5.1rem)/1.03 Georgia,serif;letter-spacing:-.045em;margin:9px 0 12px}h1 em{color:var(--lime);font-weight:400}p{margin:0}.sub{color:var(--muted);font-size:.9rem}.date-tools{display:flex;align-items:center;gap:9px;flex:none}select,.small-button{border:1px solid #5a705b;border-radius:8px;background:var(--panel);color:var(--ink);padding:9px 12px}.small-button{font-size:.8rem}.small-button:hover{border-color:var(--lime)}.main-grid{display:grid;grid-template-columns:minmax(0,1.95fr) minmax(280px,.88fr);gap:20px;align-items:stretch}.live-panel{border:1px solid var(--line);background:var(--panel);border-radius:15px;overflow:hidden;min-width:0}.panel-top{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;gap:8px;font-size:.8rem}.panel-top strong{font-weight:600}.live-badge{font:600 .65rem ui-monospace,monospace;letter-spacing:.08em;color:var(--muted);display:flex;align-items:center;gap:7px}.dot{height:7px;width:7px;display:inline-block;border-radius:50%;background:var(--muted)}[data-state=live] .dot{background:var(--lime);box-shadow:0 0 0 4px #d4ec9a12}[data-state=retry] .dot{background:#e7b68d}.stage{position:relative;aspect-ratio:16/9;background:#080f0b}.stage canvas{display:block;width:100%;height:100%;object-fit:contain}.stage:fullscreen{display:grid;place-items:center;width:100vw;height:100vh;aspect-ratio:auto}.stage:fullscreen canvas{max-width:100%;max-height:100vh}.overlay{position:absolute;inset:0;display:grid;place-content:center;text-align:center;padding:25px;background:#101c16d9;pointer-events:none}.overlay strong{font:400 clamp(1.4rem,3vw,2rem) Georgia,serif}.overlay span{color:var(--muted);font-size:.83rem;margin-top:8px}.stage[data-state=retry] canvas{opacity:.35}.live-tools{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:12px 16px}.toolset{display:flex;flex-wrap:wrap;gap:6px}.tool{border:1px solid #50634e;background:transparent;color:var(--ink);border-radius:7px;padding:7px 10px;font-size:.77rem}.tool:hover{background:#30432e}.live-caption{color:var(--muted);font-size:.72rem}.diary{background:var(--sand);color:var(--dark);border-radius:15px;padding:25px;display:flex;flex-direction:column;position:relative;overflow:hidden}.diary .eyebrow{color:#5c6a44}.diary h2{font:400 2.15rem/1.1 Georgia,serif;letter-spacing:-.035em;margin:15px 0 12px}.diary .note{font-size:.84rem;color:#4c604c;max-width:40ch}.countrow{display:grid;grid-template-columns:1fr 1fr;margin:22px 0;padding:18px 0;border-block:1px solid #bac2a8}.countrow div+div{padding-left:22px;border-left:1px solid #bac2a8}.countrow strong{display:block;font:400 2.6rem/1 Georgia,serif}.countrow span{display:block;font-size:.7rem;margin-top:6px;color:#4b604c}.diary .primary{display:block;margin-top:auto;padding:12px 14px;border-radius:8px;background:var(--dark);color:#eef1db;text-align:center;text-decoration:none;font-size:.8rem}.diary .primary:hover{background:#354d36}.diary .fine{font-size:.7rem;color:#54684f;margin:12px 0 0}.lower-grid{display:grid;grid-template-columns:minmax(0,1.95fr) minmax(280px,.88fr);gap:20px;margin-top:24px}.section-top{display:flex;justify-content:space-between;gap:10px;align-items:baseline;margin-bottom:14px}.section-top h2{font:400 1.6rem Georgia,serif;letter-spacing:-.02em;margin:0}.section-top a{color:var(--lime);font-size:.77rem;white-space:nowrap}.moments{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;list-style:none;padding:0;margin:0}.moment{min-width:0}.moment a{display:block;text-decoration:none;border:1px solid var(--line);border-radius:11px;overflow:hidden;background:var(--panel)}.moment a:hover{border-color:#94a77a}.moment img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#223529}.moment footer{padding:10px 12px}.moment strong{font-size:.81rem;font-weight:550}.moment small{display:block;color:var(--muted);font-size:.68rem;margin-top:2px}.empty{grid-column:1/-1;border:1px dashed var(--line);border-radius:10px;padding:25px;color:var(--muted);font-size:.87rem}.activity{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:19px;align-self:start}.activity h2{font:400 1.5rem Georgia,serif;margin:0 0 6px}.activity .sub{font-size:.76rem}.chart{display:grid;grid-template-columns:repeat(24,minmax(0,1fr));align-items:end;height:112px;gap:3px;margin-top:20px}.bar{display:flex;align-items:end;justify-content:center;height:100%;min-width:0;padding:0 1px 0;border:0;background:transparent;border-bottom:1px solid #75816b;border-radius:3px 3px 0 0}.bar span{display:block;width:100%;height:var(--height);min-height:3px;background:#a2bc7d;border-radius:2px 2px 0 0}.bar.zero span{background:#40553d}.bar:hover span,.bar[aria-pressed=true] span{background:var(--lime)}.chart-labels{display:flex;justify-content:space-between;color:var(--muted);font:.65rem ui-monospace,monospace;margin:8px 0 13px}.activity-note{font-size:.74rem;color:var(--muted);min-height:2.8em}.filter-reset{border:0;color:var(--lime);background:transparent;padding:8px 0 0;font-size:.76rem;text-decoration:underline;text-underline-offset:3px}.system-strip{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;border-block:1px solid var(--line);padding:18px 0;margin-top:28px}.system-strip span{display:block;color:var(--muted);font-size:.74rem;margin-top:3px}.system-strip strong{font-size:.76rem;font-weight:500}.footnote{padding:17px 0 30px;display:flex;gap:15px;justify-content:space-between;flex-wrap:wrap;font-size:.68rem;color:var(--muted)}.footnote a{color:var(--lime)}.notice{font-size:.8rem;color:#f0b496;margin-top:14px}.skip{position:absolute;left:-1000px;top:0}.skip:focus{left:10px;z-index:10;background:var(--bg);padding:12px}#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--sand);color:var(--dark);border:1px solid #8a9877;border-radius:10px;padding:12px 18px;font-size:.85rem;max-width:90vw;z-index:5}body.focus .heading,body.focus .diary,body.focus .lower-grid,body.focus .system-strip,body.focus .footnote{display:none}body.focus .main-grid{display:block;margin-top:20px}body.focus .live-panel{max-width:1200px;margin:auto}
.diary .note.from-diary{font:400 .98rem/1.55 Georgia,serif;color:#2c3d2e;max-width:none;display:-webkit-box;-webkit-line-clamp:7;-webkit-box-orient:vertical;overflow:hidden}.diary-link{font-size:.76rem;color:#3d5a33;margin-top:8px;display:inline-block}.moment .who{color:var(--lime)}.moment small.caption{font:italic 400 .74rem/1.4 Georgia,serif;color:#cdd6c3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(max-width:1000px){.main-grid,.lower-grid{grid-template-columns:minmax(0,1.6fr) minmax(250px,1fr)}.diary{padding:20px}.diary h2{font-size:1.8rem}.countrow{margin:14px 0;padding:12px 0}.moments{grid-template-columns:repeat(2,minmax(0,1fr))}.nav{gap:16px}}
@media(max-width:760px){.wrap{padding-inline:18px}.top{align-items:start;flex-direction:column;gap:10px;padding:18px 0 12px;min-height:0}.nav{gap:20px;flex-wrap:wrap}.nav a{font-size:.77rem}.nav a[aria-current]{padding-block:0 6px}.heading{align-items:start;flex-direction:column;padding-top:24px;gap:16px}h1{font-size:3rem}.main-grid,.lower-grid{grid-template-columns:1fr}.diary{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}.diary>.eyebrow,.diary>h2,.diary>.note{grid-column:1}.diary .countrow{grid-column:2;grid-row:1/4;align-self:center}.diary .primary,.diary .fine{grid-column:1/-1}.diary .primary{margin-top:16px}.diary h2{margin:8px 0}.diary .countrow strong{font-size:2.2rem}.countrow div+div{padding-left:12px}.lower-grid{gap:24px}.activity{grid-row:1}.chart{height:95px}.chart-labels{margin-bottom:8px}.activity-note{min-height:0}.system-strip{grid-template-columns:1fr;gap:13px}.moments{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:390px){.diary{display:flex}.diary .countrow{width:100%}.diary h2{font-size:2rem}.date-tools{flex-wrap:wrap}.tool{padding:7px 8px}.nav{gap:15px}}
@media(prefers-reduced-motion:no-preference){.moment a,.tool{transition:background .15s,border-color .15s}}
</style></head><body>
<a class="skip" href="#main">Skip to camera</a>
<div class="wrap"><header class="top"><a class="brand" href="/" aria-label="Cat Cam home"><svg class="catmark" viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M6 16 5 5l9 6h4l9-6-1 11c4 15-24 15-20 0Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M11 19h1m8 0h1m-7 4 2 2 2-2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>Cat Cam</a>
<nav class="nav" aria-label="Cat camera"><a href="/" aria-current="page">The window</a><a href="/insights/">Diary</a><a href="/studio/">Studio</a><a href="/sightings/">Sightings</a><a href="/camera/">Camera</a><a href="/highlights/">Daily reels</a><a href="/sightings/?view=saved">Saved</a><a href="/storage/">Storage</a></nav></header>
<main id="main"><div class="heading"><div><div class="eyebrow">Mina · A little window into their world</div><h1>A day in <em>good company.</em></h1><p class="sub">Live from the window. Little moments, worth keeping.</p></div><div class="date-tools"><label for="day" class="eyebrow">Journal</label><select id="day" aria-label="Journal date" disabled><option>Loading…</option></select><button id="refresh" class="small-button" type="button">Refresh</button></div></div>
<div class="main-grid"><section class="live-panel" aria-label="Live camera"><div class="panel-top"><strong>The window, right now</strong><span class="live-badge" id="liveBadge" data-state="connecting"><i class="dot" aria-hidden="true"></i><span id="status" role="status">CONNECTING</span></span></div><div class="stage" id="stage"><canvas id="camera" width="1280" height="720" aria-label="Live view from Mina’s camera"></canvas><div class="overlay" id="overlay"><strong id="overlayTitle">Opening the window…</strong><span id="overlayHelp">Connecting to Mina’s camera.</span></div></div><div class="live-tools"><div class="toolset"><button class="tool" id="pause" type="button" aria-pressed="false">Pause live</button><button class="tool" id="snapshot" type="button" disabled>Save photo</button><button class="tool" id="focus" type="button" aria-pressed="false">Focus view</button><button class="tool" id="fullscreen" type="button">Full screen</button><button class="tool" id="reconnect" type="button" hidden>Reconnect</button></div><span class="live-caption" id="liveCaption">Live view is always now</span></div></section>
<aside class="diary" aria-label="Daily journal"><div class="eyebrow" id="journalDate">Today’s journal</div><h2 id="journalTitle">A day takes shape.</h2><p class="note" id="journalText">Gathering the moments Mina has found.</p><a class="diary-link" id="diaryLink" href="/insights/" hidden>Read the full diary →</a><div class="countrow"><div><strong id="catCount">—</strong><span>likely cat clips</span></div><div><strong id="savedCount">—</strong><span>saved this day</span></div></div><a class="primary" id="reelLink" href="/sightings/">Explore the sightings →</a><p class="fine" id="reelNote">Finished recordings are checked for likely cats.</p></aside></div>
<p class="notice" id="notice" role="status" hidden></p>
<div class="lower-grid"><section aria-labelledby="momentsTitle"><div class="section-top"><h2 id="momentsTitle">Little moments</h2><a id="allLink" href="/sightings/">See all sightings →</a></div><ul class="moments" id="moments"><li class="empty">Loading the day’s sightings…</li></ul></section><section class="activity" aria-labelledby="activityTitle"><h2 id="activityTitle">The rhythm of the day</h2><p class="sub">Likely cat clips, by hour. Tap a bar to explore.</p><div class="chart" id="chart" role="group" aria-label="Likely cat recordings by start hour"></div><div class="chart-labels" aria-hidden="true"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div><p class="activity-note" id="activityNote">Counts reflect recordings, not individual visits.</p><button class="filter-reset" id="resetHour" type="button" hidden>Show all hours</button></section></div>
<div class="system-strip"><div><strong>Detector</strong><span id="detector">Checking the latest scan…</span></div><div><strong>Recording storage</strong><span id="storage">Checking Mina’s storage…</span></div><div><strong>Camera &amp; diary</strong><span id="health">Star a sighting to protect its original recording.</span></div></div><footer class="footnote"><span id="timeNote">Times follow the camera on Mina. A visit can span several clips.</span><a href="/sightings/#storageSettings">Manage recordings & storage →</a></footer></main></div>
<div id="toast" role="status" hidden></div>
<script>
'use strict';
const $=id=>document.getElementById(id),camera=$('camera'),ctx=camera.getContext('2d',{alpha:false});
let insights=null,insightRequest=0,daily=null,selectedHour=null,dataController=null,dayRequest=0,pauseRequested=false,streamController=null,streamSequence=0,retryTimer=0,lastFrame=0,toastTimer=0;
const initialDay=new URLSearchParams(location.search).get('day')||'';
let followToday=!initialDay;
const clock=stamp=>new Date(stamp).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
const dayLabel=d=>new Date(d+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});
const gib=n=>(n/1073741824).toFixed(1)+' GiB';
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,3500)}
function streamState(state,title,help){$('liveBadge').dataset.state=state;$('stage').dataset.state=state;$('status').textContent=state==='live'?'LIVE':state==='paused'?'PAUSED':state==='retry'?'RECONNECTING':'CONNECTING';$('overlay').hidden=state==='live';$('overlayTitle').textContent=title||'';$('overlayHelp').textContent=help||'';$('snapshot').disabled=state!=='live';$('reconnect').hidden=state!=='retry';}
async function* jpegFrames(reader){const decoder=new TextDecoder();let buffer=new Uint8Array(0),length=0,online=true;while(true){const {value,done}=await reader.read();if(done)return;const joined=new Uint8Array(buffer.length+value.length);joined.set(buffer);joined.set(value,buffer.length);buffer=joined;while(buffer.length){if(!length){let end=0;while(end+3<buffer.length&&!(buffer[end]===13&&buffer[end+1]===10&&buffer[end+2]===13&&buffer[end+3]===10))end++;if(end>8192)throw Error('Invalid header');if(end+3>=buffer.length)break;const headers=decoder.decode(buffer.subarray(0,end)),match=/^Content-Length:\s*(\d+)\s*$/im.exec(headers);length=match?Number(match[1]):0;if(length<1||length>8*1024*1024)throw Error('Invalid frame size');online=!/^X-UStreamer-Online:\s*(0|false)\s*$/im.test(headers);buffer=buffer.subarray(end+4)}if(buffer.length<length)break;const jpeg=buffer.subarray(0,length);buffer=buffer.subarray(length);length=0;if(online)yield jpeg;else throw Error('Camera offline')}}}
async function watch(){clearTimeout(retryTimer);if(pauseRequested||document.hidden)return;const token=++streamSequence;streamController?.abort();const control=new AbortController();streamController=control;let frameAt=performance.now(),sampleStart=frameAt,count=0;streamState('connecting','Opening the window…','Connecting to Mina’s camera.');const watchdog=setInterval(()=>{if(performance.now()-frameAt>6000)control.abort()},1000);try{const r=await fetch('/live',{cache:'no-store',signal:control.signal});if(!r.ok||!r.body)throw Error();for await(const jpeg of jpegFrames(r.body.getReader())){if(token!==streamSequence)break;const bitmap=await createImageBitmap(new Blob([jpeg],{type:'image/jpeg'}));if(token!==streamSequence){bitmap.close();break}ctx.drawImage(bitmap,0,0,camera.width,camera.height);bitmap.close();frameAt=performance.now();lastFrame=Date.now();if(!count)streamState('live');count++;if(frameAt-sampleStart>=2000){$('liveCaption').textContent=Math.round(count*1000/(frameAt-sampleStart))+' fps · Live view is always now';count=0;sampleStart=frameAt}}}catch(_){}finally{clearInterval(watchdog);control.abort();if(token===streamSequence&&!pauseRequested&&!document.hidden){streamState('retry','The window is reconnecting.','Check that Mina is awake and the camera is in PCCAM mode.');$('liveCaption').textContent=lastFrame?'Last frame received '+new Date(lastFrame).toLocaleTimeString():'Waiting for the first frame';retryTimer=setTimeout(watch,2000)}}}
function stopStream(hidden=false){streamSequence++;clearTimeout(retryTimer);streamController?.abort();streamState('paused',hidden?'Live view is resting.':'Live view paused.','Mina’s recording service is unaffected.');$('liveCaption').textContent='Live viewing paused'}
$('pause').onclick=()=>{pauseRequested=!pauseRequested;$('pause').textContent=pauseRequested?'Resume live':'Pause live';$('pause').setAttribute('aria-pressed',String(pauseRequested));if(pauseRequested)stopStream();else watch()};
$('reconnect').onclick=watch;
$('focus').onclick=()=>{const on=document.body.classList.toggle('focus');$('focus').setAttribute('aria-pressed',String(on));$('focus').textContent=on?'Exit focus':'Focus view'};
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.body.classList.contains('focus'))$('focus').click()});
$('fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('stage').requestFullscreen()}catch(_){toast('Full screen is unavailable in this browser. Try Focus view.')}};
$('snapshot').onclick=()=>{if($('snapshot').disabled)return;camera.toBlob(blob=>{if(!blob){toast('Could not save a photo. Try again.');return}const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='catcam-'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('Photo ready to save.')},'image/jpeg',.94)};
function renderMoments(){const list=$('moments');list.replaceChildren();const events=daily.events.filter(e=>selectedHour===null||Number(e.timestamp.slice(11,13))===selectedHour);$('momentsTitle').textContent=selectedHour===null?'Little moments':String(selectedHour).padStart(2,'0')+':00–'+String(selectedHour).padStart(2,'0')+':59';for(const e of events.slice(0,6)){const li=document.createElement('li'),a=document.createElement('a'),img=document.createElement('img'),foot=document.createElement('footer'),strong=document.createElement('strong'),small=document.createElement('small');li.className='moment';a.href=e.link;img.src=e.thumbnail;img.alt='';img.loading='lazy';img.decoding='async';img.onerror=()=>{img.removeAttribute('src');img.onerror=null};const seen=insights&&insights.by[e.name];strong.textContent=clock(e.timestamp)+(e.saved?' · Saved':'');if(seen){const who=document.createElement('span');who.className='who';who.textContent=' · '+(seen.is_cat?seen.label:'maybe not a cat');strong.append(who);small.className='caption';small.textContent=seen.caption||'Watch the detected moment →'}else small.textContent='Watch the detected moment →';foot.append(strong,small);a.append(img,foot);li.append(a);list.append(li)}if(!events.length){const li=document.createElement('li');li.className='empty';li.textContent=selectedHour!==null?'No likely cat clips in this hour. Try another bar or show all hours.':daily.counts.pending?'Mina is still checking this day’s recordings.':daily.counts.outside_scan_window?'This date is outside the detector’s recent scan window. Saved sightings remain available.':'No likely cat clips for this day yet. You can still watch live above.';list.append(li)}$('resetHour').hidden=selectedHour===null;document.querySelectorAll('.bar').forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.hour)===selectedHour)));$('activityNote').textContent=selectedHour===null?(daily.peak_hour===null?'No likely cat clips found for this day.':'Most likely cat clips began in the '+String(daily.peak_hour).padStart(2,'0')+':00 hour.'):'This hour: '+events.length+' likely cat '+(events.length===1?'clip.':'clips.');}
function render(){const c=daily.counts,opts=$('day');opts.replaceChildren();for(const d of daily.days){const o=document.createElement('option');o.value=d;o.textContent=(d===daily.today?'Today · ':'')+dayLabel(d);opts.append(o)}opts.value=daily.day;opts.disabled=false;$('journalDate').textContent=(daily.day===daily.today?'Today’s journal · ':'')+dayLabel(daily.day);$('catCount').textContent=c.cats;$('savedCount').textContent=daily.saved_error?'Unavailable':c.saved;$('journalTitle').textContent=c.cats?'Little things, collected.':'The story is still unfolding.';$('journalText').textContent=c.cats?'Mina found likely cats in '+c.cats+' '+(c.cats===1?'recording.':'recordings.')+' Pick a moment below and jump straight to it.':'Finished recordings appear here when the detector finds a likely cat.';const link='/sightings/?day='+daily.day;$('allLink').href=link;$('reelLink').href=daily.reel||link;$('reelLink').textContent=daily.reel?'Watch this day’s reel →':'Explore the sightings →';$('reelNote').textContent=daily.reel?'A short film assembled from this day’s likely cat moments.':'The detector can miss cats or mistake other things for cats.';const chart=$('chart');chart.replaceChildren();const max=Math.max(1,...daily.hours.map(x=>x.cats));for(const h of daily.hours){const b=document.createElement('button'),bar=document.createElement('span');b.type='button';b.className='bar'+(h.cats?'':' zero');b.dataset.hour=h.hour;b.title=String(h.hour).padStart(2,'0')+':00 · '+h.cats+' likely cat clips';b.setAttribute('aria-label',b.title);b.style.setProperty('--height',Math.round(h.cats/max*100)+'%');b.onclick=()=>{selectedHour=selectedHour===h.hour?null:h.hour;renderMoments()};b.append(bar);chart.append(b)}renderMoments();const age=daily.scan_age_seconds===null?'No scan yet':daily.scan_age_seconds<60?'Scanned less than a minute ago':'Scanned '+Math.floor(daily.scan_age_seconds/60)+' min ago';$('detector').textContent=c.checked+' of '+c.recordings+' recordings checked · '+age+(c.pending?' · '+c.pending+' waiting':'')+(c.settling?' · '+c.settling+' still finishing':'')+(c.unreadable?' · '+c.unreadable+' unreadable':'')+(c.outside_scan_window?' · '+c.outside_scan_window+' outside scan window':'');const s=daily.storage;$('storage').textContent=s?gib(s.bytes)+' recorded · '+gib(s.free)+' free · '+(daily.saved_error&&s.retention_days?'cleanup paused':s.retention_days?'keep '+s.retention_days+' days':'cleanup off'):'Storage is currently unavailable';$('timeNote').textContent='Times follow Mina ('+daily.timezone+'). Counts are clips, not individual visits.';const warnings=[];if(daily.scan_stale)warnings.push('The detector has not reported a recent scan. Counts may be incomplete.');if(daily.saved_error)warnings.push('The saved list could not be read. Automatic cleanup is paused.');$('notice').textContent=warnings.join(' ');$('notice').hidden=!warnings.length;applyInsights();}
function applyInsights(){if(!daily||!insights||insights.day!==daily.day)return;const d=insights.diary,note=$('journalText');
if(d&&d.generated!=='template'){$('journalTitle').textContent=d.title;note.textContent=d.story;note.classList.add('from-diary');$('diaryLink').hidden=false;$('diaryLink').href='/insights/'+(daily.day===daily.today?'':'?day='+daily.day)}else{note.classList.remove('from-diary');$('diaryLink').hidden=!insights.ready}
const st=insights.status||{},cam=insights.camera||{},g=(st.guard||{}).events||[],reset=g.filter(e=>e.kind==='reset').pop();
const parts=[cam.online===true?'Camera online':cam.online===false?'Camera disconnected':'Camera status unknown'];
if(insights.ready)parts.push(st.model_state==='ready'?'diary model ready'+(st.waiting?' ('+st.waiting+' waiting)':''):'diary model: '+(st.model_state||'starting'));
if(reset)parts.push('last auto-reset '+new Date(reset.time).toLocaleString([],{weekday:'short',hour:'numeric',minute:'2-digit'}));
$('health').textContent=parts.join(' · ');}
async function loadInsights(day){const token=++insightRequest;try{const r=await fetch('/api/insights?day='+encodeURIComponent(day),{cache:'no-store'});if(!r.ok)return;const next=await r.json();if(token!==insightRequest)return;next.by={};for(const s of next.sightings)next.by[s.name]=s;insights=next;if(daily&&daily.day===next.day){renderMoments();applyInsights()}}catch(_){}}
async function loadDay(day='',quiet=false){const token=++dayRequest;dataController?.abort();const control=new AbortController();dataController=control;const timeout=setTimeout(()=>control.abort(),12000);if(!quiet)$('refresh').disabled=true;try{const r=await fetch('/api/day'+(day?'?day='+encodeURIComponent(day):''),{cache:'no-store',signal:control.signal});if(!r.ok)throw Error();const next=await r.json();if(token!==dayRequest)return;daily=next;followToday=followToday||daily.day===daily.today;render();loadInsights(daily.day);const u=new URL(location.href);if(daily.day===daily.today)u.searchParams.delete('day');else u.searchParams.set('day',daily.day);history.replaceState(null,'',u);}catch(_){if(token!==dayRequest)return;$('notice').hidden=false;$('notice').textContent='Could not refresh the journal. '+(daily?'Showing the last loaded day. ':'')+'Check Mina’s connection and try Refresh.';if(daily)$('day').value=daily.day;}finally{clearTimeout(timeout);if(token===dayRequest)$('refresh').disabled=false}}
$('day').onchange=()=>{selectedHour=null;followToday=$('day').value===daily?.today;loadDay($('day').value)};$('refresh').onclick=()=>loadDay(followToday?'':daily?.day||initialDay);$('resetHour').onclick=()=>{selectedHour=null;renderMoments()};
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopStream(true);dataController?.abort()}else{if(!pauseRequested)watch();loadDay(followToday?'':daily?.day||initialDay,true)}});
window.addEventListener('pagehide',()=>{stopStream(true);dataController?.abort()});window.addEventListener('pageshow',e=>{if(e.persisted){if(!pauseRequested)watch();loadDay(followToday?'':daily?.day||initialDay,true)}});
setInterval(()=>{if(!document.hidden)loadDay(followToday?'':daily?.day||initialDay,true)},60000);
loadDay(initialDay);watch();
</script></body></html>
""".encode("utf-8")


def size_label(size):
    for label in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or label == "GiB":
            return f"{size:.1f} {label}"
        size /= 1024


def clip_time(path, modified):
    try:
        return datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(modified)


@lru_cache(maxsize=1)
def owner_health_module(modified):
    source = Path.home() / "catcam-lounge" / "health_check.py"
    spec = importlib.util.spec_from_file_location("catcam_owner_health", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def retention_days():
    try:
        value = RETENTION.read_text().strip()
    except (OSError, UnicodeError):
        return None
    if re.fullmatch(r"[1-9]\d{0,3}", value):
        return int(value)
    return None


def expire_clips(directory, days):
    cutoff = time.time() - days * 86400
    # Coordinate cleanup with the viewer, so a newly saved clip is not removed.
    with SAVED_LOCK:
        try:
            saved = read_saved_sightings()
        except (OSError, ValueError) as error:
            print(f"Clip cleanup paused because saved sightings could not be read: {error}", flush=True)
            return
        for path in directory.glob("*.mp4"):
            if not MOTION_NAME.fullmatch(path.name) or path.name in saved:
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                print(f"Could not expire {path.name}: {error}", flush=True)


def recorded_files(directory):
    for path in directory.glob("*.mp4"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            yield path, info


def archive_page(directory, selected_day):
    days = {}
    total = count = old_size = 0
    keep_days = retention_days()
    try:
        saved = read_saved_sightings()
    except (OSError, ValueError):
        saved = None
    horizon = keep_days or 7
    cutoff = time.time() - horizon * 86400
    for path, info in recorded_files(directory):
        stamp = clip_time(path, info.st_mtime)
        days.setdefault(stamp.date().isoformat(), []).append((stamp, path.name, info.st_size))
        total += info.st_size
        count += 1
        if (info.st_mtime < cutoff and MOTION_NAME.fullmatch(path.name) and
                (saved is None or path.name not in saved)):
            old_size += info.st_size
    day = selected_day if selected_day in days else max(days, default="")
    options = "".join(f'<option {"selected" if d == day else ""}>{d}</option>'
                      for d in sorted(days, reverse=True))
    rows = []
    for stamp, name, size in sorted(days.get(day, [])):
        label = stamp.strftime("%I:%M:%S %p").lstrip("0")
        rows.append(f'<li><a class="visit" href="/clips/{quote(name, safe="")}">{label}</a>'
                    f'<span>{size_label(size)}</span></li>')
    free = shutil.disk_usage(directory).free
    if keep_days and saved is None:
        cleanup = "paused because the saved sightings list cannot be read"
    elif keep_days:
        cleanup = f"after {keep_days} days, excluding saved sightings"
    else:
        cleanup = "off"
    older_label = "Clips" if saved is None else "Unsaved clips"
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Recorded visits</title>
<style>
*{{box-sizing:border-box}}body{{max-width:780px;margin:2rem auto;padding:0 1rem;background:#151915;color:#ecf2ea;font:16px system-ui}}
header,form,li{{display:flex;align-items:center;gap:12px;justify-content:space-between}}
h1{{font-size:1.6rem}}a{{color:#b9eac3}}p,li span{{color:#c6cfca}}
header nav{{display:flex;gap:.8rem;flex-wrap:wrap;justify-content:flex-end}}
select,button{{font:inherit;padding:.45em .75em;border-radius:5px}}form{{flex-wrap:wrap;justify-content:flex-start}}
video{{width:100%;max-height:58vh;background:#101410;margin-top:1rem}}
ul{{padding:0}}li{{border-bottom:1px solid #39423b;padding:.45rem 0}}
.visit[aria-current]{{font-weight:bold;color:white}}
</style><header><h1>Recorded visits</h1><nav aria-label="Cat Cam">
<a href="/">Live camera</a><a href="/sightings/">Cat sightings</a><a href="/highlights/">Cat highlights</a></nav></header>
<p>{count} clips &middot; {size_label(total)} recorded &middot; {size_label(free)} free on mina</p>
<p>Automatic cleanup: {cleanup}.
{older_label} older than {horizon} days occupy {size_label(old_size)}.</p>"""
    if not rows:
        return (page + "<p>No recordings yet.</p></html>").encode()
    page += f"""<form><label>Day <select name="day">{options}</select></label><button>Show day</button></form>
<h2>Visits on {escape(day)}</h2><button type="button" id="play">Play day ({len(rows)} clips)</button>
<video controls playsinline hidden></video><p role="status" id="now"></p>
<a id="save" download hidden>Download this clip</a><ul>{''.join(rows)}</ul>"""
    page += """<script>
const clips = Array.from(document.querySelectorAll('.visit'));
const player = document.querySelector('video'), now = document.getElementById('now');
let current = 0, sequence = false;
function play(index) {
  clips[current].removeAttribute('aria-current');
  current = index;
  clips[index].setAttribute('aria-current','true');
  player.hidden = false;
  player.src = clips[index].href;
  const save = document.getElementById('save');
  save.href = clips[index].href; save.hidden = false;
  now.textContent = clips[index].textContent + ' (' + (index+1) + ' of ' + clips.length + ')';
  player.play().catch(()=>{});
}
document.getElementById('play').onclick = () => {sequence = true; play(0)};
clips.forEach((clip,i) => clip.onclick = e => {
  if(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault(); sequence = false; play(i);
});
player.onended = () => {if(sequence && current+1 < clips.length) play(current+1)};
player.onerror = () => {
  now.textContent = 'This clip is unavailable or still being written.';
  if(sequence && current+1 < clips.length) play(current+1);
};
</script></html>"""
    return page.encode()


def read_scan_state():
    try:
        state = json.loads(SCAN_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("version") == 1 else {}


def write_atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as output:
        staging = Path(output.name)
        try:
            output.write(data)
            output.close()
            staging.replace(path)
        finally:
            staging.unlink(missing_ok=True)


def highlights_page(selected_day):
    reels = {path.stem: info.st_size for path, info in recorded_files(REELS)
             if REEL_NAME.fullmatch(path.name)}
    day = selected_day if selected_day in reels else max(reels, default="")
    state = read_scan_state()
    if "checked" in state:
        status = f'{state["checked"]} recent clips checked · {state["cats"]} with likely cat sightings'
        if state.get("pending"):
            status += f' · {state["pending"]} queued'
    else:
        status = "Waiting for the first scan of finished clips."
    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>Cat highlights</title>
<style>
*{{box-sizing:border-box}}
body{{max-width:900px;margin:0 auto;padding:clamp(1rem,4vw,2.5rem);
background:#151915;color:#ecf2ea;font:16px/1.5 system-ui}}
header,nav,form,.reel-footer{{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}}
header{{justify-content:space-between;border-bottom:1px solid #384339;padding-bottom:1rem}}
h1{{font-size:clamp(1.65rem,5vw,2.25rem);line-height:1.1;margin:0}}
a{{color:#b9eac3;text-underline-offset:.22em}}
main{{margin-top:1.4rem}} p{{margin:.6rem 0;color:#c6d1c7}}
button,select{{font:inherit;padding:.4rem .55rem;border:1px solid #506654;
border-radius:.4rem;background:#26332a;color:inherit}}
.reel{{background:#202821;border:1px solid #3a473c;border-radius:.8rem;
padding:clamp(.75rem,3vw,1.2rem);margin-top:1.25rem}}
video{{display:block;width:100%;aspect-ratio:16/9;background:#101510;border-radius:.5rem}}
.reel-footer{{justify-content:space-between;margin-top:.65rem}}
</style></head><body>
<header><h1>Cat highlights</h1><nav aria-label="Cat Cam">
<a href="/">Live camera</a><a href="/insights/">Diary</a><a href="/sightings/">Cat sightings</a><a href="/clips/">Recorded visits</a></nav></header>
<main><p>{escape(status)}</p>"""
    if not day:
        return (page + '<section class="reel"><p>No reels yet. Cat Cam makes a reel when it finds a likely cat in a finished clip.</p></section></main></body></html>').encode()
    options = "".join(f'<option {"selected" if saved == day else ""}>{saved}</option>'
                      for saved in sorted(reels, reverse=True))
    return (page + f"""
<form><label>Reel date <select name="day">{options}</select></label><button>Show reel</button></form>
<section class="reel" aria-label="Highlights for {day}">
<video controls playsinline preload="metadata" src="/highlights/{day}.mp4"></video>
<div class="reel-footer"><a href="/highlights/{day}.mp4" download="cat-cam-{day}.mp4">Download reel</a>
<span>{size_label(reels[day])}</span></div></section></main></body></html>""").encode()


class FavoritesUnavailable(OSError):
    """Archive deletion and changes to favorites must stop until the index is readable."""


def read_saved_sightings(path=None):
    path = SAVED_SIGHTINGS if path is None else path
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("Favorites path is not a regular file")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FavoritesUnavailable("Favorites cannot be read. Automatic cleanup is paused.") from exc
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or not isinstance(data.get("items"), dict)):
        raise FavoritesUnavailable("Invalid favorites index. Automatic cleanup is paused.")
    items = data["items"]
    for name, record in items.items():
        if not MOTION_NAME.fullmatch(name) or not isinstance(record, dict):
            raise FavoritesUnavailable("Invalid favorite. Automatic cleanup is paused.")
        signature, moment = record.get("signature"), record.get("moment")
        if (not isinstance(signature, list) or len(signature) != 2 or
                any(type(n) is not int or n < 0 for n in signature) or not isinstance(moment, dict)):
            raise FavoritesUnavailable("Invalid favorite signature. Automatic cleanup is paused.")
        start = moment.get("start")
        if type(start) not in (int, float) or not 0 <= start <= 86400:
            raise FavoritesUnavailable("Invalid favorite timestamp. Automatic cleanup is paused.")
    return items


def favorites_backup():
    return SAVED_SIGHTINGS.with_name("saved-sightings.last-good.json")


def init_favorites(restore=False):
    """An existing library can only be initialized explicitly with cleanup switched off."""
    if retention_days() is not None:
        raise SystemExit("Turn cleanup off in Sightings before repairing the favorites list.")
    if SAVED_SIGHTINGS.exists():
        read_saved_sightings()
        return
    backup = favorites_backup()
    if not restore and backup.exists():
        raise SystemExit("A favorites backup exists. Use --restore-favorites instead.")
    items = read_saved_sightings(backup) if restore else {}
    SAVED_SIGHTINGS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation protects an existing list if another process won the race.
    with SAVED_SIGHTINGS.open("x", encoding="utf-8") as f:
        json.dump({"version": 1, "items": items}, f, allow_nan=False)
    print(f"Favorites list ready with {len(items)} starred recordings.", flush=True)


def backup_favorites(items):
    try:
        write_atomic(favorites_backup(), json.dumps({"version": 1, "items": items}, allow_nan=False).encode())
    except OSError as error:
        print(f"Could not refresh the favorites recovery copy: {error}", flush=True)


def save_sighting(name, keep):
    if not isinstance(name, str) or not MOTION_NAME.fullmatch(name) or type(keep) is not bool:
        raise ValueError("Choose a valid recording.")
    with SAVED_LOCK:
        saved = read_saved_sightings()
        if keep:
            records = read_scan_state().get("clips", {})
            record = records.get(name) if isinstance(records, dict) else None
            if not isinstance(record, dict) or not isinstance(record.get("moment"), dict):
                record = saved.get(name)
            if not isinstance(record, dict) or not isinstance(record.get("moment"), dict):
                raise FileNotFoundError("This sighting is no longer available.")
            info = (CLIPS / name).lstat()
            if not stat.S_ISREG(info.st_mode) or record.get("signature") != [info.st_size, info.st_mtime_ns]:
                raise FileNotFoundError("This recording is still changing.")
            moment = record["moment"]
            start = moment.get("start")
            if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400:
                raise ValueError("This sighting has an invalid timestamp.")
            stored_moment = {"start": start}
            detected = moment.get("detected_at")
            if type(detected) in (int, float) and math.isfinite(detected) and 0 <= detected <= 86400:
                stored_moment["detected_at"] = detected
            saved[name] = {"signature": [info.st_size, info.st_mtime_ns], "moment": stored_moment}
        else:
            saved.pop(name, None)
        HIGHLIGHT_DATA.mkdir(parents=True, exist_ok=True)
        write_atomic(SAVED_SIGHTINGS, json.dumps({"version": 1, "items": saved}, allow_nan=False).encode())
        backup_favorites(saved)
        return {"saved": keep, "total_saved": len(saved)}


def sighting_index(selected_day="", saved_only=False):
    """Expose the detector's cached results while verifying each source still exists."""
    state = read_scan_state()
    try:
        saved = read_saved_sightings()
        saved_error = False
    except (OSError, ValueError):
        saved = {}
        saved_error = True
    recent = state.get("clips", {})
    if not isinstance(recent, dict):
        recent = {}
    records = dict(saved)
    for name, record in recent.items():
        if name not in records or (isinstance(record, dict) and isinstance(record.get("moment"), dict)):
            records[name] = record
    days = {date.today().isoformat()}
    events = []
    for name, record in records.items():
        if not isinstance(name, str) or not MOTION_NAME.fullmatch(name) or not isinstance(record, dict):
            continue
        path = CLIPS / name
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or record.get("signature") != [info.st_size, info.st_mtime_ns]:
            continue
        stamp = clip_time(path, info.st_mtime)
        days.add(stamp.date().isoformat())
        moment = record.get("moment")
        if not isinstance(moment, dict):
            continue
        start = moment.get("start")
        if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400:
            continue
        events.append({
            "name": name,
            "timestamp": stamp.isoformat(timespec="seconds"),
            "offset": round(start, 3),
            "bytes": info.st_size,
            "thumbnail": "/sightings/thumb/" + quote(name, safe="") + ".jpg",
            "video": "/clips/" + quote(name, safe=""),
            "saved": name in saved,
        })
    ordered_days = sorted(days, reverse=True)
    selected = selected_day if selected_day in days else ("all" if saved_only else ordered_days[0])
    events = sorted((event for event in events if (not saved_only or event["saved"]) and
                     (selected == "all" or event["timestamp"][:10] == selected)),
                    key=lambda event: (event["timestamp"], event["name"]), reverse=True)
    try:
        storage = {"bytes": sum(info.st_size for _, info in recorded_files(CLIPS)),
                   "free": shutil.disk_usage(CLIPS).free, "retention_days": retention_days()}
    except OSError:
        storage = None
    return {
        "day": selected, "days": (["all"] if saved_only else []) + ordered_days, "events": events,
        "ready": bool(state), "checked": state.get("checked", 0),
        "cats": state.get("cats", 0), "pending": state.get("pending", 0),
        "updated_at": state.get("updated_at"), "saved_only": saved_only,
        "saved_total": len(saved), "saved_error": saved_error, "storage": storage,
    }


def day_overview(selected_day=""):
    """Summarize existing recordings, never infer visits or continuous occupancy."""
    today = date.today()
    if selected_day:
        if not isinstance(selected_day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", selected_day):
            raise ValueError("Choose a date in YYYY-MM-DD format.")
        chosen = date.fromisoformat(selected_day)
    else:
        chosen = today
    day = chosen.isoformat()
    now = time.time()
    state = read_scan_state()
    records = state.get("clips", {})
    if not isinstance(records, dict):
        records = {}
    index = sighting_index(day)
    # sighting_index can fall back to its newest date; a dashboard never does.
    events = [event for event in index["events"] if event["timestamp"][:10] == day]
    hours = [{"hour": hour, "cats": 0, "recordings": 0} for hour in range(24)]
    days = {today.isoformat(), day}
    total = checked = pending = unreadable = settling = archive = day_bytes = 0
    recent_cutoff = today - timedelta(days=SCAN_DAYS - 1)
    for path, info in recorded_files(CLIPS):
        if not MOTION_NAME.fullmatch(path.name):
            continue
        stamp = clip_time(path, info.st_mtime)
        days.add(stamp.date().isoformat())
        if stamp.date() != chosen:
            continue
        total += 1
        day_bytes += info.st_size
        hours[stamp.hour]["recordings"] += 1
        record = records.get(path.name)
        valid = isinstance(record, dict) and record.get("signature") == [info.st_size, info.st_mtime_ns]
        if valid:
            if record.get("unreadable"):
                unreadable += 1
            else:
                checked += 1
        elif info.st_mtime > now - 30:
            settling += 1
        elif stamp.date() < recent_cutoff:
            archive += 1
        else:
            pending += 1
    for event in events:
        hours[datetime.fromisoformat(event["timestamp"]).hour]["cats"] += 1
        event["link"] = "/sightings/?day=" + day + "&clip=" + quote(event["name"], safe="")
    reel = REELS / (day + ".mp4")
    reel_url = None
    try:
        if stat.S_ISREG(reel.lstat().st_mode) and reel.stat().st_size > 0:
            reel_url = "/highlights/?day=" + day
    except OSError:
        pass
    peak = max(hours, key=lambda item: item["cats"])
    timestamp = state.get("updated_at")
    scan_age = (max(0, now - timestamp) if type(timestamp) in (int, float)
                and math.isfinite(timestamp) and timestamp > 0 else None)
    return {
        "version": APP_VERSION, "day": day, "today": today.isoformat(),
        "days": sorted(days, reverse=True), "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": datetime.now().astimezone().tzname() or "Mina local time",
        "counts": {"recordings": total, "checked": checked, "pending": pending,
                   "settling": settling, "unreadable": unreadable, "outside_scan_window": archive,
                   "cats": len(events), "saved": sum(event["saved"] for event in events), "bytes": day_bytes},
        "hours": hours, "peak_hour": peak["hour"] if peak["cats"] else None,
        "events": events, "reel": reel_url, "storage": index["storage"],
        "saved_error": index["saved_error"], "scan_age_seconds": scan_age,
        "scan_stale": scan_age is None or scan_age > max(900, SCAN_INTERVAL * 5),
    }

def resolve_sighting(name):
    if not isinstance(name, str) or not MOTION_NAME.fullmatch(name):
        raise FileNotFoundError("Unknown recording")
    records = read_scan_state().get("clips", {})
    if not isinstance(records, dict):
        raise FileNotFoundError("No detector results")
    record = records.get(name)
    if not isinstance(record, dict) or not isinstance(record.get("moment"), dict):
        try:
            record = read_saved_sightings().get(name)
        except (OSError, ValueError):
            record = None
    if not isinstance(record, dict) or not isinstance(record.get("moment"), dict):
        raise FileNotFoundError("No sighting for this recording")
    moment = record["moment"]
    start = moment.get("start")
    if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400:
        raise FileNotFoundError("Invalid sighting")
    source = CLIPS / name
    info = source.lstat()
    signature = [info.st_size, info.st_mtime_ns]
    if not stat.S_ISREG(info.st_mode) or record.get("signature") != signature:
        raise FileNotFoundError("Recording is still changing")
    return source, signature, moment


def sighting_thumbnail(name):
    source, signature, moment = resolve_sighting(name)
    start = moment["start"]
    detected = moment.get("detected_at")
    if type(detected) not in (int, float) or not math.isfinite(detected) or detected < 0:
        detected = start + 2
    digest = hashlib.sha256(json.dumps([name, signature, detected]).encode()).hexdigest()[:24]
    cached = THUMBNAILS / (digest + ".jpg")
    with THUMBNAIL_LOCK:
        if not cached.is_file():
            THUMBNAILS.mkdir(parents=True, exist_ok=True)
            args = [
                "-v", "error", "-threads", "1", "-ss", f"{detected:.3f}", "-i", str(source),
                "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
                "-vf", "scale=640:360:force_original_aspect_ratio=decrease",
                "-frames:v", "1", "-q:v", "4", "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ]
            def extract(arguments):
                if shutil.which("nice"):
                    return run_media("nice", ["-n", "10", "ffmpeg", *arguments], timeout=30)
                return run_media("ffmpeg", arguments, timeout=30)
            image = extract(args)
            if not image:
                # An older, very short recording may finish before the preview offset.
                args[args.index("-ss") + 1] = f"{start:.3f}"
                image = extract(args)
            if not image.startswith(b"\xff\xd8"):
                raise ValueError("Could not make a thumbnail")
            # The detector's cached result no longer applies if the file changed.
            current = source.lstat()
            if not stat.S_ISREG(current.st_mode) or [current.st_size, current.st_mtime_ns] != signature:
                raise FileNotFoundError("Recording changed")
            write_atomic(cached, image)
        return cached.read_bytes()


def short_details(name):
    source, signature, moment = resolve_sighting(name)
    start = moment["start"]
    ident = hashlib.sha256(json.dumps([name, signature, start, 1]).encode()).hexdigest()[:24]
    return {"id": ident, "name": name, "source": source, "signature": signature, "start": start}


def read_short(ident):
    if not isinstance(ident, str) or not SHORT_NAME.fullmatch(ident):
        return None
    try:
        metadata_file = SHORTS / (ident + ".json")
        if not stat.S_ISREG(metadata_file.lstat().st_mode):
            return None
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        video = SHORTS / (ident + ".mp4")
        poster = SHORTS / (ident + ".jpg")
        video_info = video.lstat()
        if (not isinstance(metadata, dict) or metadata.get("id") != ident or
                not stat.S_ISREG(video_info.st_mode) or not stat.S_ISREG(poster.lstat().st_mode)):
            return None
        if (not isinstance(metadata.get("timestamp"), str) or not isinstance(metadata.get("name"), str)
                or not MOTION_NAME.fullmatch(metadata["name"])):
            return None
        datetime.fromisoformat(metadata["timestamp"])
        duration = metadata.get("duration")
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= SHORT_SECONDS + 1:
            return None
    except (OSError, ValueError):
        return None
    return {
        "id": ident, "name": metadata["name"], "timestamp": metadata["timestamp"],
        "offset": 0, "duration": duration, "bytes": video_info.st_size, "kind": "short",
        "video": "/shorts/" + ident + ".mp4", "thumbnail": "/shorts/" + ident + ".jpg",
    }


def shorts_index(selected_day=""):
    base = sighting_index()
    clips = [clip for path in SHORTS.glob("*.json")
             if SHORT_NAME.fullmatch(path.stem) and (clip := read_short(path.stem))]
    days = sorted({clip["timestamp"][:10] for clip in clips}, reverse=True)
    selected = selected_day if selected_day in days else "all"
    base.update({
        "day": selected, "days": ["all"] + days, "view": "shorts",
        "events": sorted((clip for clip in clips if selected == "all" or clip["timestamp"][:10] == selected),
                         key=lambda clip: (clip["timestamp"], clip["id"]), reverse=True),
    })
    return base


def short_status(name):
    details = short_details(name)
    ready = read_short(details["id"])
    if ready:
        return {"state": "ready", "clip": ready}
    with SHORT_LOCK:
        job = SHORT_JOBS.get(details["id"], {"state": "missing"})
        return {"state": "missing"} if job["state"] == "ready" else dict(job)


def render_short(details):
    SHORTS.mkdir(parents=True, exist_ok=True)
    source = details["source"]
    with tempfile.TemporaryDirectory(dir=SHORTS, prefix=".render-") as folder:
        work = Path(folder)
        video = work / "clip.mp4"
        args = [
            "-v", "error", "-threads", "1", "-ss", f'{details["start"]:.3f}', "-i", str(source),
            "-map", "0:v:0", "-t", str(SHORT_SECONDS), "-an", "-sn", "-dn", "-filter_threads", "1",
            "-vf", "fps=15,scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   "pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-threads", "1", "-movflags", "+faststart", str(video),
        ]
        if shutil.which("nice"):
            run_media("nice", ["-n", "10", "ffmpeg", *args], timeout=120)
        else:
            run_media("ffmpeg", args, timeout=120)
        duration = float(run_media("ffprobe", [
            "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video),
        ], timeout=15))
        if not math.isfinite(duration) or not 0 < duration <= SHORT_SECONDS + 1:
            raise ValueError("The short clip has an invalid duration")
        poster = work / "poster.jpg"
        poster_args = [
            "-v", "error", "-threads", "1", "-ss", f"{min(1.0, duration / 2):.3f}", "-i", str(video),
            "-map", "0:v:0", "-an", "-frames:v", "1", "-q:v", "4", "-threads", "1", str(poster),
        ]
        if shutil.which("nice"):
            run_media("nice", ["-n", "10", "ffmpeg", *poster_args], timeout=30)
        else:
            run_media("ffmpeg", poster_args, timeout=30)
        if not poster.is_file() or not video.is_file():
            raise ValueError("The short clip could not be completed")
        current = source.lstat()
        if not stat.S_ISREG(current.st_mode) or [current.st_size, current.st_mtime_ns] != details["signature"]:
            raise FileNotFoundError("The original recording changed")
        ident = details["id"]
        metadata = {
            "id": ident, "name": details["name"], "timestamp": clip_time(source, current.st_mtime).isoformat(timespec="seconds"),
            "duration": round(duration, 3), "created_at": time.time(),
        }
        video.replace(SHORTS / (ident + ".mp4"))
        poster.replace(SHORTS / (ident + ".jpg"))
        write_atomic(SHORTS / (ident + ".json"), json.dumps(metadata, allow_nan=False).encode())


def process_short_queue():
    while True:
        details = SHORT_QUEUE.get()
        ident = details["id"]
        with SHORT_LOCK:
            SHORT_JOBS[ident] = {"state": "working", "time": time.time()}
        try:
            render_short(details)
        except Exception as error:
            print(f"Could not create a short clip from {details['name']}: {error}", flush=True)
            with SHORT_LOCK:
                SHORT_JOBS[ident] = {"state": "error", "time": time.time()}
        else:
            with SHORT_LOCK:
                SHORT_JOBS[ident] = {"state": "ready", "time": time.time()}
        finally:
            SHORT_QUEUE.task_done()


def queue_short(name):
    global SHORT_WORKER
    details = short_details(name)
    ready = read_short(details["id"])
    if ready:
        return {"state": "ready", "clip": ready}
    with SHORT_LOCK:
        existing = SHORT_JOBS.get(details["id"])
        if existing and existing["state"] in {"queued", "working"}:
            return {"state": existing["state"]}
        # Finished jobs are represented by their saved metadata on disk.
        for key in list(SHORT_JOBS):
            item = SHORT_JOBS[key]
            if item["state"] in {"ready", "error"} and time.time() - item["time"] > 3600:
                del SHORT_JOBS[key]
        try:
            SHORT_QUEUE.put_nowait(details)
        except queue.Full:
            raise RuntimeError("The clip queue is full. Try again shortly.")
        SHORT_JOBS[details["id"]] = {"state": "queued", "time": time.time()}
        if SHORT_WORKER is None or not SHORT_WORKER.is_alive():
            SHORT_WORKER = threading.Thread(target=process_short_queue, name="catcam-short-clips", daemon=True)
            SHORT_WORKER.start()
        return {"state": "queued"}


def camera_status():
    try:
        with urlopen(SOURCE + "/stream?extra_headers=1", timeout=4) as response:
            for _ in range(40):
                line = response.readline(8193)
                if not line or len(line) > 8192:
                    break
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"x-ustreamer-online:"):
                    value = line.split(b":", 1)[1].strip().lower()
                    if value in (b"true", b"1"):
                        return {"online": True}
                    if value in (b"false", b"0"):
                        return {"online": False}
    except (OSError, HTTPException):
        pass
    return {"online": None}


def retention_preview(days):
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("Choose between 1 and 365 days.")
    cutoff = time.time() - days * 86400
    count = total = 0
    with SAVED_LOCK:
        saved = read_saved_sightings()
        for path, info in recorded_files(CLIPS):
            if MOTION_NAME.fullmatch(path.name) and path.name not in saved and info.st_mtime < cutoff:
                count += 1
                total += info.st_size
    return {"days": days, "clips": count, "bytes": total}


def set_retention(days):
    if days is not None and (type(days) is not int or not 1 <= days <= 365):
        raise ValueError("Choose between 1 and 365 days, or turn cleanup off.")
    with SAVED_LOCK:
        if days is None:
            RETENTION.unlink(missing_ok=True)
        else:
            # Refuse to enable cleanup if the protected list cannot be checked.
            read_saved_sightings()
            RETENTION.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(RETENTION, str(days).encode("ascii"))
    return {"retention_days": days}


def read_insights():
    try:
        data = json.loads(INSIGHTS_DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == 1 else {}


def read_cats():
    try:
        data = json.loads(CATS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cats = data.get("cats") if isinstance(data, dict) else None
    return [{"name": str(c.get("name", ""))[:40], "description": str(c.get("description", ""))[:160],
             "personality": str(c.get("personality", ""))[:200]}
            for c in (cats if isinstance(cats, list) else []) if isinstance(c, dict) and c.get("name")][:8]


def set_cats(cats):
    if not isinstance(cats, list) or len(cats) > 8:
        raise ValueError("Up to 8 cats")
    clean, seen = [], set()
    for cat in cats:
        if not isinstance(cat, dict):
            raise ValueError("Invalid cat")
        name = str(cat.get("name", "")).strip()
        description = str(cat.get("description", "")).strip()
        personality = str(cat.get("personality", "")).strip()
        if not CAT_NAME.fullmatch(name) or len(description) > 160 or name.lower() in seen \
                or name.lower() == "unknown" or not CAT_TEXT.fullmatch(description) \
                or not CAT_TEXT.fullmatch(personality):
            raise ValueError("Invalid cat name")
        seen.add(name.lower())
        clean.append({"name": name, "description": description, "personality": personality})
    CATS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(CATS_CONFIG, json.dumps({"version": 1, "cats": clean}).encode())
    return {"cats": clean}


def insight_label(record):
    names = [c.get("name") or c.get("appearance") or "a cat" for c in record.get("cats", []) if isinstance(c, dict)]
    return " & ".join(names) if names else ("a cat" if record.get("is_cat") else "maybe not a cat")


IDENTITY_DIR = Path.home() / ".local" / "share" / "catcam" / "identity"


def fix_cat(body):
    """A one-tap fix from the Diary page: this sighting is really CAT (or not a cat)."""
    sighting, cat = str(body.get("sighting", "")), str(body.get("cat", ""))
    names = {c["name"] for c in read_cats()}
    if not MOTION_NAME.fullmatch(sighting) or (cat not in names and cat != "none"):
        raise ValueError("Unknown sighting or cat")
    fixes = IDENTITY_DIR / "fixes"
    fixes.mkdir(parents=True, exist_ok=True)
    if len(list(fixes.glob("*.json"))) >= 50:
        raise ValueError("Too many fixes waiting")
    write_atomic(fixes / f"{time.time_ns()}-{os.urandom(3).hex()}.json",
                 json.dumps({"sighting": sighting, "cat": cat}).encode())
    return {"queued": True}


def insight_entry(name, record):
    ident = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    return {"name": name, "timestamp": record.get("timestamp", ""), "label": insight_label(record),
            "identity": ident.get("method", ""),
            "is_cat": bool(record.get("is_cat")), "activity": record.get("activity", ""),
            "spot": record.get("spot", ""), "caption": record.get("caption", ""),
            "named": [c["name"] for c in record.get("cats", []) if isinstance(c, dict) and c.get("name")]}


def insights_overview(selected_day=""):
    today = date.today().isoformat()
    if selected_day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", selected_day):
        raise ValueError("Choose a date in YYYY-MM-DD format.")
    day = date.fromisoformat(selected_day).isoformat() if selected_day else today
    data = read_insights()
    sightings = data.get("sightings") if isinstance(data.get("sightings"), dict) else {}
    diaries = data.get("diaries") if isinstance(data.get("diaries"), dict) else {}
    days = {today, day} | {d for d in diaries if isinstance(d, str)}
    days |= {r.get("timestamp", "")[:10] for r in sightings.values() if isinstance(r, dict) and r.get("timestamp")}
    entries = sorted((insight_entry(n, r) for n, r in sightings.items()
                      if isinstance(r, dict) and str(r.get("timestamp", "")).startswith(day)),
                     key=lambda e: e["timestamp"], reverse=True)
    diary = diaries.get(day) if isinstance(diaries.get(day), dict) else None
    if diary:
        diary = {k: diary.get(k) for k in ("title", "story", "best", "generated", "generated_at", "facts")}
    unsure = [insight_entry(n, sightings[n]) for n in data.get("identity_unsure", [])
              if isinstance(n, str) and isinstance(sightings.get(n), dict)][:8]
    return {"version": APP_VERSION, "day": day, "today": today, "days": sorted(days, reverse=True)[:60],
            "ready": bool(data), "unsure": unsure, "diary": diary, "sightings": entries, "stats": data.get("stats"),
            "status": data.get("status"), "cats": read_cats(), "camera": camera_status()}


def insights_captions():
    sightings = read_insights().get("sightings")
    if not isinstance(sightings, dict):
        return {}
    return {name: insight_entry(name, r) for name, r in sightings.items() if isinstance(r, dict)}





def studio_overview():
    try:
        data = json.loads((STUDIO / "drafts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    drafts = data.get("drafts") if isinstance(data, dict) and isinstance(data.get("drafts"), dict) else {}
    fields = ("id", "kind", "created", "origin", "day", "who", "status", "keep", "hook", "caption",
              "youtube_title", "hashtags", "duration", "error")
    listed = sorted(({k: d.get(k) for k in fields} for d in drafts.values() if isinstance(d, dict)
                     and STUDIO_ID.fullmatch(str(d.get("id", "")))), key=lambda d: d.get("created") or 0, reverse=True)
    for d in listed:
        d["who"] = d["who"] if isinstance(d["who"], list) else []
        d["hashtags"] = d["hashtags"] if isinstance(d["hashtags"], list) else []
        for key in ("hook", "caption", "youtube_title", "origin", "day", "error"):
            d[key] = str(d[key] or "")
    since = (date.today() - timedelta(days=2)).isoformat()
    recent = sorted((e for e in insights_captions().values() if e["is_cat"] and e["timestamp"] >= since),
                    key=lambda e: e["timestamp"], reverse=True)[:24]
    try:
        stored = json.loads(STUDIO_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    settings = {"handle": "", "auto_daily": True, "auto_weekly": True, "daily_time": "20:30"}
    if isinstance(stored, dict):
        settings.update({k: stored[k] for k in settings if k in stored})
    pending = len(list((STUDIO / "requests").glob("*.json"))) if (STUDIO / "requests").is_dir() else 0
    return {"version": APP_VERSION, "drafts": listed, "recent": recent, "settings": settings, "pending": pending}


def studio_request(body):
    action = body.get("action")
    if action == "settings":
        handle = str(body.get("handle", "")).strip()
        daily_time = str(body.get("daily_time", "20:30"))
        if not re.fullmatch(r"[@\w.\- ]{0,30}", handle) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", daily_time):
            raise ValueError("Invalid studio settings")
        STUDIO_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(STUDIO_SETTINGS, json.dumps({"handle": handle, "auto_daily": body.get("auto_daily") is True,
                                                  "auto_weekly": body.get("auto_weekly") is True,
                                                  "daily_time": daily_time}).encode())
        return {"saved": True}
    if action == "make":
        name = str(body.get("name", ""))
        if not MOTION_NAME.fullmatch(name) or not insights_captions().get(name, {}).get("is_cat"):
            raise ValueError("Choose a cat sighting")
        request = {"action": "make", "name": name}
    elif action in ("delete", "keep"):
        draft_id = str(body.get("id", ""))
        if not STUDIO_ID.fullmatch(draft_id):
            raise ValueError("Unknown draft")
        request = {"action": action, "id": draft_id, "keep": body.get("keep") is True}
    else:
        raise ValueError("Unknown studio action")
    queue = STUDIO / "requests"
    queue.mkdir(parents=True, exist_ok=True)
    if len(list(queue.glob("*.json"))) >= 20:
        raise ValueError("The studio is busy")
    write_atomic(queue / f"{time.time_ns()}-{os.urandom(3).hex()}.json", json.dumps(request).encode())
    return {"queued": True}





SIGHTINGS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#151d18"><title>Cat sightings</title>
<style>
:root{--bg:#151d18;--panel:#202d24;--line:#405246;--text:#eff1e7;--muted:#b3c1b3;--accent:#d1eb96;font:16px/1.45 system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}[hidden]{display:none!important}
button,select{font:inherit;color:inherit}button,select,a{outline-offset:4px}:focus-visible{outline:2px solid var(--accent)}
.wrap{width:min(1380px,100%);margin:auto;padding-inline:clamp(18px,4vw,56px)}header{border-bottom:1px solid #37483c}
.top{min-height:70px;display:flex;justify-content:space-between;align-items:center;gap:18px}
.brand{font-weight:750;letter-spacing:-.04em;font-size:1.25rem;color:var(--text);text-decoration:none}
.brand span{color:var(--accent);margin-right:.3em}nav{display:flex;gap:clamp(12px,2.5vw,28px);flex-wrap:wrap}
nav a{color:#d4dfd1;text-decoration:none;font-size:.86rem}nav a:hover{text-decoration:underline}
.camera-health{color:var(--muted);font-size:.77rem}.camera-health::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:#a3af9e;margin-right:6px}.camera-health[data-state=online]::before{background:#b9e985}.camera-health[data-state=offline]::before{background:#eca78f}
main{padding-block:clamp(26px,4vw,45px) 50px}.heading{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:24px}
.eyebrow{text-transform:uppercase;color:var(--accent);font:600 .7rem monospace;letter-spacing:.12em}
h1{font:400 clamp(2.8rem,5vw,5rem)/1 Georgia,serif;letter-spacing:-.055em;margin:.18em 0}
.sub{margin:0;color:var(--muted);font-size:.94rem}.controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
select,button.secondary{background:#293b2e;border:1px solid #5b7060;border-radius:7px;padding:.48rem .7rem}
select{max-width:190px}button{cursor:pointer}button:hover,select:hover{border-color:var(--accent)}
.views{display:flex;border:1px solid #536658;border-radius:7px;overflow:hidden}.views button{background:transparent;border:0;padding:.48rem .7rem}.views button[aria-pressed=true]{background:#d1eb96;color:#1b2a1e}
.layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(320px,.9fr);gap:16px;align-items:start}
.box{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;min-width:0}
.playerhead,.playerfoot,.listhead{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:15px 19px}
.playerhead strong,.listhead h2{margin:0;font-size:1rem;font-weight:650}.small{color:var(--muted);font-size:.79rem}
.screen{aspect-ratio:16/9;background:#0b100d;display:grid;place-items:center;position:relative}
.screen video{width:100%;height:100%;object-fit:contain;background:#0b100d}.empty{padding:1.5rem;text-align:center;color:var(--muted)}
.playerfoot{align-items:start;flex-wrap:wrap}.playerfoot p{margin:2px 0 0}.playerfoot strong{font-size:.95rem}
.playeractions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.playeractions button{font-size:.82rem}.playeractions button:disabled{opacity:.55;cursor:wait}
a.action{color:var(--accent);font-size:.85rem;text-underline-offset:4px}
.listing{padding:0 14px 14px}.listhead{padding-inline:5px}.items{list-style:none;margin:0;padding:0;max-height:65vh;overflow:auto}
.item{margin:0 0 5px;display:flex;align-items:center;gap:3px}.pick{flex:1;min-width:0;display:flex;align-items:center;gap:12px;text-align:left;background:transparent;border:1px solid transparent;border-radius:8px;padding:6px;color:inherit}
.pick[aria-pressed=true]{background:#304031;border-color:#75906b}.pick:hover{background:#293a2c}
.save{flex:none;width:38px;height:38px;border:1px solid #536658;border-radius:7px;background:transparent;color:#c4d0bf;font-size:1.25rem}.save[aria-pressed=true]{background:#d1eb96;color:#1b2a1e;border-color:#d1eb96}.save:disabled{opacity:.45}
.pick img,.thumb-placeholder{width:112px;aspect-ratio:16/9;object-fit:cover;background:#121b15;border-radius:5px;flex:none}
.pick strong{display:block;font-size:.95rem}.pick small{display:block;color:var(--muted);font-size:.77rem;margin-top:4px}
.listempty{color:var(--muted);font-size:.9rem;padding:20px 7px}.foot{margin-top:19px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:.8rem}
.storage-settings{margin-top:18px;padding:14px 18px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}.storage-settings summary{cursor:pointer;font-weight:650}.storage-settings p{color:var(--muted);font-size:.82rem;max-width:70ch}.storage-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
#notice{color:var(--accent);min-height:1.3em;margin:12px 0 0;font-size:.85rem}.warn{color:#f2c1a4!important}
@media(max-width:820px){.layout{grid-template-columns:1fr}.items{max-height:none}.heading{align-items:start;flex-direction:column}.top{padding-block:14px;align-items:start;flex-direction:column;gap:8px}.playerhead,.playerfoot{padding:13px 15px}}
@media(max-width:420px){.pick img,.thumb-placeholder{width:90px}h1{font-size:2.85rem}.controls{width:100%}}
</style></head><body>
<header><div class="wrap top"><a class="brand" href="/"><span aria-hidden="true">◒</span>Cat Cam</a>
<nav aria-label="Cat camera"><span class="camera-health" id="cameraHealth" data-state="unknown" role="status">Checking camera</span><a href="/">Live camera</a><a href="/insights/">Diary</a><a href="/studio/">Studio</a><a href="/highlights/">Daily reels</a><a href="/clips/">All recordings</a></nav></div></header>
<main class="wrap"><div class="heading"><div><div class="eyebrow">From mina’s camera</div><h1>Cat sightings.</h1>
<p class="sub">Find the moments the camera thinks contain a cat, then watch the original recording.</p></div>
<div class="controls"><div class="views" aria-label="Sighting filter"><button type="button" id="recent" aria-pressed="true">Recent</button><button type="button" id="saved" aria-pressed="false">Saved</button><button type="button" id="shorts" aria-pressed="false">Short clips</button></div><label for="day" class="small">Day</label><select id="day" aria-label="Sighting day" disabled><option>Loading…</option></select>
<button type="button" class="secondary" id="refresh">Refresh</button></div></div>
<div class="layout"><section class="box" aria-label="Sighting player"><div class="playerhead"><strong>Watch a sighting</strong><span class="small" id="playerStatus">Choose a clip</span></div>
<div class="screen"><video id="video" controls playsinline preload="metadata" hidden></video><div class="empty" id="videoEmpty">Select a sighting to watch the recording.</div></div>
<div class="playerfoot"><div><strong id="playing">Original recording</strong><p class="small" id="playingDetails">Playback starts near the detected moment.</p><p class="small" id="shortHint" hidden>Short clips are up to 12 seconds and are saved on mina.</p></div>
<div class="playeractions"><button id="makeShort" class="secondary" type="button" hidden>Make a short clip</button><button id="toStudio" class="secondary" type="button" hidden>Make a vertical post</button><a id="shortDownload" class="action" hidden download>Download short clip</a><a id="download" class="action" hidden download>Download recording</a></div></div></section>
<section class="box listing"><div class="listhead"><h2 id="listTitle">Likely cat clips</h2><span class="small" id="count"></span></div>
<ul class="items" id="items"></ul><p class="small" id="listHelp">The detector can miss cats or mistake other things for cats. Saved clips are protected from Cat Cam’s optional cleanup.</p></section></div>
<p id="notice" role="status" aria-live="polite"></p><div class="foot"><span id="scan">Waiting for the detector.</span><a class="action" href="/highlights/">Watch the daily reel →</a></div>
<div class="foot"><span id="storage"></span><a class="action" href="/clips/">View storage and recordings →</a></div>
<details class="storage-settings" id="storageSettings"><summary>Manage recording storage</summary>
<p>Choose how long to keep unsaved motion recordings. Cleanup permanently deletes older unsaved recordings. Saved sightings, daily reels and short clips are kept.</p>
<div class="storage-actions"><label for="retention" class="small">Keep unsaved recordings</label>
<select id="retention"><option value="off">Cleanup off</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option><option value="90">90 days</option></select>
<button type="button" class="secondary" id="applyRetention">Review change</button></div>
<p id="retentionHelp">The current setting is shown above. No change is made until you confirm it.</p></details>
</main><script>
const $=id=>document.getElementById(id);
const openingParams=new URLSearchParams(location.search);
let data=null,view=['saved','shorts'].includes(openingParams.get('view'))?openingParams.get('view'):'recent',active='',selectedEvent=null,sequence=0,requestSequence=0,ready=false;
let requestedClip=openingParams.get('clip')||'';
let readyShort=null,shortTimer=0,retentionDirty=false,updatingRetention=false,captions={};
const describe=event=>{const c=captions[event.name];return c?(c.is_cat?c.label+(c.activity?' · '+c.activity:''):'maybe not a cat'):''};
async function loadCaptions(){try{const r=await fetch('/api/insights/captions',{cache:'no-store'});if(!r.ok)return;captions=await r.json();if(data)render();if(selectedEvent&&captions[selectedEvent.name]&&captions[selectedEvent.name].caption&&selectedEvent.kind!=='short')$('playingDetails').textContent=captions[selectedEvent.name].caption+' · opens at '+offset(selectedEvent.offset)}catch(_){}}
const clock=stamp=>new Date(stamp).toLocaleTimeString([],{hour:'numeric',minute:'2-digit',second:'2-digit'});
const dayLabel=day=>new Date(day+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'short',day:'numeric',year:'numeric'});
const offset=s=>Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');
const sizeLabel=n=>n>=1073741824?(n/1073741824).toFixed(1)+' GiB':(n/1048576).toFixed(1)+' MiB';
const keyOf=event=>event.kind==='short'?event.id:event.name;
function note(message,bad=false){$('notice').textContent=message;$('notice').classList.toggle('warn',bad)}
function shortUI(state,clip){
  const b=$('makeShort');readyShort=state==='ready'?clip:null;
  b.disabled=state==='queued'||state==='working';
  b.textContent=state==='ready'?'Watch short clip':b.disabled?'Creating short clip…':state==='error'?'Try making clip again':'Make a short clip';
  $('shortDownload').hidden=!readyShort;
  if(readyShort){$('shortDownload').href=readyShort.video;$('shortDownload').download='catcam-'+readyShort.timestamp.replace(/[:T]/g,'-')+'.mp4'}
}
async function pollShort(event,token){
  try{const response=await fetch('/api/short-status?name='+encodeURIComponent(event.name),{cache:'no-store'});
    if(!response.ok)throw Error();const result=await response.json();if(token!==sequence)return;
    shortUI(result.state,result.clip);
    if(result.state==='queued'||result.state==='working')shortTimer=setTimeout(()=>pollShort(event,token),2000);
  }catch(_){if(token===sequence)shortUI('error')}
}
function openVideo(url,start,play,token){
  const player=$('video');
  const seek=()=>{if(token!==sequence)return;try{player.currentTime=start}catch(_){}
    if(play)player.play().catch(()=>note('Press play in the video to start.'))};
  if(player.dataset.source!==url){player.pause();player.dataset.source=url;player.src=url;player.addEventListener('loadedmetadata',seek,{once:true});player.load()}
  else if(player.readyState>=1)seek();else player.addEventListener('loadedmetadata',seek,{once:true});
}
function pick(event,play){
  active=keyOf(event);selectedEvent=event;const player=$('video'),token=++sequence;
  clearTimeout(shortTimer);readyShort=null;const isShort=event.kind==='short';
  $('videoEmpty').hidden=true;player.hidden=false;
  $('playing').textContent=(isShort?'Short clip from ':'Clip started ')+clock(event.timestamp);
  $('playingDetails').textContent=isShort?'A '+Math.round(event.duration)+'-second clip saved on mina.':(captions[event.name]&&captions[event.name].caption?captions[event.name].caption+' · opens at '+offset(event.offset):'Opening at '+offset(event.offset)+' into the original recording.');
  $('shortHint').hidden=isShort;
  $('playerStatus').textContent=dayLabel(event.timestamp.slice(0,10));
  $('download').hidden=false;$('download').href=event.video;$('download').textContent=isShort?'Download short clip':'Download recording';
  $('download').download=isShort?'catcam-'+event.timestamp.replace(/[:T]/g,'-')+'.mp4':event.name;
  $('makeShort').hidden=isShort;$('shortDownload').hidden=true;$('toStudio').hidden=isShort||!(captions[event.name]&&captions[event.name].is_cat);$('toStudio').disabled=false;$('toStudio').textContent='Make a vertical post';
  document.querySelectorAll('.pick').forEach(b=>b.setAttribute('aria-pressed',b.dataset.key===active));
  const permalink=new URL(location.href);permalink.searchParams.set('view',view);
  if(data?.day)permalink.searchParams.set('day',data.day);
  permalink.searchParams.set('clip',keyOf(event));history.replaceState(null,'',permalink);
  openVideo(event.video,event.offset,play,token);
  if(!isShort){shortUI('missing');pollShort(event,token)}
}
function render(){
  const options=$('day');options.replaceChildren();
  for(const day of data.days){const o=document.createElement('option');o.value=day;o.textContent=day==='all'?'All dates':dayLabel(day);options.append(o)}
  options.value=data.day;options.disabled=false;const list=$('items');list.replaceChildren();
  for(const name of ['recent','saved','shorts'])$(name).setAttribute('aria-pressed',view===name);
  $('saved').textContent='Saved'+(data.saved_total?' ('+data.saved_total+')':'');
  $('listTitle').textContent=view==='shorts'?'Short clips':view==='saved'?'Saved sightings':'Likely cat clips';
  $('listHelp').textContent=view==='shorts'?'Short clips stay here even if the original recording is cleaned up.':'The detector can miss cats or mistake other things for cats. Saved clips are protected from Cat Cam’s optional cleanup.';
  $('count').textContent=data.events.length+' '+(data.events.length===1?'clip':'clips');
  for(const event of data.events){
    const isShort=event.kind==='short',key=keyOf(event),li=document.createElement('li'),button=document.createElement('button'),image=document.createElement('img'),text=document.createElement('span'),title=document.createElement('strong'),detail=document.createElement('small');
    li.className='item';button.className='pick';button.dataset.key=key;button.type='button';
    button.setAttribute('aria-label',(isShort?'Play short from ':'Play recording started at ')+clock(event.timestamp));button.setAttribute('aria-pressed',key===active);
    image.src=event.thumbnail;image.alt='';image.loading='lazy';image.decoding='async';
    image.onerror=()=>{image.removeAttribute('src');image.className='thumb-placeholder';image.onerror=null};
    title.textContent=clock(event.timestamp)+(describe(event)?' · '+describe(event):'');detail.textContent=(data.day==='all'?dayLabel(event.timestamp.slice(0,10))+' · ':'')+(isShort?Math.round(event.duration)+' seconds':(captions[event.name]&&captions[event.name].caption)||'open at '+offset(event.offset));
    text.append(title,detail);button.append(image,text);button.onclick=()=>pick(event,true);li.append(button);
    if(!isShort){const save=document.createElement('button');save.type='button';save.className='save';save.textContent=event.saved?'★':'☆';save.disabled=data.saved_error;
      save.setAttribute('aria-label',(event.saved?'Remove saved sighting from ':'Save sighting from ')+clock(event.timestamp));save.setAttribute('aria-pressed',event.saved);
      save.title=event.saved?'Remove from saved sightings':'Save this sighting';save.onclick=()=>setSaved(event,save);li.append(save)}
    list.append(li)
  }
  if(!data.events.length){
    const li=document.createElement('li');li.className='listempty';
    li.textContent=view==='shorts'?'No short clips yet. Choose a sighting and select Make a short clip.':view==='saved'?'No saved sightings for this selection.':data.ready?'No likely cat sightings for this day yet.':'The detector is preparing its first scan.';list.append(li);
    active='';selectedEvent=null;sequence++;clearTimeout(shortTimer);$('video').pause();$('video').removeAttribute('src');$('video').load();$('video').hidden=true;$('videoEmpty').hidden=false;$('download').hidden=true;$('makeShort').hidden=true;$('shortDownload').hidden=true;$('shortHint').hidden=true;$('playing').textContent='Original recording';$('playingDetails').textContent='Playback starts near the detected moment.';$('playerStatus').textContent='Choose a clip'
  }else if(!data.events.some(event=>keyOf(event)===active)){
    const linked=requestedClip?data.events.find(event=>keyOf(event)===requestedClip):null;
    if(requestedClip&&!linked){note('That clip is no longer in this view. Showing the available sightings.',true)}
    pick(linked||data.events[0],false);requestedClip='';
  }
  const queued=data.pending?' · '+data.pending+' queued':'';
  const when=data.updated_at?' · last scan '+new Date(data.updated_at*1000).toLocaleString():'';
  $('scan').textContent=data.ready?data.checked+' recent clips checked · '+data.cats+' with likely cats'+queued+when:'Waiting for the first scan of finished clips.';
  if(data.storage){
    const s=data.storage,cleanup=data.saved_error&&s.retention_days?'cleanup paused':s.retention_days?'cleanup after '+s.retention_days+' days':'cleanup off';
    $('storage').textContent=sizeLabel(s.bytes)+' recorded · '+sizeLabel(s.free)+' free on mina · '+cleanup;
    if(!retentionDirty&&!updatingRetention){const value=s.retention_days?String(s.retention_days):'off';
      if(!Array.from($('retention').options).some(o=>o.value===value)){const option=document.createElement('option');option.value=value;option.textContent=value+' days (current)';$('retention').append(option)}
      $('retention').value=value}
  }
}
async function setSaved(event,button){
  button.disabled=true;
  try{const response=await fetch('/api/sightings/saved',{method:'POST',
      headers:{'Content-Type':'application/json','X-Catcam-Action':'save-sighting'},
      body:JSON.stringify({name:event.name,saved:!event.saved})});
    if(!response.ok)throw Error('Could not update saved sightings.');
    await load(data.day,true);note(event.saved?'Removed from saved sightings.':'Sighting saved on mina.');
  }catch(error){note(error.message,true);button.disabled=false}
}
async function load(day,quiet=false){
  const token=++requestSequence,params=new URLSearchParams(),requestedView=view;
  if(day)params.set('day',day);if(requestedView==='saved')params.set('saved','1');
  if(!quiet){$('refresh').disabled=true;note('Checking sightings…')}
  try{const query=params.toString(),endpoint=requestedView==='shorts'?'/api/shorts':'/api/sightings';
    const r=await fetch(endpoint+(query?'?'+query:''),{cache:'no-store'});
    if(!r.ok)throw Error('Could not load sightings.');const next=await r.json();if(token!==requestSequence)return;
    data=next;render();ready=true;if(data.saved_error)note('The saved sightings list could not be read. Cleanup is paused.',true);else if(!quiet)note('Sightings updated.');
  }catch(error){if(token===requestSequence&&(!quiet||!ready))note('Could not load sightings. Try Refresh.',true)}
  finally{if(token===requestSequence)$('refresh').disabled=false}
}
async function makeShort(){
  if(!selectedEvent||selectedEvent.kind==='short')return;
  if(readyShort){const token=++sequence;clearTimeout(shortTimer);$('playing').textContent='Short clip from '+clock(readyShort.timestamp);
    $('playingDetails').textContent='A '+Math.round(readyShort.duration)+'-second clip saved on mina.';openVideo(readyShort.video,0,true,token);return}
  const event=selectedEvent,token=sequence;shortUI('queued');
  try{const response=await fetch('/api/shorts',{method:'POST',
      headers:{'Content-Type':'application/json','X-Catcam-Action':'make-short'},body:JSON.stringify({name:event.name})});
    if(!response.ok)throw Error('Could not make a short clip. Try again.');
    const result=await response.json();if(token!==sequence)return;
    shortUI(result.state,result.clip);if(result.state==='queued'||result.state==='working')shortTimer=setTimeout(()=>pollShort(event,token),1000);
  }catch(error){if(token===sequence){shortUI('error');note(error.message,true)}}
}
async function refreshCamera(){
  try{const r=await fetch('/api/camera-status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();
    const state=s.online===true?'online':s.online===false?'offline':'unknown';$('cameraHealth').dataset.state=state;
    $('cameraHealth').textContent=state==='online'?'Camera online':state==='offline'?'Camera disconnected':'Camera unavailable';
  }catch(_){$('cameraHealth').dataset.state='unknown';$('cameraHealth').textContent='Camera unavailable'}
}
async function applyRetention(){
  if(!data||!data.storage||updatingRetention)return;
  const value=$('retention').value,days=value==='off'?null:Number(value);
  if(days===data.storage.retention_days){retentionDirty=false;note('That cleanup setting is already active.');return}
  updatingRetention=true;$('applyRetention').disabled=true;
  try{
    if(days!==null){const response=await fetch('/api/retention-preview?days='+encodeURIComponent(days),{cache:'no-store'});
      if(!response.ok)throw Error('Could not preview cleanup. Check that saved sightings are available.');
      const preview=await response.json(),message='Turn on '+days+'-day cleanup?\\n\\nCat Cam will permanently delete unsaved motion recordings last modified more than '+days+' days ago. About '+preview.clips+' clips ('+sizeLabel(preview.bytes)+') currently qualify. Cleanup will continue automatically.\\n\\nSaved sightings, daily reels and short clips are kept.';
      if(!window.confirm(message)){retentionDirty=false;$('retention').value=data.storage.retention_days?String(data.storage.retention_days):'off';note('Cleanup setting unchanged.');return}
    }
    const response=await fetch('/api/retention',{method:'POST',headers:{'Content-Type':'application/json','X-Catcam-Action':'set-retention'},body:JSON.stringify({days})});
    if(!response.ok)throw Error('Could not change the cleanup setting.');
    retentionDirty=false;await load(data.day,true);note(days===null?'Automatic cleanup is off.':'Automatic cleanup is set to '+days+' days.');
  }catch(error){note(error.message,true)}finally{updatingRetention=false;$('applyRetention').disabled=false}
}
$('day').onchange=()=>load($('day').value);
$('refresh').onclick=()=>load(data&&data.day);
for(const mode of ['recent','saved','shorts'])$(mode).onclick=()=>{if(view!==mode){view=mode;load('')}};
$('makeShort').onclick=makeShort;
$('retention').onchange=()=>{retentionDirty=true};
$('applyRetention').onclick=applyRetention;
if(location.hash==='#storageSettings')$('storageSettings').open=true;
$('video').onerror=()=>{if(!$('video').hidden)note('This recording is unavailable or still finishing.',true)};
$('toStudio').onclick=async()=>{const b=$('toStudio');if(!selectedEvent)return;b.disabled=true;try{const r=await fetch('/api/studio',{method:'POST',headers:{'Content-Type':'application/json','X-Catcam-Action':'studio'},body:JSON.stringify({action:'make',name:selectedEvent.name})});if(!r.ok)throw Error();b.textContent='Sent to the Studio ✓';note('A vertical post is being made. Find it on the Studio page in a minute or two.')}catch(_){b.disabled=false;note('Could not send this to the Studio.',true)}};
load(openingParams.get('day')||'');refreshCamera();loadCaptions();setInterval(()=>{if(!document.hidden)loadCaptions()},120000);
setInterval(()=>{if(data&&!document.hidden)load(data.day,true)},90000);
setInterval(()=>{if(!document.hidden)refreshCamera()},30000);
</script></body></html>""".encode("utf-8")


class CatDetector:
    def __init__(self):
        import numpy
        import onnxruntime

        model = STRONG_MODEL if STRONG_MODEL.is_file() else MODEL
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.side = int(self.session.get_inputs()[0].shape[2])
        self.name = model.stem
        self.numpy = numpy

    def score(self, frame):
        # YOLOX uses a BGR image padded on the bottom and right, with values 0–255.
        batch = frame.transpose(2, 0, 1)[None].astype(self.numpy.float32)
        predictions = self.session.run(None, {self.input_name: batch})[0][0]
        class_scores = predictions[:, 5:]
        if class_scores.shape[1] != 80:
            raise ValueError("Expected the YOLOX COCO model with 80 classes")
        cats = predictions[class_scores.argmax(axis=1) == CAT_CLASS]
        return float(self.numpy.max(cats[:, 4] * cats[:, 5 + CAT_CLASS], initial=0.0))


def run_media(program, args, timeout=90, cwd=None):
    result = subprocess.run(
        [program, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd=cwd
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(message[-650:] or f"{program} exited with code {result.returncode}")
    return result.stdout


def find_cat_moment(path, detector):
    duration = float(run_media("ffprobe", [
        "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Missing clip duration")
    interval = max(2, math.ceil(duration / 60))
    side = getattr(detector, "side", SAMPLE_SIZE)
    samples = run_media("ffmpeg", [
        "-v", "error", "-xerror", "-threads", "1", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
        "-vf", f"fps=1/{interval}:start_time=0:eof_action=pass,"
        f"scale={side}:{side}:force_original_aspect_ratio=decrease,"
        f"pad={side}:{side}:0:0:color=0x727272",
        "-frames:v", "61", "-pix_fmt", "bgr24", "-threads", "1", "-f", "rawvideo", "pipe:1",
    ])
    frames = detector.numpy.frombuffer(samples, dtype=detector.numpy.uint8).reshape(-1, side, side, 3)
    if not len(frames):
        raise ValueError("Clip has no decodable frames")
    best_score, best_time = 0.0, 0
    for index, frame in enumerate(frames):
        score = detector.score(frame)
        if score > best_score and math.isfinite(score):
            best_score, best_time = score, index * interval
    if best_score < CAT_THRESHOLD:
        return None
    # Include the approach and leave room for the full excerpt near the end of a clip.
    return {"score": round(best_score, 4),
            "start": max(0, min(best_time - 2, duration - MOMENT_SECONDS)),
            "detected_at": max(0, min(best_time, duration - 0.15))}


def render_reel(out, moments):
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".reel-") as folder:
        staging = Path(folder)
        parts = []
        for number, (path, start) in enumerate(moments):
            name = f"part-{number:02d}.mp4"
            run_media("ffmpeg", [
                "-v", "error", "-xerror", "-threads", "1", "-ss", f"{start:.3f}",
                "-i", str(path), "-map", "0:v:0", "-t", str(MOMENT_SECONDS), "-an",
                "-filter_threads", "1", "-vf",
                "fps=15,scale=640:360:force_original_aspect_ratio=decrease,"
                "pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "24", "-maxrate", "1500k", "-bufsize", "3000k",
                "-threads", "1", str(staging / name),
            ])
            parts.append(f"file '{name}'\n")
        playlist = staging / "parts.txt"
        playlist.write_text("".join(parts), encoding="ascii")
        movie = staging / "reel.mp4"
        run_media("ffmpeg", [
            "-v", "error", "-f", "concat", "-safe", "1", "-i", playlist.name,
            "-c", "copy", "-movflags", "+faststart", movie.name,
        ], cwd=staging)
        if movie.stat().st_size == 0:
            raise RuntimeError("FFmpeg created an empty reel")
        movie.replace(out)


def scan_highlights(detector):
    HIGHLIGHT_DATA.mkdir(parents=True, exist_ok=True)
    REELS.mkdir(parents=True, exist_ok=True)
    state = read_scan_state()
    previous = state.get("clips", {})
    rendered = state.get("rendered", {})
    earliest_day = date.today() - timedelta(days=SCAN_DAYS - 1)
    detector_id = getattr(detector, "name", "yolox_nano")
    now = time.time()
    settled_before = now - 30
    records = {}
    sightings = {}
    processed = pending = errors = retried = 0
    files = sorted(recorded_files(CLIPS), reverse=True)
    for path, info in files:
        if not MOTION_NAME.fullmatch(path.name) or info.st_mtime > settled_before:
            continue
        stamp = clip_time(path, info.st_mtime)
        if stamp.date() < earliest_day:
            continue
        signature = [info.st_size, info.st_mtime_ns]
        record = previous.get(path.name)
        same_bytes = isinstance(record, dict) and record.get("signature") == signature
        unchanged = same_bytes and record.get("detector", "yolox_nano") == detector_id
        upgrade = same_bytes and not unchanged
        # A decoder may fail transiently. Recheck a stable file once, within
        # the existing scan budget. Changing bytes always start a new attempt.
        retry_at = record.get("retry_after", info.st_mtime + 900) if unchanged else None
        retry = (unchanged and record.get("unreadable") and not record.get("retried")
                 and type(retry_at) in (int, float) and now >= retry_at and retried < 2)
        if not unchanged and processed >= SCAN_BATCH:
            pending += 1
            if not upgrade:
                continue
        if (not unchanged or retry) and processed < SCAN_BATCH:
            processed += 1
            retried += int(bool(retry))
            try:
                moment = find_cat_moment(path, detector)
                record = {"signature": signature, "moment": moment, "detector": detector_id}
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                record = {"signature": signature, "moment": None, "unreadable": True,
                          "retry_after": now + 900, "retried": bool(retry), "detector": detector_id}
                print(f"Could not scan {path.name}: {error}", flush=True)
        records[path.name] = record
        errors += bool(record.get("unreadable"))
        if record["moment"] is not None:
            sightings.setdefault(stamp.date().isoformat(), []).append((path, record))
    for day, clips in sightings.items():
        ranked = sorted(clips, key=lambda item: item[1]["moment"]["score"], reverse=True)
        selected = sorted(ranked[:REEL_MOMENTS])
        moments = [(path, record["moment"]["start"]) for path, record in selected]
        versions = [[path.name, record["signature"], record["moment"]] for path, record in selected]
        destination = REELS / f"{day}.mp4"
        if rendered.get(day) == versions and destination.is_file():
            continue
        try:
            render_reel(destination, moments)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"Could not build the {day} reel: {error}", flush=True)
        else:
            rendered[day] = versions
    found = sum(len(clips) for clips in sightings.values())
    write_atomic(SCAN_STATE, json.dumps({
        "version": 1, "clips": records, "rendered": {day: rendered[day] for day in sightings if day in rendered},
        "checked": len(records) - errors, "cats": found, "pending": pending,
        "updated_at": time.time(),
    }).encode())
    print(f"Cat highlights checked {len(records) - errors} clips, found {found} with likely cats.", flush=True)


class Viewer(SimpleHTTPRequestHandler):
    def clip(self, path):
        name = unquote(path[len("/clips/"):])
        if not name.endswith(".mp4") or any(c in name for c in ("/", "\\", "\x00")):
            self.send_error(404)
            return
        self.mp4(Path(self.directory, name))

    def reel(self, path):
        name = path[len("/highlights/"):]
        if not REEL_NAME.fullmatch(name):
            self.send_error(404)
            return
        self.mp4(REELS / name)

    def mp4(self, file):
        if file.is_symlink() or not file.is_file():
            self.send_error(404)
            return
        try:
            media = file.open("rb")
        except OSError:
            self.send_error(404)
            return
        with media:
            size = os.fstat(media.fileno()).st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", requested)
                if match and any(match.groups()):
                    first, last = match.groups()
                    start = int(first) if first else max(0, size - int(last))
                    end = min(end, int(last)) if first and last else end
                if not match or not any(match.groups()) or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end-start+1))
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                media.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = media.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path.startswith("/clips/") and path.endswith(".mp4"):
            self.clip(path)
        elif path.startswith("/highlights/") and path.endswith(".mp4"):
            self.reel(path)
        elif path.startswith("/sightings/thumb/") and path.endswith(".jpg"):
            self.sighting_image(path)
        elif path.startswith("/shorts/"):
            self.short_media(path)
        elif path.startswith("/studio/media/"):
            self.studio_media(path)
        elif path in {"/", "/storage/", "/clips/", "/highlights/", "/sightings/", "/api/sightings",
                      "/api/shorts", "/api/short-status", "/api/camera-status", "/api/retention-preview", "/api/day",
                      "/insights/", "/api/insights", "/api/insights/captions", "/studio/", "/api/studio",
                      "/camera/", "/api/camera"}:
            self.do_GET()
        else:
            self.send_error(405)

    def sighting_image(self, path):
        name = unquote(path[len("/sightings/thumb/"):-len(".jpg")])
        try:
            data = sighting_thumbnail(name)
            self.reply(data, "image/jpeg")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FileNotFoundError:
            self.send_error(404)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            self.send_error(503, "Thumbnail unavailable")

    def studio_media(self, path):
        match = re.fullmatch(r"/studio/media/([0-9a-f]{16})\.(mp4|jpg)", path)
        if not match:
            self.send_error(404)
            return
        file = STUDIO / (match.group(1) + "." + match.group(2))
        if match.group(2) == "mp4":
            self.mp4(file)
            return
        try:
            if file.is_symlink():
                raise OSError
            self.reply(file.read_bytes(), "image/jpeg")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.send_error(404)

    def short_media(self, path):
        match = re.fullmatch(r"/shorts/([0-9a-f]{24})\.(mp4|jpg)", path)
        if not match or not read_short(match.group(1)):
            self.send_error(404)
            return
        file = SHORTS / (match.group(1) + "." + match.group(2))
        if match.group(2) == "mp4":
            self.mp4(file)
        else:
            try:
                self.reply(file.read_bytes(), "image/jpeg")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_error(404)

    def live(self):
        try:
            upstream = urlopen(SOURCE + "/stream?extra_headers=1", timeout=5)
        except (OSError, HTTPException):
            self.send_error(503, "Camera unavailable")
            return
        with upstream:
            try:
                self.send_response(200)
                # The viewer reads the multipart body itself to detect stalled frames.
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := upstream.read1(65536):
                    self.wfile.write(chunk)
            except (OSError, HTTPException):
                pass

    def reply(self, content, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.reply(PAGE, "text/html; charset=utf-8")
        elif path == "/storage/":
            source = Path.home() / "catcam-lounge" / "health_check.py"
            try:
                dashboard = owner_health_module(source.stat().st_mtime_ns)
                self.reply(dashboard.page(), "text/html; charset=utf-8")
            except (OSError, ImportError, ValueError, AttributeError):
                self.send_error(503, "Owner storage report is unavailable")
        elif path == "/live":
            self.live()
        elif path == "/frame":
            try:
                with urlopen(SOURCE + "/snapshot", timeout=2.5) as frame:
                    if frame.headers.get("X-UStreamer-Online", "").lower() in {"false", "0"}:
                        raise ValueError("Camera offline")
                    data = frame.read()
                    if not data.startswith(b"\xff\xd8"):
                        raise ValueError("Waiting for camera frame")
                self.reply(data, "image/jpeg")
            except (URLError, TimeoutError, ValueError):
                self.send_error(503, "Camera unavailable")
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/clips/":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            self.reply(archive_page(Path(self.directory), day), "text/html; charset=utf-8")
        elif path == "/sightings/":
            self.reply(SIGHTINGS_PAGE, "text/html; charset=utf-8")
        elif path in ("/insights", "/diary", "/diary/"):
            self.send_response(302)
            self.send_header("Location", "/insights/")
            self.end_headers()
        elif path == "/insights/":
            self.reply(page("insights.html"), "text/html; charset=utf-8")
        elif path == "/camera":
            self.send_response(302)
            self.send_header("Location", "/camera/")
            self.end_headers()
        elif path == "/camera/":
            self.reply(page("camera.html"), "text/html; charset=utf-8")
        elif path == "/api/camera":
            self.reply(json.dumps(camera_settings(), allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path.startswith("/insights/crop/"):
            match = re.fullmatch(r"/insights/crop/(\d{8}-\d{6}-\d+\.mp4)\.jpg", path)
            crop = IDENTITY_DIR / "crops" / (match.group(1) + ".jpg") if match else None
            try:
                if not crop or crop.is_symlink():
                    raise OSError
                self.reply(crop.read_bytes(), "image/jpeg")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_error(404)
        elif path == "/camera/preview.jpg":
            preview = FRAME_DIR / "full.jpg"
            try:
                if preview.is_symlink() or time.time() - preview.stat().st_mtime > 10:
                    raise OSError
                self.reply(preview.read_bytes(), "image/jpeg")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_error(404, "No preview running")
        elif path == "/studio":
            self.send_response(302)
            self.send_header("Location", "/studio/")
            self.end_headers()
        elif path == "/studio/":
            self.reply(page("studio.html"), "text/html; charset=utf-8")
        elif path == "/api/studio":
            self.reply(json.dumps(studio_overview(), allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path.startswith("/studio/media/"):
            self.studio_media(path)
        elif path == "/api/insights":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            try:
                self.reply(json.dumps(insights_overview(day), allow_nan=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except (TypeError, ValueError):
                self.send_error(400, "Choose a valid diary date")
        elif path == "/api/insights/captions":
            self.reply(json.dumps(insights_captions(), allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/day":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            try:
                data = json.dumps(day_overview(day), allow_nan=False).encode("utf-8")
                self.reply(data, "application/json; charset=utf-8")
            except (TypeError, ValueError):
                self.send_error(400, "Choose a valid journal date")
        elif path == "/api/sightings":
            query = parse_qs(urlsplit(self.path).query)
            day = query.get("day", [""])[0]
            data = json.dumps(sighting_index(day, query.get("saved") == ["1"]), allow_nan=False).encode("utf-8")
            self.reply(data, "application/json; charset=utf-8")
        elif path == "/api/shorts":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            self.reply(json.dumps(shorts_index(day), allow_nan=False).encode(), "application/json; charset=utf-8")
        elif path == "/api/short-status":
            name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
            try:
                self.reply(json.dumps(short_status(name), allow_nan=False).encode(), "application/json; charset=utf-8")
            except (OSError, ValueError):
                self.send_error(404, "Sighting unavailable")
        elif path == "/api/camera-status":
            self.reply(json.dumps(camera_status()).encode(), "application/json; charset=utf-8")
        elif path == "/api/retention-preview":
            try:
                value = parse_qs(urlsplit(self.path).query).get("days", [""])[0]
                if not value.isascii() or not value.isdecimal() or len(value) > 3:
                    raise ValueError("Invalid number of days")
                self.reply(json.dumps(retention_preview(int(value))).encode(), "application/json; charset=utf-8")
            except ValueError:
                self.send_error(400, "Invalid retention preview")
            except OSError:
                self.send_error(503, "Could not preview cleanup")
        elif path.startswith("/shorts/"):
            self.short_media(path)
        elif path.startswith("/sightings/thumb/") and path.endswith(".jpg"):
            self.sighting_image(path)
        elif path == "/highlights/":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            self.reply(highlights_page(day), "text/html; charset=utf-8")
        elif path.startswith("/highlights/"):
            self.reel(path)
        elif path.startswith("/clips/"):
            self.clip(path)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        actions = {"/api/sightings/saved": "save-sighting", "/api/shorts": "make-short",
                   "/api/retention": "set-retention", "/api/insights/cats": "set-cats", "/api/studio": "studio",
                   "/api/camera": "camera", "/api/insights/fix": "fix-cat"}
        if path not in actions:
            self.send_error(404)
            return
        # A cross-origin page cannot send this custom header without a preflight.
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if self.headers.get("X-Catcam-Action") != actions[path] or content_type != "application/json":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Invalid request size.")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("Invalid request.")
            if path == "/api/sightings/saved":
                result = save_sighting(request.get("name"), request.get("saved"))
            elif path == "/api/shorts":
                result = queue_short(request.get("name"))
            elif path == "/api/insights/cats":
                result = set_cats(request.get("cats"))
            elif path == "/api/studio":
                result = studio_request(request)
            elif path == "/api/camera":
                result = camera_request(request)
            elif path == "/api/insights/fix":
                result = fix_cat(request)
            else:
                if "days" not in request:
                    raise ValueError("Missing retention setting")
                result = set_retention(request["days"])
            self.reply(json.dumps(result).encode(), "application/json; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FavoritesUnavailable:
            self.send_error(503, "Favorites unavailable. Cleanup and favorite changes are paused.")
        except FileNotFoundError:
            self.send_error(404, "Recording unavailable")
        except (TypeError, ValueError):
            self.send_error(400, "Invalid request")
        except RuntimeError:
            self.send_error(503, "The clip queue is full")
        except OSError:
            self.send_error(500, "Could not save the sighting")

    def log_request(self, code="-", size="-"):
        if self.path != "/frame" or code != 200:
            super().log_request(code, size)


def check_detector():
    detector = CatDetector()
    frame = detector.numpy.full((detector.side, detector.side, 3), 114, dtype=detector.numpy.uint8)
    if not math.isfinite(detector.score(frame)):
        raise ValueError("The detector returned an invalid score")


def install_highlights():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("Run this on mina as faure, without sudo. It requests sudo for the service restart.")
    app = Path.home() / "catcam.py"
    service = "catcam-recorder.service"
    original = app.read_bytes()
    load_state = subprocess.check_output(
        ["systemctl", "show", "-p", "LoadState", "--value", service], text=True
    ).strip()
    if load_state != "loaded":
        raise SystemExit(f"The existing {service} is required on mina.")

    # Install Python dependencies into a Cat Cam venv, separate from system Python.
    import importlib.util
    packages = []
    if not all(shutil.which(program) for program in ("ffmpeg", "ffprobe")):
        packages.append("ffmpeg")
    if not WORKER_PYTHON.is_file() and importlib.util.find_spec("ensurepip") is None:
        packages.append("python3-venv")
    if packages:
        subprocess.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages], check=True)
    HIGHLIGHT_DATA.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    if not WORKER_PYTHON.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(WORKER_PYTHON.parent.parent)], check=True)
    subprocess.run([
        str(WORKER_PYTHON), "-m", "pip", "--isolated", "install", "--only-binary=:all:",
        "--disable-pip-version-check", "--index-url", "https://pypi.org/simple",
        "onnxruntime==1.23.2",
    ], check=True)
    if not MODEL.is_file():
        print("Downloading the YOLOX-Nano model from its official release.", flush=True)
        limit = 8 * 1024 * 1024
        with urlopen(MODEL_URL, timeout=25) as response:
            model_bytes = response.read(limit + 1)
        if not 2 * 1024 * 1024 <= len(model_bytes) <= limit:
            raise ValueError("The model download has an unexpected size")
        write_atomic(MODEL, model_bytes)
    subprocess.run([str(WORKER_PYTHON), str(script), "--check-detector"], check=True)
    if b"libx264" not in run_media("ffmpeg", ["-hide_banner", "-encoders"]):
        raise SystemExit("FFmpeg needs the libx264 encoder for phone-compatible reels.")
    print("Cat detector passed its startup check.", flush=True)

    # Keep a recoverable copy and switch only after the dependencies pass preflight.
    subprocess.run(["sudo", "-v"], check=True)
    with tempfile.NamedTemporaryFile(dir=HIGHLIGHT_DATA, prefix="catcam-before-", suffix=".py", delete=False) as backup:
        backup.write(original)
        backup_path = backup.name
    print(f"Previous Cat Cam script saved at {backup_path}", flush=True)
    write_atomic(app, script.read_bytes())
    restart = ["sudo", "systemctl", "restart", service]
    try:
        subprocess.run(restart, check=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/highlights/", timeout=2) as page:
                    if b"<title>Cat highlights</title>" in page.read():
                        print("Cat highlights is ready. Mina is scanning finished clips in the background.", flush=True)
                        return
            except (OSError, HTTPException):
                pass
            time.sleep(1)
        raise RuntimeError("The highlights page did not start")
    except (Exception, KeyboardInterrupt):
        print("Restoring the previous Cat Cam script.", flush=True)
        write_atomic(app, original)
        subprocess.run(restart, check=True)
        raise


def install_sightings():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("Run this on mina as faure, without sudo. It will request sudo for the restart.")
    app = Path.home() / "catcam.py"
    service = "catcam-recorder.service"
    if not app.is_file() or not MODEL.is_file() or not WORKER_PYTHON.is_file():
        raise SystemExit("The existing Cat Cam and its highlights detector are required.")
    load_state = subprocess.check_output(
        ["systemctl", "show", "-p", "LoadState", "--value", service], text=True
    ).strip()
    if load_state != "loaded":
        raise SystemExit(f"The existing {service} is required on mina.")
    script = Path(__file__).resolve()
    updated = script.read_bytes()
    compile(updated, str(script), "exec")
    original = app.read_bytes()
    HIGHLIGHT_DATA.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sudo", "-v"], check=True)
    with tempfile.NamedTemporaryFile(dir=HIGHLIGHT_DATA, prefix="catcam-before-sightings-",
                                     suffix=".py", delete=False) as backup:
        backup.write(original)
        backup_path = backup.name
    print(f"Previous Cat Cam script saved at {backup_path}", flush=True)
    write_atomic(app, updated)
    restart = ["sudo", "systemctl", "restart", service]
    try:
        subprocess.run(restart, check=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/api/sightings", timeout=2) as response:
                    data = json.load(response)
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/api/shorts", timeout=2) as response:
                    shorts = json.load(response)
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/sightings/", timeout=2) as response:
                    page = response.read()
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/api/day", timeout=4) as response:
                    journal = json.load(response)
                with urlopen(f"http://127.0.0.1:{VIEW_PORT}/", timeout=2) as response:
                    home = response.read()
                if (isinstance(data.get("events"), list) and "checked" in data and
                        isinstance(shorts.get("events"), list) and b'id="makeShort"' in page and
                        journal.get("version") == APP_VERSION and len(journal.get("hours", [])) == 24 and
                        b'id="journalTitle"' in home):
                    print("Cat Cam is ready with The Window dashboard, daily activity and short clips.", flush=True)
                    return
            except (OSError, HTTPException, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError("The updated dashboard did not pass its health checks.")
    except (Exception, KeyboardInterrupt):
        print("Restoring the previous Cat Cam script.", flush=True)
        write_atomic(app, original)
        subprocess.run(restart, check=True)
        raise


def main():
    if not shutil.which("motion"):
        raise SystemExit("On mina, install Motion with: sudo apt install motion")
    with urlopen(SOURCE + "/state", timeout=4) as response:
        source = json.load(response)["result"]["source"]
    print("AKASO at startup:", source, flush=True)
    if not CLIPS.exists() and not HIGHLIGHT_DATA.exists() and not RETENTION.exists():
        init_favorites()
    CLIPS.mkdir(parents=True, exist_ok=True)
    try:
        backup_favorites(read_saved_sightings())
    except FavoritesUnavailable:
        print("Favorites unavailable. Cleanup and changes to favorites are paused.", flush=True)
    config = f"""daemon off
netcam_url {SOURCE}/stream
width {recording_size()[0]}
height {recording_size()[1]}
framerate 15
threshold 1800
minimum_motion_frames 2
pre_capture 15
post_capture 45
event_gap 8
movie_max_time 120
movie_output on
movie_codec mp4
movie_filename %Y%m%d-%H%M%S-%v
target_dir {CLIPS}
picture_output off
stream_port 0
webcontrol_port 0
"""
    handler = partial(Viewer, directory=str(CLIPS))
    with ThreadingHTTPServer(("127.0.0.1", VIEW_PORT), handler) as server, tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "motion.conf"
        conf.write_text(config, encoding="utf-8")
        with subprocess.Popen(["motion", "-n", "-c", str(conf)], cwd=tmp) as recorder:
            print(f"Viewer on port {VIEW_PORT}. Recordings: {CLIPS}", flush=True)
            server.timeout = 1
            next_cleanup = 0
            worker = None
            next_scan = time.monotonic() + 5
            highlights_enabled = MODEL.is_file() and WORKER_PYTHON.is_file()
            try:
                while recorder.poll() is None:
                    server.handle_request()
                    if time.monotonic() >= next_cleanup:
                        next_cleanup = time.monotonic() + 3600
                        with SAVED_LOCK:
                            days = retention_days()
                            if days is not None:
                                expire_clips(CLIPS, days)
                    if highlights_enabled and time.monotonic() >= next_scan:
                        next_scan = time.monotonic() + SCAN_INTERVAL
                        if worker is None or worker.poll() is not None:
                            try:
                                worker = subprocess.Popen([
                                    str(WORKER_PYTHON), "-u", str(Path(__file__).resolve()),
                                    "--scan-highlights",
                                ])
                            except OSError as error:
                                print(f"Could not start the highlight scan: {error}", flush=True)
                raise SystemExit(f"Motion ended with status {recorder.returncode}. See its log above.")
            except KeyboardInterrupt:
                pass
            finally:
                if recorder.poll() is None:
                    recorder.terminate()
                if worker is not None and worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()
                if recorder.poll() is None:
                    recorder.wait()


def end_scan(_signal, _frame):
    raise SystemExit(0)


if __name__ == "__main__":
    os.umask(0o077)
    mode = sys.argv[1:]
    if mode == ["--install-highlights"]:
        install_highlights()
    elif mode == ["--install-sightings"]:
        install_sightings()
    elif mode in (["--init-empty-favorites"], ["--restore-favorites"]):
        init_favorites(restore=mode == ["--restore-favorites"])
    elif mode == ["--check-detector"]:
        check_detector()
    elif mode == ["--scan-highlights"]:
        signal.signal(signal.SIGTERM, end_scan)
        os.nice(10)
        scan_highlights(CatDetector())
    elif not mode:
        main()
    else:
        raise SystemExit("Usage: python3 catcam.py [--install-highlights | --install-sightings | --scan-highlights | --init-empty-favorites | --restore-favorites]")
