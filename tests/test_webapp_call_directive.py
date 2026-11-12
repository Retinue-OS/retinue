#!/usr/bin/env python3
"""The call directive [[call: <number> | <name>]] (webapp/components/markdown.js), under Node.

Ara puts it at the end of a reply that places a call. In a bubble it renders
as a tap-to-dial link; in the phone shell's voice conversation the thread
finds it with callOf() and places the call (conversation.js, _contCall). This
pins the parts a wrong number would come from: how a written number becomes
a dialable one (separators dropped, the bracketed trunk zero after a country
code dropped, too short or too long refused), that the last directive in a
message is the one placed, that one quoted as code (a span, a ``` or ~~~
fence, an unclosed fence) stays code, that only what the bubble shows as a
call link is a call (not one split across lines, cut apart by a table, or
quoted by a code span whose backticks pair only across lines), and that the
label is the name — or the number — behind a glyph, with no words in any
language.

A second harness drives the thread itself (conversation.js), with a shell
that places calls, a scripted mic and detector, and time run ten times
faster: a call is placed even when the no-speech timeout (or the mic) gives
out before its 5 s are up; its row shows the number beside the name; a call
the shell refuses, or never answers, says in the composer that it was not
placed; a user who is writing gets no call, and a take that cancelled one
never sends what is in the box unseen; and only Ara's own answer places a
call, never a message an agent pushed into the thread nor her reply to a
chat arrival, in the same poll as the answer or while it is read.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")

HARNESS = r"""
import assert from 'node:assert/strict';

globalThis.HTMLElement = class {};
globalThis.document = { addEventListener: () => {}, getElementById: () => null };

const { renderInline, renderMarkdown, callOf, callNumber, calledBefore } = await import('./markdown.js');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };
const PHONE = '\u{1F4DE} ';

ok('a written number becomes a dialable one', () => {
  assert.equal(callNumber('+41 79 123 45 67'), '+41791234567');
  assert.equal(callNumber('044 / 123-45-67'), '0441234567');
  assert.equal(callNumber('(044) 123.45.67'), '0441234567');
  assert.equal(callNumber('+41 (0)79 123 45 67'), '+41791234567');
  assert.equal(callNumber('*100#'), '*100#');
  assert.equal(callNumber('112'), '112');
});

ok('anything else is not a number', () => {
  assert.equal(callNumber('12'), null);
  // A service code counts its digits too: the skill's example of one that
  // is no call (dashboard-composing).
  assert.equal(callNumber('*#06#'), null);
  assert.equal(callNumber('123456789012345678901'), null);
  assert.equal(callNumber('+41 79 abc'), null);
  assert.equal(callNumber('tel:+41791234567'), null);
  assert.equal(callNumber(''), null);
  assert.equal(callNumber(null), null);
});

ok('the directive renders as a tap-to-dial link labelled with the name', () => {
  assert.equal(renderInline('Calling Anna now. [[call: +41 79 123 45 67 | Anna]]'),
    `Calling Anna now. <a class="md-call" href="tel:+41791234567">${PHONE}Anna</a>`);
});

ok('without a name the number is the label, as written', () => {
  assert.equal(renderInline('[[call: 044 123 45 67]]'),
    `<a class="md-call" href="tel:0441234567">${PHONE}044 123 45 67</a>`);
});

ok('the name is escaped', () => {
  assert.equal(renderInline('[[call: 0441234567 | <b>A & B</b>]]'),
    `<a class="md-call" href="tel:0441234567">${PHONE}&lt;b&gt;A &amp; B&lt;/b&gt;</a>`);
});

ok('a directive with no dialable number stays text', () => {
  assert.equal(renderInline('[[call: 12 | Short]]'), '[[call: 12 | Short]]');
});

ok('quoted as code it stays code', () => {
  assert.equal(renderInline('`[[call: 112]]`'), '<code>[[call: 112]]</code>');
});

ok('callOf finds the last valid directive', () => {
  assert.deepEqual(callOf('[[call: 0441234567 | A]] then [[CALL: +41 79 123 45 67 | B ]]'),
    { number: '+41791234567', name: 'B', voiceCancel: true });
  assert.deepEqual(callOf('[[call: 0441234567]] [[call: 12 | bad]]'),
    { number: '0441234567', name: '', voiceCancel: true });
  assert.equal(callOf('Call Anna on +41 79 123 45 67.'), null);
  assert.equal(callOf(null), null);
});

