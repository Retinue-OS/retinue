#!/usr/bin/env python3
"""Behavioural tests for the dashboard's read-aloud player (webapp/components/speech.js).

The module runs in the browser on top of `speechSynthesis`, so the test drives
it under Node with a scripted stand-in for the engine: the test decides when an
utterance starts, ends or fails, and asserts what the reader asks the engine to
do and what it reports back. This pins the behaviour the conversation player
depends on — sentence-sized pieces, positions, pause/seek/skip restarting at a
piece boundary, the cancel/speak grace, the engine quirks that used to make a
tap on the loudspeaker do nothing.

Standalone like the rest of the suite. Needs `node` on PATH (GitHub's runners
have it); without it the test reports a skip and passes, so the Python-only
suite is never blocked by it.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPEECH_JS = os.path.join(HERE, "..", "webapp", "components", "speech.js")

HARNESS = r"""
import assert from 'node:assert/strict';
import { Reader, chunk } from './speech.mjs';

// ── A scripted speechSynthesis ───────────────────────────────────────────────
class FakeUtterance {
  constructor(text) { this.text = text; this.lang = ''; this.voice = null;
    this.onstart = null; this.onend = null; this.onerror = null; this.onboundary = null; }
}
const synth = {
  speaking: false, pending: false, paused: false, queue: [], log: [],
  getVoices() {
    return [
      { name: 'de-net', lang: 'de-DE', localService: false },
      { name: 'de-local', lang: 'de-DE', localService: true },
      { name: 'de-ch', lang: 'de-CH', localService: true },
      { name: 'en', lang: 'en-US', localService: true },
    ];
  },
  speak(u) { this.log.push('speak:' + u.text.slice(0, 12)); this.queue.push(u); this.pending = true; },
  cancel() {
    this.log.push('cancel');
    const q = this.queue; this.queue = []; this.speaking = false; this.pending = false;
    // Chrome reports the cancelled utterance as an error, synchronously.
    for (const u of q) if (u.onerror) u.onerror({ error: 'interrupted' });
  },
  resume() { this.log.push('resume'); this.paused = false; },
  pause() { this.paused = true; },
  // Test-side controls (each needs a queued utterance — a missing one is a
  // test bug, most often a speak that was deferred and not waited for).
  head(what) { const u = this.queue[0]; if (!u) throw new Error(what + ': nothing queued'); return u; },
  start() { const u = this.head('start'); this.speaking = true; this.pending = false; if (u.onstart) u.onstart({}); },
  boundary(i) { const u = this.head('boundary'); if (u.onboundary) u.onboundary({ charIndex: i }); },
  finish() { const u = this.head('finish'); this.queue.shift(); this.speaking = false; if (u.onend) u.onend({}); },
  fail(kind) { const u = this.head('fail'); this.queue.shift(); this.speaking = false; this.pending = false; if (u.onerror) u.onerror({ error: kind }); },
  current() { return this.queue[0] || null; },
};
globalThis.window = globalThis;
globalThis.speechSynthesis = synth;
globalThis.SpeechSynthesisUtterance = FakeUtterance;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const GRACE = 120; // > RESTART_DELAY_MS
// A speak within the grace of a cancel is deferred; wait it out where the
// test needs the engine to have been handed the next piece.
const settle = () => sleep(GRACE);
let passed = 0;
function ok(name, fn) { return Promise.resolve().then(fn).then(() => { passed++; console.log('  ok  ' + name); }); }

const sentence = (n, w) => Array.from({ length: n }, (_, i) => `${w}${i} word word word word.`).join(' ');
const LONG = sentence(30, 'Satz');           // ~30 short sentences, several pieces
const events = [];
const states = [];
const reader = new Reader((id) => states.push(id));
reader.onprogress = (ev) => events.push(ev);
const spoken = () => synth.log.filter((l) => l.startsWith('speak:')).length;

