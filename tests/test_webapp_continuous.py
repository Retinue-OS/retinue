#!/usr/bin/env python3
"""Behavioural tests for continuous conversation's pure logic (webapp/components/continuous.js).

The loop itself runs in a thread element in the browser — microphone,
recorder, read-aloud — but what it decides is decided here, without a page:
when a take has ended (the voice-activity detector, fed one level per frame
with the time it was measured), which thread a wake opens, and the per-device
settings and per-thread flags it keeps. The test drives those under Node with
synthetic levels and a scripted localStorage, so the timings are exact.

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
CONTINUOUS_JS = os.path.join(HERE, "..", "webapp", "components", "continuous.js")

HARNESS = r"""
import assert from 'node:assert/strict';
import {
  Vad, VAD_DEFAULTS, rms, pickWakeThread, WAKE_WINDOW_MS,
  modeEntries, isContinuous, setContinuous, touchContinuous, MODE_KEY,
  voiceTimeouts, setVoiceTimeouts, DEFAULT_SILENCE_MS, DEFAULT_NO_SPEECH_MS,
} from './continuous.mjs';

// ── A scripted localStorage ──────────────────────────────────────────────────
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};

let passed = 0;
function ok(name, fn) { fn(); passed++; console.log('  ok  ' + name); }

// Feed a level for a span of time at a frame rate; returns every event.
const FRAME = 50;
function run(vad, from, to, level) {
  const out = [];
  for (let t = from; t < to; t += FRAME) {
    const ev = vad.feed(typeof level === 'function' ? level(t) : level, t);
    if (ev) out.push({ t, ...ev });
  }
  return out;
}
const QUIET = 0.004;
const SPEECH = 0.15;

// ── The detector ─────────────────────────────────────────────────────────────
ok('speech then a pause of silenceMs ends the take', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000 });
  let evs = run(vad, 0, 1000, QUIET);
  assert.deepEqual(evs, []);
  evs = run(vad, 1000, 3000, SPEECH);
  assert.equal(evs.length, 1);
  assert.equal(evs[0].type, 'speech');
  // Confirmed after minSpeechMs of level, not on the first loud frame.
  assert.ok(evs[0].t >= 1000 + VAD_DEFAULTS.minSpeechMs, `speech at ${evs[0].t}`);
  assert.equal(vad.state, 'speaking');
  assert.ok(vad.heard);
  evs = run(vad, 3000, 6000, QUIET);
  assert.equal(evs.length, 1);
  assert.equal(evs[0].type, 'end');
  assert.equal(evs[0].reason, 'silence');
  // Counted from the last voiced frame (2950): ends 1500 ms after it.
  assert.equal(evs[0].t, 2950 + 1500);
  assert.equal(vad.state, 'done');
});

ok('pauses shorter than silenceMs keep the take open', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000 });
  run(vad, 0, 500, QUIET);
  // Speak 1 s, pause 1 s, speak 1 s, pause 1.2 s, speak — never 1.5 s quiet.
  const level = (t) => {
    if (t < 1500) return SPEECH;
    if (t < 2500) return QUIET;
    if (t < 3500) return SPEECH;
    if (t < 4700) return QUIET;
    if (t < 5500) return SPEECH;
    return QUIET;
  };
  const evs = run(vad, 500, 9000, level);
  assert.deepEqual(evs.map((e) => e.type), ['speech', 'end']);
  assert.equal(evs[1].t, 5450 + 1500);
});

ok('no speech closes the mic after noSpeechMs, not before', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000 });
  let evs = run(vad, 0, 7950, QUIET);
  assert.deepEqual(evs, []);
  evs = run(vad, 7950, 9000, QUIET);
  assert.equal(evs.length, 1);
  assert.equal(evs[0].type, 'timeout');
  assert.equal(evs[0].t, 8000);
  assert.equal(vad.heard, false);
  // Done is done: no further events.
  assert.deepEqual(run(vad, 9000, 12000, SPEECH), []);
});