ok('no-voice-cancel is read, and never shows', () => {
  assert.deepEqual(callOf('Calling again. [[call: 0441234567 | Anna | no-voice-cancel]]'),
    { number: '0441234567', name: 'Anna', voiceCancel: false });
  assert.deepEqual(callOf('[[call: 0441234567 | no-voice-cancel]]'),
    { number: '0441234567', name: 'no-voice-cancel', voiceCancel: true });
  assert.deepEqual(callOf('[[call: 0441234567 | | no-voice-cancel]]'),
    { number: '0441234567', name: '', voiceCancel: false });
  assert.equal(renderInline('[[call: 0441234567 | Anna | no-voice-cancel]]'),
    `<a class="md-call" href="tel:0441234567">${PHONE}Anna</a>`);
  // Any other third part is not a call directive.
  assert.equal(callOf('[[call: 0441234567 | Anna | now]]'), null);
});

ok('a directive quoted as code is an example, not a call', () => {
  assert.equal(callOf('Write it so: `[[call: +41 79 123 45 67 | Anna Muster]]`'), null);
  assert.equal(callOf('For example:\n```\n[[call: 0441234567 | Anna]]\n```\nThat is all.'), null);
  // A ~~~ fence, and a fence never closed, render as code all the same.
  assert.equal(callOf('For example:\n~~~\n[[call: 0441234567 | Anna]]\n~~~\nThat is all.'), null);
  assert.equal(callOf('For example:\n```text\n[[call: 0441234567 | Anna]]'), null);
  assert.equal(callOf('For example:\n~~~\n[[call: 0441234567 | Anna]]\nand so on'), null);
  // A fence closes on the next fence line of either kind, as in the bubble,
  // so what follows it is prose again.
  assert.deepEqual(callOf('Like so:\n~~~\n[[call: 112]]\n```\nCalling Anna. [[call: 0441234567 | Anna]]'),
    { number: '0441234567', name: 'Anna', voiceCancel: true });
  assert.deepEqual(callOf('Like so: ```[[call: 112]]``` Calling Anna. [[call: 0441234567 | Anna]]'),
    { number: '0441234567', name: 'Anna', voiceCancel: true });
  assert.deepEqual(callOf('Like `[[call: 0441234567 | Example]]`. Calling Anna. [[call: +41 79 123 45 67 | Anna]]'),
    { number: '+41791234567', name: 'Anna', voiceCancel: true });
  assert.deepEqual(callOf('Calling Anna. [[call: 0441234567 | Anna]] Written `[[call: 112]]`.'),
    { number: '0441234567', name: 'Anna', voiceCancel: true });
});

ok('only what the bubble shows as a call link is a call', () => {
  // A stray backtick on one line pairs with nothing: the bubble pairs
  // backticks within a line, so the directive on the next is still code.
  assert.equal(callOf('use `notes\nand then `[[call: +1 900 555 0100 | Bank]]`'), null);
  // A directive split across lines is no link on either.
  assert.equal(callOf('Calling the bank. [[call: +1 900 555 0100 |\nBank]]'), null);
  assert.equal(callOf('Calling the bank. [[call:\n+1 900 555 0100 | Bank]]'), null);
  // A table cuts a named directive apart at its pipe; one without a name,
  // or with the pipe escaped, is a link in its cell as anywhere else.
  assert.equal(callOf('| who | call |\n|---|---|\n| Bank | [[call: +1 900 555 0100 | Bank]] |'), null);
  assert.deepEqual(callOf('| who | call |\n|---|---|\n| Bank | [[call: +1 900 555 0100]] |'),
    { number: '+19005550100', name: '', voiceCancel: true });
  assert.deepEqual(callOf('| who | call |\n|---|---|\n| Bank | [[call: +1 900 555 0100 \\| Bank]] |'),
    { number: '+19005550100', name: 'Bank', voiceCancel: true });
  // A chip's label may hold a backtick, which then opens no code span: the
  // directive after it is a link, and so a call.
  assert.deepEqual(callOf('[[chip: a`b]] Calling the bank. [[call: +1 900 555 0100 | Bank]]` end'),
    { number: '+19005550100', name: 'Bank', voiceCancel: true });
  assert.equal(callOf('[[chip: a`b]] `[[call: +1 900 555 0100 | Bank]]` end'), null);
  // The name is the one written, not the escaped label.
  assert.deepEqual(callOf('[[call: 0441234567 | Smith & "Sons" <AG>]]'),
    { number: '0441234567', name: 'Smith & "Sons" <AG>', voiceCancel: true });
  // And so is every call link the bubble shows: the last of them is placed.
  const text = 'Here:\n> [[call: 0441234567 | Anna]]\n- [[call: +1 900 555 0100 | Bank]]';
  assert.match(renderMarkdown(text), /tel:\+19005550100/);
  assert.deepEqual(callOf(text), { number: '+19005550100', name: 'Bank', voiceCancel: true });
});

