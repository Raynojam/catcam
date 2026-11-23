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
    if (prior) URL.revokeObjectURL(prior);
    picture.hidden = false;
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
      if (frames === 0) { offline(); nextDelay = 2000; }
      else nextDelay = 500;
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
  if (paused) curtain('View paused', 'Tap Resume my view to return to the perch.', 'Paused');
  else watch();
});
byId('focus').addEventListener('click', async () => {
  if (closing) return;
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await byId('stage').requestFullscreen();
  } catch { byId('focus').textContent = 'Full screen unavailable'; }
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