ok('the timeout counts from the first frame, whenever that comes', () => {
  const vad = new Vad({ noSpeechMs: 3000 });
  const evs = run(vad, 100000, 104000, QUIET);
  assert.deepEqual(evs.map((e) => [e.type, e.t]), [['timeout', 103000]]);
});

ok('the warm-up (the chime) is not speech, and seeds the floor from its quietest frame', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 4000 });
  // A loud chime for 200 ms, then the quiet room.
  const evs = run(vad, 0, 5000, (t) => (t < 200 ? 0.5 : QUIET));
  assert.deepEqual(evs.map((e) => e.type), ['timeout']);
  assert.ok(vad.floor < 0.01, `floor ${vad.floor}`);
});

ok('a click shorter than minSpeechMs is not speech', () => {
  const vad = new Vad({ noSpeechMs: 3000 });
  const evs = run(vad, 0, 4000, (t) => (t >= 1000 && t < 1050 ? 0.6 : QUIET));
  assert.deepEqual(evs.map((e) => e.type), ['timeout']);
});

ok('a steady noisy room is learned, and speech above it is still heard', () => {
  const NOISE = 0.03; // well above minLevel: a fan, a car
  const vad = new Vad({ silenceMs: 1000, noSpeechMs: 6000 });
  let evs = run(vad, 0, 2000, NOISE);
  assert.deepEqual(evs, [], 'the noise itself is not speech');
  assert.ok(Math.abs(vad.floor - NOISE) < 0.005, `floor ${vad.floor}`);
  assert.ok(vad.threshold() > NOISE);
  evs = run(vad, 2000, 3000, 0.2);
  assert.deepEqual(evs.map((e) => e.type), ['speech']);
  // Back to the noise: that is silence here, and the take ends.
  evs = run(vad, 3000, 5000, NOISE);
  assert.deepEqual(evs.map((e) => [e.type, e.reason]), [['end', 'silence']]);
});

ok('the floor follows a room that grows quieter quickly, and a louder one slowly', () => {
  const vad = new Vad({ noSpeechMs: 60000 });
  run(vad, 0, 1000, 0.02);
  const before = vad.floor;
  run(vad, 1000, 1500, 0.003);
  assert.ok(vad.floor < 0.005, `down quickly: ${vad.floor}`);
  const low = vad.floor;
  // Slightly louder, below the threshold: crept towards, not jumped to.
  run(vad, 1500, 1600, 0.006);
  assert.ok(vad.floor > low && vad.floor < 0.006, `up slowly: ${vad.floor}`);
  assert.ok(before > 0.015);
});

ok('a take that never falls silent ends at the length cap', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000, maxMs: 10000 });
  run(vad, 0, 500, QUIET);
  // Speech has gaps between words: 250 ms of voice, 100 ms between.
  const evs = run(vad, 500, 20000, (t) => ((t % 350) < 250 ? SPEECH : QUIET));
  assert.deepEqual(evs.map((e) => [e.type, e.reason || '']), [['speech', ''], ['end', 'max']]);
  assert.equal(evs[1].t, 10000);
});

ok('a room that gets louder for good is not speech, and no take ends in it', () => {
  // Quiet 2 s, then a fan: a constant level well above the old threshold.
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000 });
  const FAN = 0.05;
  const evs = run(vad, 0, 30000, (t) => (t < 2000 ? QUIET : FAN));
  // It looks like speech at first, is retracted once it has not dipped for
  // stepMs, and the open mic closes on the no-speech timeout: nothing ends
  // as a take, so nothing could be sent.
  assert.deepEqual(evs.map((e) => e.type), ['speech', 'noise', 'timeout']);
  assert.equal(evs[1].t, 2000 + VAD_DEFAULTS.stepMs);
  assert.equal(evs[2].t, 8000);
  assert.equal(vad.heard, false);
  assert.ok(Math.abs(vad.floor - FAN) < 0.005, `floor ${vad.floor}`);
});

ok('a noise step after the no-speech window closes the mic at once', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 4000 });
  const evs = run(vad, 0, 30000, (t) => (t < 3000 ? QUIET : 0.05));
  assert.deepEqual(evs.map((e) => [e.type, e.t]), [['speech', 3150], ['timeout', 6000]]);
});