ok('calledBefore: only an earlier reply to the same number counts', () => {
  const msgs = [
    { role: 'user', text: 'Call Anna' },
    { role: 'assistant', text: 'Calling Anna. [[call: +41 79 123 45 67 | Anna]]' },
    { role: 'user', text: 'Call Anna, it is loud here' },
    { role: 'assistant', text: 'Again. [[call: +41791234567 | Anna | no-voice-cancel]]' },
  ];
  assert.equal(calledBefore(msgs, 3, '+41791234567'), true);
  assert.equal(calledBefore(msgs, 1, '+41791234567'), false);
  assert.equal(calledBefore(msgs, 3, '0441234567'), false);
  // The user saying a directive is not an attempt.
  assert.equal(calledBefore([{ role: 'user', text: '[[call: 0441234567]]' }, {}], 1, '0441234567'), false);
  assert.equal(calledBefore(null, 3, '0441234567'), false);
  // An agent's message pushed into the thread is no attempt of Ara's, nor is
  // a directive of hers quoted as code.
  assert.equal(calledBefore([
    { role: 'user', text: 'Call Anna' },
    { role: 'agent', text: 'Relayed: [[call: 0441234567 | Anna]]' },
    { role: 'assistant', text: 'Write `[[call: 0441234567]]` to call.' },
    { role: 'assistant', text: '[[call: 0441234567 | Anna | no-voice-cancel]]' },
  ], 3, '0441234567'), false);
  // Nor is her reply to a chat arrival, which the loop never places.
  assert.equal(calledBefore([
    { role: 'user', text: 'Call Anna' },
    { role: 'assistant', arrival: true, text: 'Noted. [[call: 0441234567 | Anna]]' },
    { role: 'assistant', text: '[[call: 0441234567 | Anna | no-voice-cancel]]' },
  ], 2, '0441234567'), false);
});

console.log(`${passed} checks passed`);
"""

LOOP_HARNESS = r"""
import assert from 'node:assert/strict';

// ── Time, ten times faster: the call waits 5 s, the mic 2 s at the least ────
// Timers, intervals and Date.now() are scaled alike, so the detector, the
// call's wait and the loop's timeouts keep their order.
const SPEED = 10;
const realSetTimeout = globalThis.setTimeout;
const realSetInterval = globalThis.setInterval;
const realNow = Date.now.bind(Date);
const t0 = realNow();
Date.now = () => t0 + (realNow() - t0) * SPEED;
globalThis.setTimeout = (fn, ms = 0, ...rest) => realSetTimeout(fn, Math.max(0, Number(ms) || 0) / SPEED, ...rest);
globalThis.setInterval = (fn, ms = 0, ...rest) => realSetInterval(fn, Math.max(1, (Number(ms) || 0) / SPEED), ...rest);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(cond, what, ms = 15000) {
  const end = Date.now() + ms;
  while (!cond() && Date.now() < end) await wait(20);
  assert.ok(cond(), what);
}

// ── A scripted DOM ───────────────────────────────────────────────────────────
// Enough for the element to render into a shadow root and read its own
// markup back; nothing in it is ever found by a selector, so the wiring is
// skipped and the tests drive the element's own methods.
const registry = new Map();
class FakeShadow {
  constructor() { this.innerHTML = ''; this.activeElement = null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this._attrs = new Map(); this._l = new Map(); this.shadowRoot = null; this.isConnected = true; this.style = {}; }
  attachShadow() { this.shadowRoot = new FakeShadow(); return this.shadowRoot; }
  addEventListener(t, f) { if (!this._l.has(t)) this._l.set(t, []); this._l.get(t).push(f); }
  removeEventListener(t, f) { this._l.set(t, (this._l.get(t) || []).filter((g) => g !== f)); }
  dispatchEvent(ev) { for (const f of this._l.get(ev.type) || []) f(ev); return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) {
    const before = this.getAttribute(n);
    this._attrs.set(n, String(v));
    if (this.attributeChangedCallback) this.attributeChangedCallback(n, before, String(v));
  }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  toggleAttribute(n, on) {
    const want = on === undefined ? !this.hasAttribute(n) : !!on;
    if (want) this._attrs.set(n, ''); else this._attrs.delete(n);
    return want;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} },
};
globalThis.window = globalThis;
const winListeners = new Map();
globalThis.addEventListener = (t, f) => { if (!winListeners.has(t)) winListeners.set(t, []); winListeners.get(t).push(f); };
globalThis.removeEventListener = (t, f) => { winListeners.set(t, (winListeners.get(t) || []).filter((g) => g !== f)); };
globalThis.dispatchEvent = (ev) => { for (const f of [...(winListeners.get(ev.type) || [])]) f(ev); return true; };
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/', search: '', href: 'http://dash/' };
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
const store = new Map();
globalThis.localStorage = {
  get length() { return store.size; },
  key: (i) => [...store.keys()][i] ?? null,
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };

// ── The phone: a shell that places calls, a mic, a speech engine ────────────
const placed = [];
// What the shell answers a placeCall with: the event's state and reason, or
// null for a shell that never answers.
let shellAnswer = { state: 'placed' };
globalThis.RetinueShell = {
  platform: 'android', version: 6,
  setVoiceActive() {}, openSettings() {},
  placeCall(number) {
    placed.push(number);
    const a = shellAnswer;
    if (a) setTimeout(() => dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'call', number, ...a } })), 50);
  },
};
let micFails = false;
let micAsked = 0;
const stream = { getTracks: () => [{ stop() {} }] };
Object.defineProperty(globalThis, 'navigator', { value: {
  userAgent: 'node RetinueShell/6 (Android)',
  mediaDevices: { getUserMedia: async () => { micAsked += 1; if (micFails) throw Object.assign(new Error('busy'), { name: 'NotReadableError' }); return stream; } },
}, configurable: true });
globalThis.MediaRecorder = class {
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm'; this._l = {}; }
  addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); }
  start() { this.state = 'recording'; }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    setTimeout(() => {
      for (const f of this._l.dataavailable || []) f({ data: new Blob(['audio'], { type: 'audio/webm' }) });
      for (const f of this._l.stop || []) f();
    }, 0);
  }
};
// The room as the detector hears it (an RMS level), or no Web Audio at all.
let level = 0;
let webAudio = true;
class FakeAudioContext {
  constructor() { this.state = 'running'; this.currentTime = 0; this.destination = {}; }
  resume() { return Promise.resolve(); }
  close() {}
  createOscillator() { return { type: '', frequency: { value: 0 }, connect: (g) => g, start() {}, stop() {} }; }
  createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect: (d) => d }; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createAnalyser() {
    return { fftSize: 1024, frequencyBinCount: 64, smoothingTimeConstant: 0,
      getFloatTimeDomainData: (buf) => buf.fill(level), getByteFrequencyData() {} };
  }
}
Object.defineProperty(globalThis, 'AudioContext', { get: () => (webAudio ? FakeAudioContext : undefined), configurable: true });
// Speech: every utterance is read in `speakMs`, and what was read is kept.
let speakMs = 30;
const said = [];
globalThis.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
globalThis.speechSynthesis = {
  speaking: false, pending: false, paused: false,
  getVoices: () => [], cancel() {}, resume() {},
  speak(u) {
    said.push(u.text);
    setTimeout(() => { if (u.onstart) u.onstart(); if (u.onend) u.onend(); }, speakMs);
  },
};

// ── The gateway ──────────────────────────────────────────────────────────────
const threads = {};
const posts = [];
let transcript = '';
let clock = Date.parse('2026-11-12T10:00:00Z');
const ts = () => new Date(clock += 60000).toISOString();
const answer = (body) => Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) });
globalThis.fetch = (url, opts = {}) => {
  const method = opts.method || 'GET';
  const read = /^\/conversations\/([0-9a-f]{32})$/.exec(url);
  if (read && method === 'GET') return answer(threads[read[1]]);
  const reply = /^\/conversations\/([0-9a-f]{32})\/messages$/.exec(url);
  if (reply && method === 'POST') { posts.push(JSON.parse(opts.body)); return answer(threads[reply[1]]); }
  if (url.startsWith('/conversations/transcribe') && method === 'POST') return answer({ text: transcript });
  return answer({});
};

const { setContinuous, NO_SPEECH_KEY } = await import('./continuous.js');
await import('./conversation.js');
const Conv = registry.get('retinue-conversation');
assert.ok(Conv, 'the element registered itself');

