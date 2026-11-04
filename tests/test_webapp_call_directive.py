#!/usr/bin/env python3
"""The call directive [[call: <number> | <name>]] (webapp/components/markdown.js), under Node.

Ara puts it at the end of a reply that places a call. In a bubble it renders
as a tap-to-dial link; in the phone shell's voice conversation the thread
finds it with callOf() and places the call (conversation.js, _contCall). This
pins the parts a wrong number would come from: how a written number becomes
a dialable one (separators dropped, the bracketed trunk zero after a country
code dropped, too short or too long refused), that the last directive in a
message is the one placed, that one quoted as code stays code, and that the
label is the name — or the number — behind a glyph, with no words in any
language.

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

const { renderInline, callOf, callNumber, calledBefore } = await import('./markdown.js');

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
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the call directive test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "markdown.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
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
