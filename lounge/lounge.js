'use strict';
const byId = id => document.getElementById(id);
const picture = byId('picture');
const sessionEndKey = 'catLoungeSignOutPending';
const frameTime = new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit', second: '2-digit'});
let paused = false;
let closing = false;
let sendingLogout = false;
let revision = 0;
let pollTimer;
let requestInFlight;
let imageUrl;
let liveMode = typeof ReadableStream === 'function';
const stage = byId('stage');
const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
const tour = [
  {zoom: 1, x: .5, y: .5, seconds: 20},
  {zoom: 1.45, x: .12, y: .18, seconds: 24},
  {zoom: 1, x: .5, y: .5, seconds: 18},
  {zoom: 1.45, x: .48, y: 1, seconds: 24},
];
let framing = {zoom: 1, x: .5, y: .5};
let tourEnabled = false;
let tourTimer;
let tourStep = 0;
let directorLive = false;
let drag;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

function rememberFraming() {
  storeValue('localStorage', 'catLoungeFraming', JSON.stringify({...framing, tour: tourEnabled}));
}
function paintFraming(motion = 'quick') {
  stage.dataset.motion = motionPreference.matches ? 'still' : motion;
  stage.dataset.zoomed = String(framing.zoom > 1);
  picture.style.transform = `scale(${framing.zoom})`;
  picture.style.transformOrigin = `${framing.x * 100}% ${framing.y * 100}%`;
  byId('zoomLevel').textContent = `${Number(framing.zoom.toFixed(2))}\u00d7`;
  byId('zoomOut').disabled = !directorLive || closing || framing.zoom <= 1;
  byId('zoomIn').disabled = !directorLive || closing || framing.zoom >= 2;
  byId('resetView').disabled = !directorLive || closing || (!tourEnabled && framing.zoom === 1);
  byId('tourToggle').disabled = !directorLive || closing;
  byId('tourToggle').setAttribute('aria-pressed', String(tourEnabled));
  byId('tourToggle').textContent = tourEnabled ? 'Stop tour' : 'Slow tour';
  byId('viewHelp').textContent = tourEnabled
    ? 'A gentle tour. Touch the picture or use zoom to take over.'
    : framing.zoom > 1 ? 'Drag to look around. Full picture resets your view.'
      : 'Tap the picture to look closer, or use the zoom buttons.';
  byId('viewMode').textContent = tourEnabled ? 'Tour' : framing.zoom > 1 ? 'Close-up' : 'Full picture';
  byId('viewMode').hidden = !directorLive;
}
function setFraming(zoom, x = framing.x, y = framing.y, motion = 'quick') {
  framing = {zoom: clamp(zoom, 1, 2), x: clamp(x, 0, 1), y: clamp(y, 0, 1)};
  if (framing.zoom === 1) framing.x = framing.y = .5;
  paintFraming(motion);
}
function stopTour() {
  tourEnabled = false;
  clearTimeout(tourTimer);
}
function changeZoom(amount) {
  if (!directorLive || closing) return;
  stopTour();
  setFraming(Math.round((framing.zoom + amount) * 100) / 100);
  rememberFraming();
}
function resetFraming() {
  if (!directorLive || closing) return;
  stopTour();
  setFraming(1);
  rememberFraming();
}
function scheduleShot() {
  clearTimeout(tourTimer);
  if (!directorLive || !tourEnabled || document.hidden || paused || closing) return;
  tourTimer = setTimeout(() => {
    if (!directorLive || !tourEnabled || document.hidden || paused || closing) return;
    tourStep = (tourStep + 1) % tour.length;
    const shot = tour[tourStep];
    setFraming(shot.zoom, shot.x, shot.y, 'tour');
    scheduleShot();
  }, tour[tourStep].seconds * 1000);
}
function toggleTour() {
  if (!directorLive || closing) return;
  if (tourEnabled) stopTour();
  else { tourEnabled = true; tourStep = 0; setFraming(1); }
  paintFraming();
  rememberFraming();
  scheduleShot();
}
function cancelDrag() {
  if (drag && stage.hasPointerCapture?.(drag.id)) stage.releasePointerCapture(drag.id);
  drag = null;
  stage.dataset.dragging = 'false';
}
function directorState(live) {
  if (live === directorLive) return;
  directorLive = live;
  clearTimeout(tourTimer);
  cancelDrag();
  paintFraming('still');
  if (live) scheduleShot();
}
function focusPoint(clientX, clientY) {
  const rect = stage.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  // Convert the tapped point back into source coordinates before reframing.
  const sx = ((clientX - rect.left) / rect.width + (framing.zoom - 1) * framing.x) / framing.zoom;
  const sy = ((clientY - rect.top) / rect.height + (framing.zoom - 1) * framing.y) / framing.zoom;
  const zoom = Math.max(1.75, framing.zoom);
  stopTour();
  setFraming(zoom, (zoom * sx - .5) / (zoom - 1), (zoom * sy - .5) / (zoom - 1));
  rememberFraming();
}
stage.addEventListener('pointerdown', event => {
  if (!directorLive || closing || event.isPrimary === false || event.button !== 0) return;
  const rect = stage.getBoundingClientRect();
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY,
    originX: framing.x, originY: framing.y, width: rect.width, height: rect.height, moved: false};
  stage.setPointerCapture(event.pointerId);
});
stage.addEventListener('pointermove', event => {
  if (!drag || event.pointerId !== drag.id || !directorLive || closing) return;
  const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
  if (Math.hypot(dx, dy) > 6) drag.moved = true;
  if (!drag.moved || framing.zoom === 1 || !drag.width || !drag.height) return;
  stopTour();
  stage.dataset.dragging = 'true';
  setFraming(framing.zoom, drag.originX - dx / (drag.width * (framing.zoom - 1)),
    drag.originY - dy / (drag.height * (framing.zoom - 1)), 'still');
});
stage.addEventListener('pointerup', event => {
  if (!drag || event.pointerId !== drag.id) return;
  const tapped = !drag.moved;
  cancelDrag();
  if (!directorLive || closing) return;
  if (tapped) focusPoint(event.clientX, event.clientY);
  else rememberFraming();
});
stage.addEventListener('pointercancel', cancelDrag);
stage.addEventListener('lostpointercapture', cancelDrag);
stage.addEventListener('keydown', event => {
  if (!directorLive || closing || event.altKey || event.ctrlKey || event.metaKey) return;
  const moves = {ArrowLeft: [-.1, 0], ArrowRight: [.1, 0], ArrowUp: [0, -.1], ArrowDown: [0, .1]};
  if (event.key === '+' || event.key === '=') changeZoom(.25);
  else if (event.key === '-') changeZoom(-.25);
  else if (event.key === '0' || event.key === 'Escape') resetFraming();
  else if (moves[event.key] && framing.zoom > 1) {
    stopTour();
    setFraming(framing.zoom, framing.x + moves[event.key][0], framing.y + moves[event.key][1]);
    rememberFraming();
  } else return;
  event.preventDefault();
});
byId('zoomOut').addEventListener('click', () => changeZoom(-.25));
byId('zoomIn').addEventListener('click', () => changeZoom(.25));
byId('resetView').addEventListener('click', resetFraming);
byId('tourToggle').addEventListener('click', toggleTour);
try {
  const saved = JSON.parse(storageValue('localStorage', 'catLoungeFraming'));
  if (saved && ['zoom', 'x', 'y'].every(key => typeof saved[key] === 'number' && Number.isFinite(saved[key]))) {
    setFraming(saved.zoom, saved.x, saved.y, 'still');
    tourEnabled = saved.tour === true && !motionPreference.matches;
  }
} catch { /* Start with the full picture when no usable preference is saved. */ }
if (motionPreference.matches) { stopTour(); setFraming(1, .5, .5, 'still'); }
paintFraming('still');
motionPreference.addEventListener('change', event => {
  if (event.matches) { stopTour(); setFraming(1, .5, .5, 'still'); }
});