// ── chunk() ──────────────────────────────────────────────────────────────────
await ok('chunk: empty and short text', () => {
  assert.deepEqual(chunk(''), []);
  assert.deepEqual(chunk('   '), []);
  assert.deepEqual(chunk('Hello there.'), ['Hello there.']);
});
await ok('chunk: pieces stay under the limit and keep every word', () => {
  const pieces = chunk(LONG);
  assert.ok(pieces.length > 3, 'expected several pieces');
  for (const p of pieces) assert.ok(p.length <= 180, `piece too long: ${p.length}`);
  assert.equal(pieces.join(' ').replace(/\s+/g, ' '), LONG.replace(/\s+/g, ' '));
});
await ok('chunk: short sentences are bundled, an overlong sentence is cut on spaces', () => {
  assert.deepEqual(chunk('A. B. C.'), ['A. B. C.']);
  const run = Array.from({ length: 120 }, (_, i) => `w${i}`).join(' '); // no punctuation
  const pieces = chunk(run);
  assert.ok(pieces.length >= 3);
  for (const p of pieces) { assert.ok(p.length <= 180); assert.ok(!p.startsWith(' ') && !p.endsWith(' ')); }
  assert.equal(pieces.join(' '), run);
});

// ── load(): positions ────────────────────────────────────────────────────────
await ok('load: piece offsets are cumulative and the reader waits, paused', () => {
  const n = reader.load([{ id: 'm1', lang: 'de', text: LONG }]);
  assert.ok(n > 3);
  assert.equal(reader.state, 'paused');
  assert.equal(reader.loaded, true);
  assert.equal(reader.speaking, false);
  let at = 0;
  for (const p of reader.pieces) { assert.equal(p.start, at); assert.equal(p.end, at + p.text.length); at = p.end; }
  assert.equal(reader.total, at);
  assert.equal(reader.pos, 0);
  assert.equal(reader.currentId, 'm1');
  assert.equal(events.at(-1).event, 'load');
  assert.equal(synth.log.length, 0, 'an idle engine is not touched by a load');
});
await ok('load at a fraction lands on the piece containing it', () => {
  reader.load([{ id: 'm1', lang: 'de', text: LONG }], { fraction: 0.5 });
  const p = reader.pieces[reader.pos];
  assert.ok(p.start <= reader.total * 0.5 && reader.total * 0.5 < p.end);
  assert.ok(reader.pos > 0);
  assert.equal(reader.progress().offset, p.start, 'paused: the position is the piece start');
  reader.load([{ id: 'm1', lang: 'de', text: LONG }], { fraction: 5 });
  assert.equal(reader.pos, reader.pieces.length - 1, 'beyond the end clamps to the last piece');
});

// ── play(): a plain speak inside the tap, then piece after piece ─────────────
reader.stop();
synth.log.length = 0; events.length = 0; states.length = 0;
await ok('play from idle speaks at once, with no cancel first', () => {
  assert.equal(reader.play([{ id: 'm1', lang: 'de', text: LONG }]), true);
  assert.equal(reader.state, 'playing');
  assert.equal(reader.starting, true);
  assert.deepEqual(synth.log, ['speak:' + reader.pieces[0].text.slice(0, 12)]);
  assert.equal(states[0], 'm1');
  assert.equal(reader._utter, synth.current(), 'the live utterance is held (Chrome GC quirk)');
  const u = synth.current();
  assert.equal(u.lang, 'de');
  assert.equal(u.voice.name, 'de-local', 'a local voice for the language wins over a network one');
});
await ok('progress moves with the engine: start, boundaries, then the next piece', async () => {
  const p0 = reader.pieces[0];
  assert.equal(events.filter((e) => e.event === 'piece').length, 1, 'one piece event per hand-over');
  const n = events.length;
  synth.start();
  assert.equal(reader.starting, false);
  assert.equal(events.length, n, 'the engine start itself announces nothing; ticks carry it');
  assert.equal(reader.progress().offset, 0);
  synth.boundary(10);
  assert.ok(reader.progress().offset >= 10, 'a word boundary moves the position');
  assert.ok(reader.progress().offset < p0.text.length);
  await sleep(300);
  assert.ok(events.some((e) => e.event === 'tick'), 'ticks while playing');
  synth.finish();
  assert.equal(reader.pos, 1);
  assert.equal(spoken(), 2, 'the next piece is handed over as soon as one ends');
  assert.equal(events.filter((e) => e.event === 'piece').length, 2, 'still one piece event per hand-over');
  assert.equal(reader.progress().offset, reader.pieces[1].start);
});

