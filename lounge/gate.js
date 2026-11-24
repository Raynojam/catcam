'use strict';

const byId = id => document.getElementById(id);
const savedKey = 'catLoungeInvitation';
const pendingKey = 'catLoungePendingInvitation';
const lockKey = 'catLoungePinRetry';
const logoutKey = 'catLoungeSignOutPending';
const validInvitation = value => typeof value === 'string' && /^[A-Za-z0-9_-]{32}$/.test(value);
const form = byId('login');
const pinField = byId('pin');
const message = byId('message');

let invitation = null;
let submitting = false;
let lockedUntil = 0;
let unlockTimer;

// A link still works on its first visit when the browser blocks storage.
function readStored(type, key) {
  try { return window[type].getItem(key); } catch { return null; }
}
function writeStored(type, key, value) {
  try {
    if (value === null) window[type].removeItem(key);
    else window[type].setItem(key, value);
  } catch { /* Access may be disabled for this browser. */ }
}
function forgetCandidate(value) {
  if (readStored('sessionStorage', pendingKey) === value) writeStored('sessionStorage', pendingKey, null);
  if (readStored('localStorage', savedKey) === value) writeStored('localStorage', savedKey, null);
}

function resetPin() {
  pinField.value = '';
  pinField.type = 'password';
  pinField.removeAttribute('aria-invalid');
  byId('showPin').setAttribute('aria-label', 'Show PIN');
  byId('showPin').textContent = 'Show';
}

function showMissing(rejected, focusHeading = false) {
  invitation = null;
  clearTimeout(unlockTimer);
  form.hidden = true;
  document.querySelector('.remember').hidden = true;
  byId('missing').hidden = false;
  byId('useSaved').hidden = !rejected || !validInvitation(readStored('localStorage', savedKey));
  byId('gateTitle').textContent = rejected ? 'This invitation has expired' : 'Open your invitation';
  byId('intro').textContent = rejected
    ? 'Open the latest link Faure sent you, or ask for a fresh invitation.'
    : 'Open the private link Faure sent you to enter the lounge.';
  if (focusHeading) byId('gateTitle').focus();
}

function updateButton() {
  byId('enter').disabled = submitting || Date.now() < lockedUntil;
  pinField.readOnly = submitting;
}
function showLock(deadline) {
  lockedUntil = deadline;
  clearTimeout(unlockTimer);
  const time = new Date(deadline).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit', second: '2-digit'});
  message.textContent = 'Too many attempts. You can try again at ' + time + '.';
  updateButton();
  // Announce once. Announcing a second-by-second countdown would interrupt a screen reader.
  unlockTimer = setTimeout(() => {
    lockedUntil = 0;
    writeStored('sessionStorage', lockKey, null);
    message.textContent = 'You can try again now.';
    updateButton();
  }, Math.max(0, deadline - Date.now()) + 10);
}

function showForm(value, focusField = false) {
  invitation = value;
  lockedUntil = 0;
  clearTimeout(unlockTimer);
  resetPin();
  byId('gateTitle').textContent = 'Enter your PIN';
  byId('intro').textContent = 'Use the four digits Faure shared with you.';
  form.hidden = false;
  document.querySelector('.remember').hidden = false;
  byId('missing').hidden = true;
  message.textContent = '';
  updateButton();
  try {
    const lock = JSON.parse(readStored('sessionStorage', lockKey) || 'null');
    const remaining = lock && lock.until - Date.now();
    if (lock && lock.invite === value && remaining > 0 && remaining <= 86400000) showLock(lock.until);
  } catch { /* The server also enforces the lock. */ }
  if (focusField) pinField.focus();
}

function openInvitation() {
  // The lounge needs only the PIN now. Old invitation links still open this page; drop their code.
  if (location.hash) history.replaceState(history.state, '', location.pathname + location.search);
  writeStored('sessionStorage', pendingKey, null);
  writeStored('localStorage', savedKey, null);
  showForm('pin');
}

byId('useSaved').addEventListener('click', () => {
  const saved = readStored('localStorage', savedKey);
  showForm('pin', true);
});

byId('showPin').addEventListener('click', () => {
  const show = pinField.type === 'password';
  pinField.type = show ? 'text' : 'password';
  byId('showPin').textContent = show ? 'Hide' : 'Show';
  byId('showPin').setAttribute('aria-label', show ? 'Hide PIN' : 'Show PIN');
});
pinField.addEventListener('input', () => {
  pinField.value = pinField.value.replace(/[^0-9]/g, '').slice(0, 4);
  if (!submitting && Date.now() >= lockedUntil) {
    pinField.removeAttribute('aria-invalid');
    message.textContent = '';
  }
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!invitation || submitting || Date.now() < lockedUntil) return;
  if (!/^[0-9]{4}$/.test(pinField.value)) {
    pinField.setAttribute('aria-invalid', 'true');
    message.textContent = 'Enter all four digits.';
    pinField.focus();
    return;
  }

  const candidate = invitation;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  submitting = true;
  updateButton();
  message.textContent = 'Opening the lounge…';
  try {
    const response = await fetch('/api/login', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({invite: candidate, pin: pinField.value}), signal: controller.signal
    });
    const result = await response.json().catch(() => ({}));
    if (response.ok && result.ok === true) {
      writeStored('localStorage', savedKey, candidate);
      writeStored('sessionStorage', pendingKey, null);
      writeStored('sessionStorage', lockKey, null);
      writeStored('localStorage', logoutKey, null);
      writeStored('sessionStorage', logoutKey, null);
      resetPin();
      location.replace('/');
      return;
    }
    resetPin();
    if (response.status === 403 && result.code === 'invalid_invite') {
      forgetCandidate(candidate);
      showMissing(true, true);
    } else if (response.status === 429) {
      const wait = Number(result.retry_after || response.headers.get('Retry-After') || 900);
      const seconds = Number.isFinite(wait) ? Math.max(1, Math.min(86400, wait)) : 900;
      const until = Date.now() + seconds * 1000;
      writeStored('sessionStorage', lockKey, JSON.stringify({invite: candidate, until}));
      showLock(until);
    } else if (response.status === 401) {
      message.textContent = 'That PIN did not match. Try again.';
      pinField.setAttribute('aria-invalid', 'true');
      pinField.focus();
    } else if (response.status === 403) {
      message.textContent = 'Open your invitation directly in your browser, then try again.';
    } else {
      message.textContent = 'The lounge is unavailable. Please try again in a moment.';
    }
  } catch {
    message.textContent = 'Check your connection, then try again.';
  } finally {
    clearTimeout(timeout);
    submitting = false;
    updateButton();
  }
});

openInvitation();
window.addEventListener('hashchange', openInvitation);