function storageValue(type, key) {
  try { return window[type].getItem(key); } catch { return null; }
}
function storeValue(type, key, value) {
  try {
    if (value === null) window[type].removeItem(key);
    else window[type].setItem(key, value);
  } catch { /* This visit still works without browser storage. */ }
}
function stripInvitation() {
  if (location.hash) history.replaceState(history.state, '', location.pathname + location.search);
}
stripInvitation();
window.addEventListener('hashchange', stripInvitation);

function clearImage() {
  directorState(false);
  picture.hidden = true;
  picture.removeAttribute('src');
  if (imageUrl) URL.revokeObjectURL(imageUrl);
  imageUrl = null;
}
function curtain(title, help, status) {
  clearImage();
  byId('curtain').hidden = false;
  byId('curtainTitle').textContent = title;
  byId('curtainHelp').textContent = help;
  if (byId('status').textContent !== status) byId('status').textContent = status;
  byId('badge').classList.remove('live');
}
function stopPolling() {
  revision++;
  clearTimeout(pollTimer);
  requestInFlight?.abort();
}
function mayShow(current) {
  return current === revision && !paused && !closing && !document.hidden;
}
function offline() {
  curtain('Waiting for Mina', 'The camera will reconnect automatically.', 'Reconnecting');
}

async function showFrame(blob, current) {
  const candidate = URL.createObjectURL(blob);
  try {
    const image = new Image();
    image.src = candidate;
    await image.decode();
    if (!mayShow(current)) return false;
    const prior = imageUrl;
    picture.src = candidate;
    imageUrl = candidate;
    const ratio = image.naturalWidth && `${image.naturalWidth} / ${image.naturalHeight}`;
    if (ratio && stage.style.aspectRatio !== ratio) {
      stage.style.aspectRatio = ratio;
      stage.style.setProperty('--frame-ratio', String(image.naturalWidth / image.naturalHeight));
    }
    if (prior) URL.revokeObjectURL(prior);
    picture.hidden = false;
    directorState(true);
    byId('curtain').hidden = true;
    if (byId('status').textContent !== 'Live') byId('status').textContent = 'Live';
    byId('badge').classList.add('live');
    byId('lastFrame').textContent = frameTime.format(new Date());
    return true;
  } finally {
    if (imageUrl !== candidate) URL.revokeObjectURL(candidate);
  }
}