// ── pause / resume ───────────────────────────────────────────────────────────
await ok('pause silences the engine and keeps the piece; resume restarts that piece', async () => {
  synth.start();
  synth.boundary(20);
  const piece = reader.pieces[reader.pos];
  synth.log.length = 0;
  reader.pause();
  assert.equal(reader.state, 'paused');
  assert.deepEqual(synth.log, ['cancel']);
  assert.equal(reader.progress().offset, piece.start);
  assert.equal(events.at(-1).event, 'pause');
  assert.equal(reader.currentId, 'm1', 'still loaded');
  await settle();
  synth.log.length = 0;
  reader.resume();
  assert.equal(reader.state, 'playing');
  assert.deepEqual(synth.log, ['speak:' + piece.text.slice(0, 12)], 'an idle engine: speak right away');
  assert.equal(reader.pieces[reader.pos], piece);
});
await ok('toggle flips between the two', async () => {
  reader.toggle(); assert.equal(reader.state, 'paused');
  reader.toggle(); assert.equal(reader.state, 'playing');
  await settle();
});

// ── the cancel/speak grace ───────────────────────────────────────────────────
await ok('a restart while the engine is busy cancels, then speaks after a grace', async () => {
  synth.start();
  synth.log.length = 0;
  reader.seek(0.9);
  assert.equal(synth.log[0], 'cancel');
  assert.equal(synth.log.length, 1, 'nothing spoken in the same task');
  assert.equal(reader.starting, true);
  await sleep(GRACE);
  assert.equal(synth.log.length, 2);
  const p = reader.pieces[reader.pos];
  assert.equal(synth.log[1], 'speak:' + p.text.slice(0, 12));
  assert.ok(p.start <= reader.total * 0.9 && reader.total * 0.9 < p.end, 'seek lands on the right piece');
});
await ok('a superseded restart never speaks (only the latest one does)', async () => {
  synth.start();
  synth.log.length = 0;
  reader.seek(0.2);
  reader.seek(0.4);
  await sleep(GRACE);
  const speaks = synth.log.filter((l) => l.startsWith('speak:'));
  assert.equal(speaks.length, 1);
  const p = reader.pieces[reader.pos];
  assert.ok(p.start <= reader.total * 0.4 && reader.total * 0.4 < p.end);
});
await ok('the cancelled utterance\'s late error is ignored', async () => {
  synth.start();
  const before = reader.pos;
  const stale = synth.current();
  reader.seek(0.6);
  stale.onerror({ error: 'interrupted' });
  stale.onend({});
  assert.equal(reader.state, 'playing');
  assert.notEqual(reader.pos, before);
  await sleep(GRACE);
});

// ── seeking while paused ─────────────────────────────────────────────────────
await ok('seek while paused moves the position and speaks nothing', () => {
  synth.start();
  reader.pause();
  synth.log.length = 0;
  reader.seek(0);
  assert.equal(reader.pos, 0);
  assert.equal(reader.state, 'paused');
  assert.equal(events.at(-1).event, 'seek');
  assert.equal(events.at(-1).fraction, 0);
  assert.deepEqual(synth.log, []);
});

// ── back / forward ───────────────────────────────────────────────────────────
await ok('back and forward step one piece; back restarts a piece played for a while', async () => {
  reader.seek(0.5);
  const i = reader.pos;
  reader.forward(); assert.equal(reader.pos, i + 1);
  reader.back(); assert.equal(reader.pos, i);
  reader.back(); assert.equal(reader.pos, i - 1);
  await settle();
  reader.resume();
  await settle();
  synth.start();
  reader._startedAt = Date.now() - 5000;
  synth.log.length = 0;
  reader.back();
  assert.equal(reader.pos, i - 1, 'a well-played piece restarts instead of stepping back');
  await sleep(GRACE);
  assert.equal(synth.log.filter((l) => l.startsWith('speak:')).length, 1);
  reader.pause();
});
await ok('forward past the last piece finishes', () => {
  reader.seek(1);
  assert.equal(reader.pos, reader.pieces.length - 1);
  events.length = 0;
  reader.forward();
  assert.equal(reader.state, 'idle');
  assert.deepEqual(events.map((e) => e.event), ['end', 'stop']);
  assert.equal(events[0].fraction, 1, 'the end reports a full read');
  assert.equal(reader.currentId, null);
  assert.equal(states.at(-1), null);
});

