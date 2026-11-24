const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'lounge', 'lounge.js'), 'utf8');

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}

function openPage({reduced = false, saved} = {}) {
  const elements = new Map(), timers = new Map();
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, Object.assign(new EventTarget(), {
      dataset: {}, style: {}, hidden: true, textContent: '',
      classList: {add() {}, remove() {}},
      setAttribute(key, value) { this[key] = value; },
      removeAttribute(key) { delete this[key]; },
    }));
    return elements.get(id);
  }
  const preference = Object.assign(new EventTarget(), {matches: reduced});
  const document = Object.assign(new EventTarget(), {hidden: false, getElementById: element});
  const window = Object.assign(new EventTarget(), {
    localStorage: storage(saved ? {catLoungeView: saved} : {}),
    sessionStorage: storage(),
    matchMedia: () => preference,
  });
  const context = vm.createContext({
    document, window, Intl, URL, Blob, AbortController, Uint8Array, TextDecoder,
    location: {hash: '', pathname: '/', search: '', replace() {}}, history: {},
    setTimeout(fn, milliseconds) { timers.set(++timerId, {fn, milliseconds}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    fetch: () => new Promise(() => {}),
    Image: class {
      naturalWidth = 422;
      naturalHeight = 446;
      async decode() {}
    },
  });
  const run = code => vm.runInContext(code, context);
  run(source);
  return {
    element, run, timers, document, preference, window,
    async frame() { await run('showFrame(new Blob(["jpeg"]), revision)'); },
    click(id) { element(id).dispatchEvent(new Event('click')); },
    advanceShot() {
      const entry = [...timers.entries()].find(([, timer]) => timer.milliseconds >= 18000);
      assert.ok(entry, 'the tour schedules its next shot');
      timers.delete(entry[0]);
      entry[1].fn();
    },
    hasShotTimer() { return [...timers.values()].some(timer => timer.milliseconds >= 18000); },
  };
}

test('the tour starts with a fresh wide frame and returns wide between close-ups', async () => {
  const page = openPage();
  assert.equal(page.hasShotTimer(), false);
  await page.frame();
  assert.equal(page.element('stage').dataset.shot, 'wide');
  page.advanceShot();
  assert.equal(page.element('stage').dataset.shot, 'upper');
  await page.frame();
  assert.equal(page.element('stage').dataset.shot, 'upper', 'new JPEGs keep the shot in progress');
  page.advanceShot();
  assert.equal(page.element('stage').dataset.shot, 'wide');
  page.advanceShot();
  assert.equal(page.element('stage').dataset.shot, 'lower');
});

test('a manual shot stays selected until Auto is chosen again', async () => {
  const page = openPage();
  await page.frame();
  page.click('viewLower');
  assert.equal(page.element('stage').dataset.shot, 'lower');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.window.localStorage.getItem('catLoungeView'), 'lower');
  page.click('viewAuto');
  assert.equal(page.element('stage').dataset.shot, 'wide');
  assert.equal(page.hasShotTimer(), true);
});

test('pause, an offline feed, and hidden tabs stop the tour and hide the picture', async () => {
  const page = openPage();
  await page.frame();
  page.advanceShot();
  page.click('pause');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('picture').hidden, true);
  assert.equal(page.element('stage').dataset.motion, 'still');
  page.click('pause');
  await page.frame();
  page.run('offline()');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('shotLabel').hidden, true);
  await page.frame();
  page.document.hidden = true;
  page.document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('picture').hidden, true);
});

test('sign-out stops motion and a late decoded frame cannot reopen the view', async () => {
  const page = openPage();
  await page.frame();
  page.click('signOut');
  await page.frame();
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('picture').hidden, true);
  assert.equal(page.element('viewAuto').disabled, true);
  assert.equal(page.window.localStorage.getItem('catLoungeSignOutPending'), '1');
});

test('reduced motion starts steady and takes effect during a tour', async () => {
  const reduced = openPage({reduced: true, saved: 'auto'});
  await reduced.frame();
  assert.equal(reduced.hasShotTimer(), false);
  assert.equal(reduced.element('viewWide')['aria-pressed'], 'true');
  const page = openPage();
  await page.frame();
  page.preference.matches = true;
  page.preference.dispatchEvent(Object.assign(new Event('change'), {matches: true}));
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('stage').dataset.motion, 'still');
});