// Returns a delay before trying again, or null when the page has moved on.
async function explainFailure(response, current) {
  if (response.status === 401) {
    stopPolling();
    curtain('Session ended', 'Opening the PIN screen.', 'Signed out');
    location.replace('/');
    return null;
  }
  const state = await response.json().catch(() => ({}));
  if (!mayShow(current)) return null;
  if (state.state === 'busy') { liveMode = false; return 100; }
  if (state.state === 'paused') curtain('The curtain is closed', 'Faure paused the camera. You can leave this page open.', 'Private time');
  else offline();
  return 2000;
}

const boundaryEnd = new Uint8Array([13, 10, 13, 10]);
function findHeaderEnd(buffer) {
  outer: for (let i = 0; i + 3 < buffer.length; i++) {
    for (let j = 0; j < 4; j++) if (buffer[i + j] !== boundaryEnd[j]) continue outer;
    return i;
  }
  return -1;
}
function join(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

// The live view: one long request, read here so a stalled stream is noticed and replaced.
async function stream() {
  clearTimeout(pollTimer);
  const current = revision;
  if (!mayShow(current)) return;
  const controller = new AbortController();
  requestInFlight = controller;
  let watchdog;
  const kick = () => { clearTimeout(watchdog); watchdog = setTimeout(() => controller.abort(), 6000); };
  kick();
  let nextDelay = 300;
  let frames = 0;
  try {
    const response = await fetch('/perch.mjpg', {cache: 'no-store', signal: controller.signal});
    if (!mayShow(current)) return;
    if (!response.ok) {
      nextDelay = await explainFailure(response, current);
      return;
    }
    if (!response.body || !(response.headers.get('Content-Type') || '').startsWith('multipart/x-mixed-replace')) {
      liveMode = false;  // this browser cannot read a stream; use quick snapshots instead
      return;
    }
    const reader = response.body.getReader();
    let buffer = new Uint8Array(0);
    for (;;) {
      const {value, done} = await reader.read();
      if (done || !mayShow(current)) break;
      kick();
      buffer = join(buffer, value);
      let newest = null;
      for (;;) {
        const headerEnd = findHeaderEnd(buffer);
        if (headerEnd < 0) break;
        const header = new TextDecoder().decode(buffer.subarray(0, headerEnd));
        const match = /content-length:\s*(\d+)/i.exec(header);
        if (!match || +match[1] > 4194304) throw Error('Unexpected stream data');
        const begin = headerEnd + 4;
        const finish = begin + Number(match[1]);
        if (buffer.length < finish + 2) break;
        newest = buffer.slice(begin, finish);
        buffer = buffer.slice(finish + 2);
      }
      if (buffer.length > 8388608) throw Error('Stream buffer overflow');
      // Show only the newest complete picture so a slow phone never falls behind.
      if (newest && await showFrame(new Blob([newest], {type: 'image/jpeg'}), current)) frames++;
    }
    if (mayShow(current) && frames === 0) { offline(); nextDelay = 2000; }
  } catch {
    if (mayShow(current)) {
      offline();
      nextDelay = frames === 0 ? 2000 : 500;
    }
  } finally {
    clearTimeout(watchdog);
    if (nextDelay !== null && mayShow(current)) pollTimer = setTimeout(liveMode ? stream : poll, nextDelay);
  }
}

// Fallback for browsers without readable streams: one picture per request.
async function poll() {
  clearTimeout(pollTimer);
  const current = revision;
  if (!mayShow(current)) return;
  const controller = new AbortController();
  requestInFlight = controller;
  const timeout = setTimeout(() => controller.abort(), 7000);
  let nextDelay = 100;
  try {
    const response = await fetch('/frame.jpg', {cache: 'no-store', signal: controller.signal});
    if (!mayShow(current)) return;
    if (!response.ok) {
      nextDelay = await explainFailure(response, current);
      return;
    }
    const blob = await response.blob();
    if (!mayShow(current)) return;
    await showFrame(blob, current);
  } catch {
    if (mayShow(current)) { offline(); nextDelay = 2000; }
  } finally {
    clearTimeout(timeout);
    if (nextDelay !== null && mayShow(current)) pollTimer = setTimeout(poll, nextDelay);
  }
}
function watch() {
  if (liveMode) stream();
  else poll();
}

function pendingLogout() {
  return storageValue('localStorage', sessionEndKey) === '1' || storageValue('sessionStorage', sessionEndKey) === '1';
}
function closeView() {
  closing = true;
  stopPolling();
  byId('pause').disabled = true;
  byId('focus').disabled = true;
  byId('lastFrame').textContent = 'View closed';
  curtain('Your view is closed', 'Finishing sign-out on this device.', 'Signing out');
}
async function signOut() {
  closeView();
  if (sendingLogout) return;
  sendingLogout = true;
  storeValue('localStorage', sessionEndKey, '1');
  storeValue('sessionStorage', sessionEndKey, '1');
  storeValue('localStorage', 'catLoungeInvitation', null);
  storeValue('sessionStorage', 'catLoungePendingInvitation', null);
  byId('signOut').disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch('/api/logout', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
      body: '{}', signal: controller.signal
    });
    if (!response.ok || (await response.json()).ok !== true) throw Error('Sign-out pending');
    storeValue('sessionStorage', 'catLoungePinRetry', null);
    // Keep the latch through redirect and old back/forward pages. A new PIN login clears it.
    location.replace('/');
  } catch {
    curtain('Your view is closed', 'Sign-out is waiting for a connection. Tap Retry sign-out when you are back online.', 'Sign-out pending');
    byId('signOut').textContent = 'Retry sign-out';
    byId('signOut').disabled = false;
  } finally {
    clearTimeout(timeout);
    sendingLogout = false;
  }
}