let n = 0;
const ANNA = 'Calling Anna now. [[call: +41 79 123 45 67 | Anna Muster]]';
// A thread in continuous mode whose loop is armed by the user's spoken turn;
// `answerMsg` is what arrives for it (a list: several messages in one poll).
// `before` runs once the thread is open.
async function spoken(answerMsg, before) {
  const id = String(++n).padStart(32, '0');
  threads[id] = { id, title: 'Anna', pending: false, model: '', messages: [
    { role: 'user', text: 'Earlier question', ts: ts() },
    { role: 'assistant', text: 'Earlier answer', ts: ts() },
  ] };
  setContinuous(id, true);
  const el = new Conv();
  el._attrs.set('conversation-id', id);
  el._attrs.set('no-autofocus', '');
  el.connectedCallback();
  await until(() => el.thread, 'the thread is read');
  if (before) before(el);
  el._contEngage();
  threads[id].messages.push({ role: 'user', text: 'Call Anna', ts: ts() },
    ...[].concat(answerMsg).map((a) => ({ ...a, ts: ts() })));
  await el._load();
  return el;
}
const callRow = (el) => { el.render(); return el.shadowRoot.innerHTML.includes('class="call-row"'); };
const NOT_PLACED = 'The call to +41791234567 could not be placed.';
const notPlaced = (el) => { el.render(); return el.shadowRoot.innerHTML.includes(NOT_PLACED); };

let passed = 0;
async function ok(name, fn) {
  placed.length = 0; posts.length = 0; micFails = false; webAudio = true; level = 0; transcript = '';
  shellAnswer = { state: 'placed' }; speakMs = 30; said.length = 0;
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

// The no-speech timeout at its least, well inside the call's 5 s.
localStorage.setItem(NO_SPEECH_KEY, '2000');

await ok('the call row shows the name and the number to be dialled', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling, 'the call waits');
  el.render();
  assert.ok(el.shadowRoot.innerHTML.includes('Anna Muster · +41791234567'), el.shadowRoot.innerHTML);
  await until(() => placed.length === 1, 'and is placed');
  await until(() => !el._placingCall, 'the shell answered');
  assert.ok(!notPlaced(el), 'a placed call needs no note');
  el.disconnectedCallback();
});

await ok('a call the shell refuses says so, with the number', async () => {
  shellAnswer = { state: 'refused', reason: 'no-telephony' };
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => placed.length === 1, 'the call is handed to the shell');
  await until(() => !el._placingCall, 'the shell answered');
  assert.ok(notPlaced(el), el.shadowRoot.innerHTML);
  assert.ok(!el._contArmed, 'the loop stopped');
  el.disconnectedCallback();
});

await ok('so does a call the shell never answers', async () => {
  shellAnswer = null;
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => placed.length === 1, 'the call is handed to the shell');
  assert.ok(!notPlaced(el), 'no note while the answer may still come');
  await until(() => !el._placingCall, 'the wait for an answer ran out');
  assert.ok(notPlaced(el), el.shadowRoot.innerHTML);
  el.disconnectedCallback();
});

await ok('the dialer opened with the number needs no note', async () => {
  shellAnswer = { state: 'dialer', reason: 'disabled' };
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => placed.length === 1, 'the call is handed to the shell');
  await until(() => !el._placingCall, 'the shell answered');
  assert.ok(!notPlaced(el), el.shadowRoot.innerHTML);
  el.disconnectedCallback();
});

// Ara's reply to a chat message that arrived in a companion thread meanwhile
// answers the correspondent, whose text may have steered it.
const ARRIVAL = { role: 'assistant', arrival: true, text: 'Noted for Ben. [[call: +1 900 555 0100 | Bank]]' };
const NOTED = 'Noted, I will remind you tonight.';

await ok('a reply to a chat arrival in the same poll as the answer places no call', async () => {
  const el = await spoken([{ role: 'assistant', text: NOTED }, ARRIVAL]);
  await until(() => said.some((s) => s.includes('remind you tonight')), 'the answer to the user is the one read');
  await wait(7000);
  assert.ok(!el._calling && !callRow(el), 'no call row');
  assert.equal(placed.length, 0, 'no call');
  assert.ok(!said.some((s) => s.includes('Noted for Ben')), 'nor is the arrival reply read over it');
  el.disconnectedCallback();
});

