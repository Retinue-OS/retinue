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
quoted by a code span whose backticks pair only across lines, with
JavaScript's whitespace, which the gateway's lint guard copies), that a
service code's # is escaped in the link but not in the call, and that the
label is the name — or the number — behind a glyph, with no words in any
language.

A second harness drives the thread itself (conversation.js), with a shell
that places calls, a scripted mic and detector, and time run ten times
faster: a call is placed even when the no-speech timeout (or the mic) gives
out before its 5 s are up; its row shows the number beside the name; a call
the shell refuses, or never answers, says in the composer that it was not
placed; a user who is writing gets no call, and a take that cancelled one
never sends what is in the box unseen; a take sent by touch, or one the
page cannot hear, cancels the call and its transcript decides; a call waiting
with the mic shut, or one resumed after an empty transcript while the page
was hidden, is dropped; no-voice-cancel in the name's place is the flag;
a take left by an earlier page carries its call while it is fresh, counted
from its transcript; a cancelling take that meets another send on the wire
leaves no call for a later turn; and only Ara's own answer places a
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
  // The flag in the name's place, with no name, is the flag: it is neither
  // ignored nor shown as a name, whatever its case.
  assert.deepEqual(callOf('[[call: 0441234567 | no-voice-cancel]]'),
    { number: '0441234567', name: '', voiceCancel: false });
  assert.deepEqual(callOf('[[call: 112 | No-Voice-Cancel ]]'),
    { number: '112', name: '', voiceCancel: false });
  assert.equal(renderInline('[[call: 112 | no-voice-cancel]]'),
    `<a class="md-call" href="tel:112">${PHONE}112</a>`);
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

ok('a service code dials whole: its # is escaped in the link, not in the call', () => {
  // A tap on tel:*100# would dial *100 — the # starts the URI's fragment.
  assert.equal(renderInline('[[call: *100# | Balance]]'),
    `<a class="md-call" href="tel:*100%23">${PHONE}Balance</a>`);
  assert.deepEqual(callOf('[[call: *100# | Balance]]'),
    { number: '*100#', name: 'Balance', voiceCancel: true });
});