byId('signOut').addEventListener('click', signOut);
byId('pause').addEventListener('click', () => {
  if (closing || pendingLogout()) return;
  paused = !paused;
  stopPolling();
  byId('pause').textContent = paused ? 'Resume my view' : 'Pause my view';
  byId('pause').setAttribute('aria-pressed', String(paused));
  if (paused) curtain('View paused', 'Tap Resume my view to look again.', 'Paused');
  else watch();
});
byId('focus').addEventListener('click', async () => {
  if (closing) return;
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await byId('viewer').requestFullscreen();
  } catch { byId('focus').textContent = 'Full screen unavailable'; }
});
document.addEventListener('fullscreenchange', () => {
  byId('focus').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen';
});
function suspend() {
  stopPolling();
  if (!closing) curtain('View paused', 'A fresh picture will appear when you return.', 'Paused');
}
function resume() {
  if (document.hidden) return;
  if (closing || pendingLogout()) { signOut(); return; }
  if (!paused) { stopPolling(); watch(); }
}
window.addEventListener('storage', event => {
  if (event.key === sessionEndKey && event.newValue === '1') signOut();
});
window.addEventListener('online', () => { if (closing) signOut(); });
document.addEventListener('visibilitychange', () => document.hidden ? suspend() : resume());
window.addEventListener('pagehide', suspend);
window.addEventListener('pageshow', event => { if (event.persisted) resume(); });
resume();