// ── the natural end ──────────────────────────────────────────────────────────
await ok('reading to the end reports end, then stop, and unloads', async () => {
  reader.play([{ id: 'a', lang: 'en', text: 'One. Two.' }]);
  await settle();
  synth.start();
  events.length = 0;
  synth.finish();
  assert.equal(reader.state, 'idle');
  assert.equal(reader.loaded, false);
  assert.deepEqual(events.map((e) => e.event), ['end', 'stop']);
  assert.equal(reader.pieces.length, 0);
});

// ── item-wise skip (the news page) ───────────────────────────────────────────
await ok('skip jumps past the whole item and announces the next one', async () => {
  states.length = 0;
  reader.play([{ id: 'n1', lang: 'en', text: LONG }, { id: 'n2', lang: 'en', text: 'Second item.' }]);
  await settle();
  synth.start();
  synth.log.length = 0;
  reader.skip();
  assert.equal(reader.currentId, 'n2');
  assert.deepEqual(states, ['n1', 'n2']);
  await sleep(GRACE);
  assert.deepEqual(synth.log, ['cancel', 'speak:Second item.']);
  synth.start();
  reader.skip();
  assert.equal(reader.state, 'idle', 'skipping past the last item ends the reading');
  assert.equal(states.at(-1), null);
});

// ── engine failures ──────────────────────────────────────────────────────────
await ok('an engine error pauses with the reason instead of racing on; resume retries the piece', async () => {
  events.length = 0;
  reader.play([{ id: 'e', lang: 'en', text: LONG }]);
  await settle();
  const piece = reader.pieces[0];
  synth.fail('not-allowed');
  assert.equal(reader.state, 'paused');
  assert.equal(reader.error, 'not-allowed');
  assert.equal(events.at(-1).event, 'error');
  assert.equal(events.at(-1).error, 'not-allowed');
  assert.equal(reader.pos, 0);
  synth.log.length = 0;
  reader.resume();
  assert.equal(reader.error, null);
  assert.deepEqual(synth.log, ['speak:' + piece.text.slice(0, 12)]);
});
await ok('a paused engine is resumed before speaking (Chrome after cancel)', async () => {
  reader.stop();
  await settle();
  synth.paused = true;
  synth.log.length = 0;
  reader.play([{ id: 'p', lang: 'en', text: 'Hi there.' }]);
  assert.deepEqual(synth.log, ['resume', 'speak:Hi there.']);
  synth.paused = false;
  reader.stop();
});
await ok('a zombie engine (speaking with nothing of ours) is cancelled, then spoken to after the grace', async () => {
  synth.speaking = true;
  synth.log.length = 0;
  reader.play([{ id: 'z', lang: 'en', text: 'After a page hop.' }]);
  assert.deepEqual(synth.log, ['cancel']);
  await sleep(GRACE);
  assert.deepEqual(synth.log, ['cancel', 'speak:After a page']);
});
await ok('resync re-speaks the piece a backgrounded engine dropped, and leaves a live one alone', async () => {
  synth.queue = []; synth.speaking = false; synth.pending = false;   // the engine forgot it
  synth.log.length = 0;
  reader.resync();
  await settle();
  assert.deepEqual(synth.log, ['cancel', 'speak:After a page']);
  synth.start();
  synth.log.length = 0;
  reader.resync();
  assert.deepEqual(synth.log, [], 'still speaking: nothing to do');
  reader.pause();
  synth.log.length = 0;
  reader.resync();
  assert.deepEqual(synth.log, [], 'paused: nothing to do');
  reader.stop();
});