ok('a room that gets louder mid-sentence: the floor follows, the take still ends on silence', () => {
  const vad = new Vad({ silenceMs: 1500, noSpeechMs: 8000 });
  run(vad, 0, 500, QUIET);
  // Speech with gaps for 2 s, then a fan starts under it and stays.
  const FAN = 0.05;
  const level = (t) => {
    if (t < 2500) return (t % 350) < 250 ? SPEECH : QUIET;
    return FAN;
  };
  const evs = run(vad, 500, 20000, level);
  const types = evs.map((e) => [e.type, e.reason || '']);
  assert.deepEqual(types, [['speech', ''], ['end', 'silence']]);
  // Not at the cap: once the fan is the floor, the speaker is silent.
  assert.ok(evs[1].t <= 2500 + VAD_DEFAULTS.stepMs + 1500 + FRAME, `end at ${evs[1].t}`);
});

ok('reset() starts a fresh take', () => {
  const vad = new Vad({ noSpeechMs: 1000 });
  run(vad, 0, 2000, QUIET);
  assert.equal(vad.state, 'done');
  vad.reset();
  assert.equal(vad.state, 'waiting');
  assert.equal(vad.floor, null);
  const evs = run(vad, 5000, 7000, QUIET);
  assert.deepEqual(evs.map((e) => [e.type, e.t]), [['timeout', 6000]]);
});

ok('garbage levels count as silence', () => {
  const vad = new Vad({ noSpeechMs: 1000 });
  const evs = run(vad, 0, 2000, (t) => [NaN, undefined, -1, 'x'][(t / FRAME) % 4]);
  assert.deepEqual(evs.map((e) => e.type), ['timeout']);
});

ok('rms of a block', () => {
  assert.equal(rms([]), 0);
  assert.equal(rms(null), 0);
  assert.equal(rms(new Float32Array([0.5, -0.5, 0.5, -0.5])), 0.5);
  assert.ok(Math.abs(rms([1, 0, 0, 0]) - 0.5) < 1e-9);
});

// ── Which thread a wake opens ────────────────────────────────────────────────
const NOW = 1_800_000_000_000;
const MIN = 60 * 1000;

ok('a wake opens the thread used in the mode most recently within 15 minutes', () => {
  assert.equal(WAKE_WINDOW_MS, 15 * MIN);
  const entries = {
    a: { on: true, at: NOW - 10 * MIN },
    b: { on: true, at: NOW - 2 * MIN },
    c: { on: true, at: NOW - 40 * MIN },
  };
  assert.equal(pickWakeThread(entries, NOW), 'b');
});

ok('nothing within the window: a new thread (null)', () => {
  assert.equal(pickWakeThread({ a: { on: true, at: NOW - 16 * MIN } }, NOW), null);
  assert.equal(pickWakeThread({}, NOW), null);
  assert.equal(pickWakeThread(null, NOW), null);
});

ok('the window is inclusive at fifteen minutes', () => {
  assert.equal(pickWakeThread({ a: { on: true, at: NOW - 15 * MIN } }, NOW), 'a');
  assert.equal(pickWakeThread({ a: { on: true, at: NOW - 15 * MIN - 1 } }, NOW), null);
});

ok('a thread switched off right after use is still the one the user was talking to', () => {
  const entries = { a: { on: false, at: NOW - MIN }, b: { on: true, at: NOW - 5 * MIN } };
  assert.equal(pickWakeThread(entries, NOW), 'a');
});

ok('unusable entries are skipped; a future stamp counts as now', () => {
  const entries = {
    bad1: { on: true, at: 'x' },
    bad2: { on: true },
    bad3: null,
    future: { on: true, at: NOW + 5 * MIN },
    a: { on: true, at: NOW - MIN },
  };
  assert.equal(pickWakeThread(entries, NOW), 'future');
  assert.equal(pickWakeThread({ bad1: entries.bad1, bad3: null }, NOW), null);
});

ok('a custom window', () => {
  assert.equal(pickWakeThread({ a: { on: true, at: NOW - 2 * MIN } }, NOW, MIN), null);
});

