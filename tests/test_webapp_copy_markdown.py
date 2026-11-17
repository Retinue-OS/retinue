#!/usr/bin/env python3
"""What an answer's "Copy" puts on the clipboard (webapp/components/markdown.js).

Every answer in a dashboard thread carries a small Copy link in its header
that copies the whole reply as Markdown. This pins `copyableMarkdown`: the
Markdown stays as written (headings, lists, code, hard-break spaces), the
app's [[chip: …]] buttons go, a [[call: …]] becomes the name and number it
dials, a directive quoted as code stays, and the blank lines a removed row of
chips leaves behind collapse.

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

const { copyableMarkdown } = await import('./markdown.js');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };

ok('plain Markdown is copied as written', () => {
  const md = '## Plan\n\n- one\n- **two**\n\n```js\nconst a = 1;\n```\n\nline  \nbreak';
  assert.equal(copyableMarkdown(md), md);
});

ok('chips are dropped and their blank lines collapse', () => {
  assert.equal(copyableMarkdown('Shall I send it?\n\n[[chip: Send]] [[chip: Edit | Change: ]]\n\n'),
    'Shall I send it?');
  assert.equal(copyableMarkdown('A\n\n[[chip: X]]\n\nB'), 'A\n\nB');
});

ok('a call directive becomes name and number', () => {
  assert.equal(copyableMarkdown('Call [[call: +41 79 123 45 67 | Mara]] now'),
    'Call Mara (+41 79 123 45 67) now');
  assert.equal(copyableMarkdown('[[call: +41 79 123 45 67]]'), '+41 79 123 45 67');
  assert.equal(copyableMarkdown('[[call: +41 79 123 45 67 | no-voice-cancel]]'), '+41 79 123 45 67');
  assert.equal(copyableMarkdown('[[call: +41 79 123 45 67 | Mara | no-voice-cancel]]'),
    'Mara (+41 79 123 45 67)');
});

ok('a directive quoted as code stays', () => {
  assert.equal(copyableMarkdown('Write `[[chip: Yes]]` to offer one.'),
    'Write `[[chip: Yes]]` to offer one.');
});

ok('nothing gives an empty string', () => {
  assert.equal(copyableMarkdown(null), '');
  assert.equal(copyableMarkdown('[[chip: Only]]'), '');
});

console.log(`${passed} passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the copy-Markdown test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "device-store.js", "markdown.js"):
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
            print("FAIL: copy-Markdown test")
            return 1
    print("PASS: copy-Markdown test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