// ── the speaking-rate estimate ───────────────────────────────────────────────
await ok('the measured rate feeds the position estimate and the time left', async () => {
  reader.play([{ id: 'r', lang: 'en', text: LONG }]);
  await settle();
  synth.start();
  const p0 = reader.pieces[0];
  reader._startedAt = Date.now() - 1000 * (p0.text.length / 30);   // read at 30 chars/s
  synth.finish();
  assert.ok(Math.abs(reader._rate - (0.7 * 15 + 0.3 * 30)) < 0.5, `rate ${reader._rate}`);
  synth.start();
  reader._startedAt = Date.now() - 2000;
  const pr = reader.progress();
  assert.ok(pr.offset > reader.pieces[1].start, 'after two seconds the estimate is into the piece');
  assert.ok(pr.offset < reader.pieces[1].end, 'but never past it');
  assert.ok(pr.remaining > 0 && pr.remaining < 60);
  reader.stop();
});

// ── voice choice ─────────────────────────────────────────────────────────────
await ok('voice: exact tag, then same language elsewhere, then none', async () => {
  const playNow = async (lang, text) => { reader.play([{ id: 'v', lang, text }]); await settle(); };
  await playNow('de-CH', 'Grüezi.');
  assert.equal(synth.current().voice.name, 'de-ch');
  await playNow('de-AT', 'Servus.');
  assert.equal(synth.current().voice.name, 'de-local');
  await playNow('fr', 'Salut.');
  assert.equal(synth.current().voice, null);
  assert.equal(synth.current().lang, 'fr', 'the language tag is still passed for the engine to match');
  await playNow('', 'Untagged.');
  assert.equal(synth.current().voice, null);
  reader.stop();
});

await ok('chunk: an unbroken token is cut at the limit, nothing lost', () => {
  const token = 'x'.repeat(500);
  const pieces = chunk(token);
  assert.ok(pieces.length >= 3);
  for (const p of pieces) assert.ok(p.length <= 180, `piece too long: ${p.length}`);
  assert.equal(pieces.join(''), token);
  for (const p of chunk('Short. ' + 'y'.repeat(400) + ' tail.')) assert.ok(p.length <= 180);
  // A hard cut must not land between the halves of a surrogate pair.
  const emoji = 'a' + '\u{1F600}'.repeat(100);
  const ep = chunk(emoji);
  assert.ok(ep.length >= 2);
  for (const p of ep) {
    assert.ok(p.length <= 180);
    assert.ok(!/[\uD800-\uDBFF]$/.test(p), 'a piece ends in a lone high surrogate');
    assert.ok(!/^[\uDC00-\uDFFF]/.test(p), 'a piece starts with a lone low surrogate');
  }
  assert.equal(ep.join(''), emoji);
});
await ok('another reader\'s speech is not cancelled by a load or a stop', () => {
  synth.speaking = true;                 // the news reader is talking
  synth.log.length = 0;
  reader.load([{ id: 'o', lang: 'en', text: 'Later.' }]);
  reader.stop();
  assert.deepEqual(synth.log, []);
  synth.speaking = false;
});
await ok('a live interruption (another reader, the platform) pauses with the place kept', async () => {
  reader.play([{ id: 'i', lang: 'en', text: LONG }]);
  await settle();
  synth.start();
  const piece = reader.pieces[reader.pos];
  events.length = 0;
  synth.fail('interrupted');
  assert.equal(reader.state, 'paused');
  assert.equal(reader.error, null);
  assert.deepEqual(events.map((e) => e.event), ['pause']);
  assert.equal(reader._ticker, null, 'no ticker while paused');
  synth.log.length = 0;
  reader.resume();
  await settle();
  assert.equal(synth.log.at(-1), 'speak:' + piece.text.slice(0, 12), 'resumes at the interrupted piece');
  reader.stop();
});
await ok('a speak() that throws pauses with the reason and leaves no ticker behind', async () => {
  await settle();
  const real = synth.speak;
  synth.speak = () => { throw new Error('engine down'); };
  events.length = 0;
  reader.play([{ id: 't', lang: 'en', text: 'Try me.' }]);
  assert.equal(reader.state, 'paused');
  assert.equal(reader.error, 'error');
  assert.equal(reader._ticker, null, 'the ticker is cleared');
  assert.equal(events.at(-1).event, 'error', 'the error is the last word');
  synth.speak = real;
  reader.resume();
  assert.equal(reader.state, 'playing');
  assert.equal(synth.log.at(-1), 'speak:Try me.');
  reader.stop();
});