await ok('nor does one that lands while the answer is read', async () => {
  speakMs = 3000;
  const el = await spoken({ role: 'assistant', text: NOTED });
  await until(() => said.some((s) => s.includes('remind you tonight')), 'the answer is being read');
  threads[el.thread.id].messages.push({ ...ARRIVAL, ts: ts() });
  await el._load();
  speakMs = 30;
  await wait(7000);
  assert.ok(!el._calling && !callRow(el), 'no call row');
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok("a reply to a chat arrival does not take the place of the answer's own call", async () => {
  speakMs = 3000;
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => said.length, 'the answer is being read');
  threads[el.thread.id].messages.push({ ...ARRIVAL, ts: ts() });
  await el._load();
  speakMs = 30;
  await until(() => placed.length === 1, 'the answer places its call');
  assert.deepEqual(placed, ['+41791234567']);
  el.disconnectedCallback();
});

await ok("nobody speaking, the detector's no-speech timeout does not drop the call", async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  await wait(3000);
  assert.ok(el._calling, 'past the no-speech timeout the call still waits');
  await until(() => placed.length === 1, 'and is placed');
  assert.deepEqual(placed, ['+41791234567']);
  await until(() => !el._placingCall, 'the shell answered');
  assert.equal(el._recState, 'idle', 'the mic is closed');
  assert.equal(posts.length, 0, 'nothing was sent');
  el.disconnectedCallback();
});

await ok('nor does the plain timer where the page cannot hear the stream', async () => {
  webAudio = false;
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  await wait(3000);
  assert.ok(el._calling, 'past the no-speech timeout the call still waits');
  await until(() => placed.length === 1, 'and is placed');
  assert.equal(posts.length, 0, 'nothing was sent');
  el.disconnectedCallback();
});

await ok('a mic that fails to open does not drop the call', async () => {
  micFails = true;
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => micAsked && el._attachError, 'the mic failed');
  assert.ok(el._calling, 'the call still waits');
  assert.ok(callRow(el), 'with its Cancel button');
  await until(() => placed.length === 1, 'and is placed');
  el.disconnectedCallback();
});

await ok('a user who is writing gets no call, and no take that could send their words', async () => {
  const asked = micAsked;
  const el = await spoken({ role: 'assistant', text: ANNA }, (e) => e.fill('A note for later'));
  await wait(1000);
  assert.ok(!el._calling && !callRow(el), 'no call row');
  assert.equal(micAsked, asked, 'no mic');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  assert.equal(posts.length, 0, 'nothing sent');
  assert.ok(!el._contArmed, 'the loop stopped, as it does for a user writing');
  el.disconnectedCallback();
});

await ok('a take that cancelled the call goes to review when the box holds something', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  await wait(500);
  el.fill('A note for later');   // a chip pressed meanwhile
  transcript = 'wait';
  level = 0.3;
  await until(() => !el._calling, 'speech cancelled the call');
  await wait(800);
  level = 0;
  await until(() => el._recState === 'idle' && !el.shadowRoot.innerHTML.includes('Transcribing'), 'the take ended');
  await wait(1500);
  assert.equal(posts.length, 0, 'nothing went out with the unseen note');
  assert.equal(placed.length, 0, 'the call stays cancelled');
  el.render();
  const box = el.shadowRoot.innerHTML.match(/<textarea[^>]*>([\s\S]*?)<\/textarea>/);
  assert.ok(box && box[1].includes('A note for later') && box[1].includes('wait'), 'both are in the box for review');
  el.disconnectedCallback();
});

await ok("a directive in an agent's message places no call", async () => {
  const el = await spoken({ role: 'agent', text: 'Relayed: [[call: +41 79 123 45 67 | Anna Muster]]' });
  await wait(1000);
  assert.ok(!el._calling && !callRow(el), 'no call row');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

console.log(`${passed} loop checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the call directive test needs it")
        return 0
    # The markdown checks need only what markdown.js imports; the loop's
    # need every component, which import each other by relative path.
    runs = (
        (HARNESS, ("base.js", "device-store.js", "markdown.js")),
        (LOOP_HARNESS, tuple(n for n in os.listdir(COMPONENTS) if n.endswith(".js"))),
    )
    for harness, names in runs:
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
            with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
                fh.write('{"type":"module"}')
            with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
                fh.write(harness)
            proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                                  text=True, timeout=120)
            sys.stdout.write(proc.stdout)
            if proc.returncode != 0:
                sys.stderr.write(proc.stderr)
                print("FAIL: call directive test")
                return 1
    print("PASS: call directive test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
