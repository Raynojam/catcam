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

test('zoom responds promptly, stays bounded, and full picture resets it', async () => {
  const page = openPage();
  assert.equal(page.element('zoomIn').disabled, true);
  await page.frame();
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('zoomOut').disabled, true);
  page.click('zoomIn');
  assert.equal(page.element('picture').style.transform, 'scale(1.25)');
  assert.equal(page.element('stage').dataset.motion, 'quick');
  for (let i = 0; i < 8; i++) page.click('zoomIn');
  assert.equal(page.element('picture').style.transform, 'scale(2)');
  assert.equal(page.element('zoomIn').disabled, true);
  page.click('resetView');
  assert.equal(page.element('picture').style.transform, 'scale(1)');
  assert.equal(page.element('zoomOut').disabled, true);
});

test('the tour is optional and manual input takes over immediately', async () => {
  const page = openPage();
  await page.frame();
  page.click('tourToggle');
  assert.equal(page.hasShotTimer(), true);
  page.advanceShot();
  assert.equal(page.element('stage').dataset.motion, 'tour');
  assert.equal(page.element('picture').style.transform, 'scale(1.45)');
  await page.frame();
  assert.equal(page.element('picture').style.transform, 'scale(1.45)');
  page.click('zoomIn');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('stage').dataset.motion, 'quick');
  assert.equal(page.element('tourToggle')['aria-pressed'], 'false');
});

test('tap focuses the chosen area and dragging cannot leave the shared picture', async () => {
  const page = openPage();
  await page.frame();
  function pointer(type, x, y) {
    page.element('stage').dispatchEvent(Object.assign(new Event(type), {
      pointerId: 1, button: 0, isPrimary: true, clientX: x, clientY: y,
    }));
  }
  pointer('pointerdown', 100, 100);
  pointer('pointerup', 100, 100);
  assert.equal(page.element('picture').style.transform, 'scale(1.75)');
  assert.equal(page.element('picture').style.transformOrigin, '0% 0%');
  pointer('pointerdown', 200, 200);
  pointer('pointermove', -1000, -1000);
  pointer('pointerup', -1000, -1000);
  assert.equal(page.element('picture').style.transformOrigin, '100% 100%');
  assert.equal(page.element('stage').dataset.dragging, 'false');
});

test('keyboard controls and stored view restore without starting a tour', async () => {
  const page = openPage({saved: {zoom: 1.5, x: .2, y: .3, tour: false}});
  await page.frame();
  assert.equal(page.element('picture').style.transformOrigin, '20% 30%');
  page.element('stage').dispatchEvent(Object.assign(new Event('keydown'), {key: '+'}));
  assert.equal(page.element('picture').style.transform, 'scale(1.75)');
  page.element('stage').dispatchEvent(Object.assign(new Event('keydown'), {key: '0'}));
  assert.equal(page.element('picture').style.transform, 'scale(1)');
  assert.equal(page.hasShotTimer(), false);
});

test('pause and disconnect disable controls and preserve the selected framing', async () => {
  const page = openPage();
  await page.frame();
  page.click('zoomIn');
  page.click('pause');
  assert.equal(page.element('picture').hidden, true);
  assert.equal(page.element('zoomIn').disabled, true);
  page.click('zoomIn');
  assert.equal(page.element('picture').style.transform, 'scale(1.25)');
  page.click('pause');
  await page.frame();
  assert.equal(page.element('picture').style.transform, 'scale(1.25)');
  page.click('tourToggle');
  page.run('offline()');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('viewMode').hidden, true);
  await page.frame();
  assert.equal(page.hasShotTimer(), true);
  page.document.hidden = true;
  page.document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(page.hasShotTimer(), false);
});

test('sign-out blocks late frames and pointer input', async () => {
  const page = openPage();
  await page.frame();
  page.click('tourToggle');
  page.click('signOut');
  await page.frame();
  page.click('zoomIn');
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('picture').hidden, true);
  assert.equal(page.element('tourToggle').disabled, true);
  assert.equal(page.window.localStorage.getItem('catLoungeSignOutPending'), '1');
});

test('reduced motion starts steady and stops an active tour', async () => {
  const reduced = openPage({reduced: true, saved: {zoom: 1.5, x: .2, y: .3, tour: true}});
  await reduced.frame();
  assert.equal(reduced.hasShotTimer(), false);
  assert.equal(reduced.element('picture').style.transform, 'scale(1)');
  const page = openPage();
  await page.frame();
  page.click('tourToggle');
  page.preference.matches = true;
  page.preference.dispatchEvent(Object.assign(new Event('change'), {matches: true}));
  assert.equal(page.hasShotTimer(), false);
  assert.equal(page.element('stage').dataset.motion, 'still');
});

function openPage({reduced = false, saved} = {}) {
  const elements = new Map(), timers = new Map();
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, Object.assign(new EventTarget(), {
      dataset: {}, style: {setProperty(key, value) { this[key] = value; }}, hidden: true, textContent: '',
      classList: {add() {}, remove() {}},
      setAttribute(key, value) { this[key] = value; },
      removeAttribute(key) { delete this[key]; },
      getBoundingClientRect() { return {left: 0, top: 0, width: 400, height: 400}; },
      setPointerCapture(id) { this.pointerId = id; },
      hasPointerCapture(id) { return this.pointerId === id; },
      releasePointerCapture() { delete this.pointerId; },
    }));
    return elements.get(id);
  }
  const preference = Object.assign(new EventTarget(), {matches: reduced});
  const document = Object.assign(new EventTarget(), {hidden: false, getElementById: element});
  const window = Object.assign(new EventTarget(), {
    localStorage: storage(saved ? {catLoungeFraming: JSON.stringify(saved)} : {}),
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