await ok('a second reader taking the engine halts the first, whose stray events change nothing', async () => {
  await settle();
  const otherEvents = [];
  const other = new Reader();
  other.onprogress = (ev) => otherEvents.push(ev);
  reader.play([{ id: 'first', lang: 'en', text: LONG }]);
  await settle();
  synth.start();
  const firstUtter = synth.current();
  const firstPos = reader.pos;
  events.length = 0;
  synth.log.length = 0;
  // Some engines answer a cancel with a plain `end` on the live utterance.
  const realCancel = synth.cancel;
  synth.cancel = function () {
    this.log.push('cancel');
    const q = this.queue; this.queue = []; this.speaking = false; this.pending = false;
    for (const u of q) if (u.onend) u.onend({});
  };
  other.play([{ id: 'second', lang: 'en', text: 'Other reader.' }]);
  assert.equal(reader.state, 'paused', 'the displaced reader is paused');
  assert.equal(reader.error, null);
  assert.deepEqual(events.map((e) => e.event), ['pause']);
  assert.equal(reader.pos, firstPos, 'at the same passage');
  assert.equal(synth.log[0], 'cancel');
  await settle();
  assert.deepEqual(synth.log, ['cancel', 'speak:Other reader'], 'and the new reader speaks after the grace');
  // Whatever else the stale utterance reports is ignored too.
  firstUtter.onend({}); firstUtter.onerror({ error: 'interrupted' });
  assert.equal(reader.state, 'paused');
  assert.equal(reader.pos, firstPos);
  assert.deepEqual(events.map((e) => e.event), ['pause'], 'no end, no progress from the first reader');
  // Resuming the first takes the engine back and pauses the second.
  synth.start();
  synth.log.length = 0;
  reader.resume();
  assert.equal(other.state, 'paused');
  assert.equal(otherEvents.at(-1).event, 'pause');
  await settle();
  assert.equal(synth.log.at(-1), 'speak:' + reader.pieces[firstPos].text.slice(0, 12));
  reader.stop(); other.stop();
  synth.cancel = realCancel;
});
await ok('a rapid hand-off honours the other reader\'s cancel grace', async () => {
  await settle();
  const other = new Reader();
  reader.play([{ id: 'first', lang: 'en', text: LONG }]);
  await settle();
  synth.start();
  const firstPos = reader.pos;
  synth.log.length = 0;
  other.play([{ id: 'second', lang: 'en', text: 'Other reader.' }]);   // cancels, speak deferred
  reader.resume();                                                       // right away: still in the grace
  assert.deepEqual(synth.log, ['cancel'], 'nothing spoken in the same task as the cancel');
  assert.equal(reader.state, 'playing');
  assert.equal(other.state, 'paused', 'the second reader is displaced before it ever spoke');
  await settle();
  assert.deepEqual(synth.log, ['cancel', 'speak:' + reader.pieces[firstPos].text.slice(0, 12)],
    'the first reader speaks after the grace, and the second not at all');
  reader.stop(); other.stop();
});

await ok('stop from any state unloads and reports it', () => {
  reader.load([{ id: 's', lang: 'en', text: 'x.' }]);
  events.length = 0;
  reader.stop();
  assert.equal(reader.state, 'idle');
  assert.deepEqual(events.map((e) => e.event), ['stop']);
  reader.stop();
  assert.equal(reader.state, 'idle');
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the speech.js behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # Node treats a bare .js as CommonJS; the module is ESM, so import a copy.
        shutil.copy(SPEECH_JS, os.path.join(tmp, "speech.mjs"))
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: speech.js behaviour test")
            return 1
    print("PASS: speech.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