// ── The stored per-thread mode ───────────────────────────────────────────────
ok('mode flags round-trip through storage and feed the wake choice', () => {
  store.clear();
  assert.deepEqual(modeEntries(), {});
  assert.equal(isContinuous('t1'), false);
  setContinuous('t1', true, NOW - 20 * MIN);
  setContinuous('t2', true, NOW - 3 * MIN);
  assert.equal(isContinuous('t1'), true);
  assert.equal(pickWakeThread(modeEntries(), NOW), 't2');
  touchContinuous('t1', NOW - MIN);
  assert.equal(pickWakeThread(modeEntries(), NOW), 't1');
  setContinuous('t1', false, NOW);
  assert.equal(isContinuous('t1'), false);
  assert.equal(modeEntries().t1.at, NOW - MIN, 'switching off keeps the last use');
  // A thread not in the mode is not stamped by a touch.
  touchContinuous('t1', NOW);
  assert.equal(modeEntries().t1.at, NOW - MIN);
  touchContinuous('nope', NOW);
  assert.equal(modeEntries().nope, undefined);
  setContinuous('', true);
  assert.equal(Object.keys(modeEntries()).length, 2);
});

ok('the stored map keeps the 50 most recently used threads', () => {
  store.clear();
  for (let i = 0; i < 60; i++) setContinuous(`t${i}`, true, NOW - (60 - i) * 1000);
  const ids = Object.keys(modeEntries());
  assert.equal(ids.length, 50);
  assert.ok(!ids.includes('t0') && ids.includes('t59'));
});

ok('a corrupt stored map reads as empty', () => {
  store.set(MODE_KEY, '{nope');
  assert.deepEqual(modeEntries(), {});
  store.set(MODE_KEY, '[1,2]');
  assert.deepEqual(modeEntries(), {});
});

// ── Voice settings ───────────────────────────────────────────────────────────
ok('voice timeouts default, store, and clamp', () => {
  store.clear();
  assert.deepEqual(voiceTimeouts(), { silenceMs: DEFAULT_SILENCE_MS, noSpeechMs: DEFAULT_NO_SPEECH_MS });
  assert.equal(DEFAULT_SILENCE_MS, 1500);
  assert.equal(DEFAULT_NO_SPEECH_MS, 8000);
  assert.deepEqual(setVoiceTimeouts({ silenceMs: 2000 }), { silenceMs: 2000, noSpeechMs: 8000 });
  assert.deepEqual(setVoiceTimeouts({ noSpeechMs: 12000 }), { silenceMs: 2000, noSpeechMs: 12000 });
  assert.deepEqual(setVoiceTimeouts({ silenceMs: 10, noSpeechMs: 999999 }), { silenceMs: 500, noSpeechMs: 30000 });
  // Nonsense falls back to the defaults rather than to zero.
  assert.deepEqual(setVoiceTimeouts({ silenceMs: NaN, noSpeechMs: -3 }),
    { silenceMs: DEFAULT_SILENCE_MS, noSpeechMs: DEFAULT_NO_SPEECH_MS });
  store.set('retinue-voice-silence-ms', 'abc');
  assert.equal(voiceTimeouts().silenceMs, DEFAULT_SILENCE_MS);
});

ok('a blocked storage gives the defaults and never throws', () => {
  const saved = globalThis.localStorage;
  globalThis.localStorage = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); },
  };
  try {
    assert.deepEqual(voiceTimeouts(), { silenceMs: DEFAULT_SILENCE_MS, noSpeechMs: DEFAULT_NO_SPEECH_MS });
    assert.deepEqual(modeEntries(), {});
    setContinuous('x', true);
    assert.equal(isContinuous('x'), false);
    setVoiceTimeouts({ silenceMs: 2000 });
  } finally {
    globalThis.localStorage = saved;
  }
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the continuous.js behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # Node treats a bare .js as CommonJS; the module is ESM, so import a copy.
        shutil.copy(CONTINUOUS_JS, os.path.join(tmp, "continuous.mjs"))
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: continuous.js behaviour test")
            return 1
    print("PASS: continuous.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