ok("the bubble's whitespace is JavaScript's", () => {
  // The gateway's lint guard copies these readings
  // (test_web_gateway_claude_spawn.py): a no-break space or byte-order mark
  // ends a fence line, and \x1c or \x85 after a separator row makes it none.
  const bank = { number: '+19005550100', name: 'Bank', voiceCancel: true };
  const call = '[[call: +1 900 555 0100 | Bank]]';
  assert.deepEqual(callOf(`Says:\n\`\`\`\n\`\`\`\u00a0\n${call}\n\`\`\`\nShall I?`), bank);
  assert.deepEqual(callOf(`Says:\n\`\`\`\n\`\`\`\ufeff\n${call}\n\`\`\`\nShall I?`), bank);
  assert.deepEqual(callOf(`Says:\n${call}\n|---|---|\x1c\nShall I?`), bank);
  assert.deepEqual(callOf(`Says:\n${call}\n|---|---|\x85\nShall I?`), bank);
  assert.equal(callOf(`| a | ${call} |\n|---|---|\u00a0`), null);
  assert.equal(callOf(`\`\`\`\u00a0\n${call}\n\`\`\``), null);
  assert.deepEqual(callOf('| a | b |\x1c\n|---|---|\n| x | y | [[call: 0441234567]] |'),
    { number: '0441234567', name: '', voiceCancel: true });
  // No chip swallows the directive: "chıp" is no chip, nor does a prefill
  // run across U+2028.
  assert.deepEqual(callOf(`[[ch\u0131p: Yes | ${call}`), bank);
  assert.deepEqual(callOf(`[[chip: Yes | a\u2028${call}`), bank);
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
// A service worker (sw.js, "Sent dictations"), switched on by a test: it
// takes each dictation, keeps what the page posted, and answers with the
// transcript and, for a sent one, `swOutcome` — as if the page had been
// frozen while the worker sent it ({sent: true}), or the send had failed.
// `swLeft` holds the jobs an earlier page left behind, handed to whichever
// page asks for their place (dictation-adopt) — as done ones, their
// transcripts in. While `swHold` is set (a promise), a new job's transcript
// waits for it.
let swOn = false;
let swOutcome = { sent: true };
const swJobs = [];
const swLeft = [];
let swHold = null;
const swListeners = [];
const SW = {
  addEventListener(t, f) { if (t === 'message') swListeners.push(f); },
  controller: {
    postMessage(msg, ports) {
      if (msg.type === 'dictation-adopt') {
        // The left jobs under these places, and this page's own still running.
        const left = swLeft.filter((j) => msg.keys.includes(j.key));
        for (const j of left) swLeft.splice(swLeft.indexOf(j), 1);
        const running = swJobs.filter((j) => !j.done && msg.keys.includes(j.key))
          .map((j) => ({ id: j.id, key: j.key, state: 'running', send: !!j.send, at: Date.now() }));
        setTimeout(() => ports[0].postMessage({ jobs: [...left, ...running] }), 0);
        return;
      }
      if (msg.type !== 'dictation') return;
      swJobs.push(msg);
      const tell = (data, ms) => setTimeout(() => swListeners.forEach((f) => f({ data })), ms);
      tell({ type: 'dictation-started', id: msg.id }, 0);
      const out = msg.send ? swOutcome : {};
      const text = transcript;
      Promise.resolve(swHold).then(() => {
        msg.done = true;
        tell({ type: 'dictation-done', id: msg.id, key: msg.key, text, error: '', send: !!msg.send,
               sent: !!out.sent, unsent: out.unsent || '', at: Date.now(), call: msg.call || null }, 20);
      });
    },
  },
};
// A sent take an earlier page left under thread `id` with its transcript in
// but not yet sent, recorded `age` ms ago, the call it cancelled with it;
// its transcript came `textAge` ms ago (none: the worker never said when).
const leftTake = (id, text, age, call, textAge) => swLeft.push({ id: `left-${id}-${swLeft.length}`, key: `conv:${id}`,
  state: 'done', text, error: '', send: true, sent: false, unsent: '', at: Date.now() - age,
  textAt: textAge === undefined ? 0 : Date.now() - textAge, call: call || null });
Object.defineProperty(globalThis, 'navigator', { value: {
  get serviceWorker() { return swOn ? SW : undefined; },
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
// While set, a transcription waits for it (a promise) before it answers.
let transcribing = null;
// While set, a message POST waits for it (a promise) before it answers.
let postGate = null;
let clock = Date.parse('2026-11-12T10:00:00Z');
const ts = () => new Date(clock += 60000).toISOString();
const answer = (body) => Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) });
globalThis.fetch = (url, opts = {}) => {
  const method = opts.method || 'GET';
  const read = /^\/conversations\/([0-9a-f]{32})$/.exec(url);
  if (read && method === 'GET') return answer(threads[read[1]]);
  const reply = /^\/conversations\/([0-9a-f]{32})\/messages$/.exec(url);
  if (reply && method === 'POST') {
    posts.push(JSON.parse(opts.body));
    return Promise.resolve(postGate).then(() => answer(threads[reply[1]]));
  }
  if (url.startsWith('/conversations/transcribe') && method === 'POST') {
    return (transcribing || Promise.resolve()).then(() => answer({ text: transcript }));
  }
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
// `history` is what the thread held before, after its earlier exchange.
async function spoken(answerMsg, before, history = []) {
  const id = String(++n).padStart(32, '0');
  threads[id] = { id, title: 'Anna', pending: false, model: '', messages: [
    { role: 'user', text: 'Earlier question', ts: ts() },
    { role: 'assistant', text: 'Earlier answer', ts: ts() },
    ...history.map((m) => ({ ...m, ts: ts() })),
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
  shellAnswer = { state: 'placed' }; speakMs = 30; said.length = 0; transcribing = null; postGate = null;
  swOn = false; swOutcome = { sent: true }; swJobs.length = 0; swLeft.length = 0; swHold = null;
  try { await fn(); } finally { swOn = false; }
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

await ok('a call waiting with the mic shut is dropped when the user leaves the page', async () => {
  // A second attempt at the number, so no-voice-cancel holds: no take
  // listens, and only Cancel on the screen stops the call. With the page
  // gone, nobody sees it, so the call goes as it would with the mic open.
  const again = 'Calling Anna again. [[call: +41 79 123 45 67 | Anna Muster | no-voice-cancel]]';
  const el = await spoken({ role: 'assistant', text: again }, null,
    [{ role: 'user', text: 'Call Anna' }, { role: 'assistant', text: ANNA }]);
  await until(() => el._calling, 'the call waits');
  assert.equal(el._calling.voiceCancel, false, 'speech cannot cancel it');
  assert.equal(el._recState, 'idle', 'the mic is shut');
  document.visibilityState = 'hidden';
  document.hidden = true;
  try {
    el._onContVisibility();
    await wait(6000);
  } finally {
    document.visibilityState = 'visible';
    document.hidden = false;
  }
  assert.equal(placed.length, 0, 'no call');
  assert.ok(!el._calling && !callRow(el), 'no call row');
  assert.ok(!el._contArmed, 'the loop stopped');
  el.disconnectedCallback();
});

const CANCELLED = { number: '+41791234567', name: 'Anna Muster' };

await ok('a take sent by touch while a call waits cancels it, and tells Ara', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  // Spoken too softly for the detector, and sent with the row's ➤.
  transcript = 'no, stop, call Ben instead';
  el._finishRecording('send');
  await until(() => posts.length === 1, 'the take goes out');
  assert.deepEqual(posts[0].cancelled_call, CANCELLED, 'with the call it cancelled');
  assert.ok(!el._calling && !callRow(el), 'no call row');
  await wait(7000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok('one sent by touch with no words in it lets the call go ahead', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  el._finishRecording('send');
  await until(() => placed.length === 1, 'the call is placed after all');
  assert.equal(posts.length, 0, 'nothing was sent');
  el.disconnectedCallback();
});

await ok('where the page cannot hear the stream, what was said decides the call', async () => {
  webAudio = false;
  transcript = 'no, stop';
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  await until(() => posts.length === 1, 'the take goes out when the call is due');
  assert.deepEqual(posts[0].cancelled_call, CANCELLED, 'with the call it cancelled');
  await wait(7000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok('a call resumed after an empty transcript is dropped when the page was hidden meanwhile', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  await wait(500);
  let release;
  transcribing = new Promise((r) => { release = r; });
  level = 0.3;
  await until(() => !el._calling, 'noise cancelled the call');
  assert.equal(placed.length, 0, 'not by placing it');
  await wait(800);
  level = 0;
  await until(() => el._recState === 'idle', 'the take went to be transcribed');
  document.visibilityState = 'hidden';
  document.hidden = true;
  try {
    el._onContVisibility();
    release();
    await wait(7000);
  } finally {
    document.visibilityState = 'visible';
    document.hidden = false;
  }
  assert.equal(placed.length, 0, 'no call');
  assert.equal(posts.length, 0, 'nothing was sent');
  assert.ok(!el._calling && !callRow(el), 'no call row');
  assert.ok(!el._contArmed && !el._contBusy(), 'the loop stopped');
  el.disconnectedCallback();
});

await ok('the flag in place of the name is the flag, and never shown or read as a name', async () => {
  const again = 'Calling again. [[call: +41 79 123 45 67 | no-voice-cancel]]';
  const el = await spoken({ role: 'assistant', text: again }, null,
    [{ role: 'user', text: 'Call Anna' }, { role: 'assistant', text: ANNA }]);
  await until(() => el._calling, 'the call waits');
  assert.equal(el._calling.voiceCancel, false, 'speech cannot cancel it');
  assert.equal(el._calling.name, '');
  assert.equal(el._recState, 'idle', 'the mic is shut');
  el.render();
  assert.ok(!el.shadowRoot.innerHTML.includes('no-voice-cancel'), el.shadowRoot.innerHTML);
  assert.ok(said.some((t) => t.includes('Calling again. +41 79 123 45 67')), said.join(' / '));
  assert.ok(!said.some((t) => /no-voice-cancel/i.test(t)), said.join(' / '));
  await until(() => placed.length === 1, 'and is placed');
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

await ok('a cancelling take the service worker sends carries the call there, and no later turn does', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  swOn = true;
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  transcript = 'no, stop, call Ben instead';
  el._finishRecording('send');
  await until(() => swJobs.length === 1, 'the take goes to the worker');
  assert.deepEqual(swJobs[0].call, CANCELLED, 'with the call it cancelled');
  assert.ok(swJobs[0].send, 'as a sent dictation');
  await wait(500);
  assert.equal(posts.length, 0, 'the worker sent it, not the page as well');
  swOn = false;
  await el._send('One more thing');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].cancelled_call, undefined, 'the next turn carries no cancelled call');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok('a cancelling take the service worker could not send waits in the box, and its call stays cancelled', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  swOn = true;
  swOutcome = { unsent: 'failed' };
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  transcript = 'no, stop, call Ben instead';
  el._finishRecording('send');
  await until(() => swJobs.length === 1, 'the take goes to the worker');
  await until(() => { el.render(); return el.shadowRoot.innerHTML.includes('be sent. It is in the box'); }, 'the note says so');
  assert.equal(posts.length, 0, 'not sent by the page either');
  swOn = false;
  await el._send(transcript);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].cancelled_call, undefined, 'sent by hand later, it no longer cancels a call');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok("a cancelling take an earlier page left is sent by the next one, with its call, while it is fresh", async () => {
  swOn = true;
  const el = await spoken({ role: 'assistant', text: NOTED }, (e) => leftTake(e.thread.id, 'yes, go ahead', 5000, CANCELLED));
  await until(() => posts.length === 1, 'the page that took the take over sends it');
  assert.equal(posts[0].message, 'yes, go ahead');
  assert.deepEqual(posts[0].cancelled_call, CANCELLED, 'with the call it cancelled');
  el.disconnectedCallback();
});

await ok('...fresh counted from its transcript, where a slow one came long after the recording', async () => {
  swOn = true;
  const el = await spoken({ role: 'assistant', text: NOTED },
    (e) => leftTake(e.thread.id, 'yes, go ahead', 200 * 1000, CANCELLED, 5000));
  await until(() => posts.length === 1, 'the page that took the take over sends it');
  assert.deepEqual(posts[0].cancelled_call, CANCELLED, 'with the call it cancelled');
  el.disconnectedCallback();
});

await ok('...but an old one goes out as words alone, and no call is placed with the mic shut', async () => {
  swOn = true;
  // Its page died before it could send it; days later the thread is opened
  // again, the mode still on.
  const el = await spoken({ role: 'assistant', text: NOTED },
    (e) => leftTake(e.thread.id, 'yes, go ahead', 3 * 24 * 3600 * 1000, CANCELLED));
  await until(() => posts.length === 1, 'the page that took the take over sends it');
  assert.equal(posts[0].message, 'yes, go ahead');
  assert.equal(posts[0].cancelled_call, undefined, 'a call that old is not put to Ara again');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok("a take another page left does not borrow the call this page's own take cancelled", async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  swOn = true;
  swOutcome = {};   // this page is there: it sends its own take
  let release;
  swHold = new Promise((r) => { release = r; });
  await wait(500);
  transcript = 'no, call Ben instead';
  level = 0.3;
  await until(() => !el._calling, 'speech cancelled the call');
  await wait(800);
  level = 0;
  await until(() => swJobs.length === 1, "this page's take is being transcribed");
  assert.deepEqual(swJobs[0].call, CANCELLED);
  // Meanwhile a tab on the same thread closes, leaving a sent take of its
  // own: this page takes it over (a resync) and sends it.
  leftTake(el.thread.id, 'and remind me at six', 5000, null);
  dispatchEvent({ type: 'pageshow', persisted: true });
  await until(() => posts.length === 1, 'the left take is sent');
  assert.equal(posts[0].message, 'and remind me at six');
  assert.equal(posts[0].cancelled_call, undefined, "it carries no call: the one cancelled is this page's take's");
  release();
  await until(() => posts.length === 2, "this page's take goes out");
  assert.equal(posts[1].message, 'no, call Ben instead');
  assert.deepEqual(posts[1].cancelled_call, CANCELLED, 'with the call it cancelled');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok("a cancelling take that meets an adopted send on the wire leaves no call for a later turn", async () => {
  const el = await spoken({ role: 'assistant', text: ANNA });
  await until(() => el._calling && el._recState === 'recording', 'the call waits with the mic open');
  swOn = true;
  swOutcome = {};   // this page is there: it sends its own take
  let release;
  swHold = new Promise((r) => { release = r; });
  await wait(500);
  transcript = 'no, stop';
  level = 0.3;
  await until(() => !el._calling, 'speech cancelled the call');
  await wait(800);
  level = 0;
  await until(() => swJobs.length === 1, "this page's take is being transcribed");
  // A take another tab left on the thread is adopted and sent, and is still
  // on the wire when this page's own transcript comes in.
  let open;
  postGate = new Promise((r) => { open = r; });
  leftTake(el.thread.id, 'and remind me at six', 5000, null);
  dispatchEvent({ type: 'pageshow', persisted: true });
  await until(() => posts.length === 1, 'the left take is on the wire');
  release();
  await until(() => swJobs[0].done, "this page's transcript is in");
  await wait(500);
  assert.equal(posts.length, 1, "this page's take does not go out over the send on the wire");
  open();
  postGate = null;
  await wait(500);
  assert.ok([...store.values()].some((v) => v.includes('no, stop')), 'it waits in the box');
  swOn = false;
  await el._send('what is the weather tomorrow?', { fromDraft: false });
  assert.equal(posts[posts.length - 1].message, 'what is the weather tomorrow?');
  assert.equal(posts[posts.length - 1].cancelled_call, undefined, 'a later turn carries no cancelled call');
  await wait(6000);
  assert.equal(placed.length, 0, 'no call');
  el.disconnectedCallback();
});

await ok('an answer that ends the mode places no call, though it ends in a directive', async () => {
  const el = await spoken({ role: 'assistant', text: ANNA }, (e) => {
    // Ara stamped the thread in the same turn (conversation-push.py --end-continuous).
    threads[e.thread.id].continuous_off = new Date(Date.now() + 1000).toISOString();
  });
  await wait(1000);
  assert.ok(!el._contOn(), 'the mode is off');
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
