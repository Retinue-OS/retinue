#!/usr/bin/env python3
"""Behavioural tests for the dashboard's paste-to-attach helper (webapp/components/clipboard.js).

Both composers that take attachments — the conversation composer and the chat
page — stage whatever a paste carries through this module. The test drives it
under Node with a scripted stand-in for the event's `clipboardData` and pins
what the composers depend on: files come out of `items` first and `files` as
the fallback, a text-only paste costs nothing, a bare copied image is the
caller's to swallow while a copied passage keeps its words, and a screenshot's
meaningless `image.png` becomes a name that says when it was pasted while a
real filename survives.

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
CLIPBOARD_JS = os.path.join(HERE, "..", "webapp", "components", "clipboard.js")

HARNESS = r"""
import assert from 'node:assert/strict';
import { pastedFiles, pastedText, nameFile } from './clipboard.mjs';

// ── A scripted clipboardData ─────────────────────────────────────────────────
// `items` carries {kind, type, file?}; `text` maps a type to its payload.
function clip({ items = [], files = [], text = {} } = {}) {
  const types = Object.keys(text).concat(files.length || items.some((i) => i.kind === 'file') ? ['Files'] : []);
  return {
    types,
    items: items.map((i) => ({
      kind: i.kind, type: i.type,
      getAsFile() { return i.kind === 'file' ? (i.file || null) : null; },
    })),
    files,
    getData(t) { return text[t] || ''; },
  };
}
const png = (name = 'image.png') => new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });
const AT = new Date(2026, 8, 19, 14, 5, 9);  // 2026-09-19 14:05:09 local

// ── Nothing to stage ─────────────────────────────────────────────────────────
assert.deepEqual(pastedFiles(null), []);
assert.deepEqual(pastedFiles(clip({ text: { 'text/plain': 'hello' } })), []);
assert.equal(pastedText(clip({ text: { 'text/plain': 'hello' } })), true);
assert.equal(pastedText(clip({ text: { 'text/plain': '   ' } })), false, 'blank text is no text');
assert.equal(pastedText(null), false);

// ── A screenshot: one file item, html beside it, no plain text ───────────────
{
  const data = clip({
    items: [{ kind: 'string', type: 'text/html' }, { kind: 'file', type: 'image/png', file: png() }],
    files: [png()],
    text: { 'text/html': '<img src="x">' },
  });
  const out = pastedFiles(data, AT);
  assert.equal(out.length, 1, 'items and files describe the same file once');
  assert.equal(out[0].name, 'pasted-20260919-140509.png');
  assert.equal(out[0].type, 'image/png');
  assert.equal(out[0].size, 4, 'the bytes are the original bytes');
  assert.equal(pastedText(data), false, 'a bare image: the caller swallows the paste');
}

// ── A passage with an image in it keeps its words ────────────────────────────
{
  const data = clip({
    items: [{ kind: 'string', type: 'text/plain' }, { kind: 'file', type: 'image/png', file: png() }],
    text: { 'text/plain': 'see the chart', 'text/html': '<p>see the chart<img></p>' },
  });
  assert.equal(pastedFiles(data, AT).length, 1);
  assert.equal(pastedText(data), true, 'the words still belong in the box');
}

// ── Files only (an engine without items): the fallback ───────────────────────
{
  const data = clip({ files: [png('shot.png'), new File(['%PDF'], 'invoice.pdf', { type: 'application/pdf' })] });
  data.items = undefined;
  const out = pastedFiles(data, AT);
  assert.deepEqual(out.map((f) => f.name), ['shot.png', 'invoice.pdf'], 'real names survive');
}

// ── Naming ───────────────────────────────────────────────────────────────────
assert.equal(nameFile(new File(['x'], 'image.png', { type: 'image/png' }), AT).name, 'pasted-20260919-140509.png');
assert.equal(nameFile(new File(['x'], 'Image.JPEG', { type: 'image/jpeg' }), AT).name, 'pasted-20260919-140509.jpg');
assert.equal(nameFile(new File(['x'], '', { type: 'image/webp' }), AT).name, 'pasted-20260919-140509.webp');
assert.equal(nameFile(new File(['x'], 'image.png', { type: '' }), AT).name, 'pasted-20260919-140509.png',
  'no type: the extension the generic name had');
assert.equal(nameFile(new File(['x'], 'blob', { type: 'application/octet-stream' }), AT).name,
  'pasted-20260919-140509.bin');
const kept = new File(['x'], 'holiday.png', { type: 'image/png' });
assert.equal(nameFile(kept, AT), kept, 'a named file is returned as is');

console.log('clipboard.js: all assertions passed');
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the clipboard.js behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # Node treats a bare .js as CommonJS; the module is ESM, so import a copy.
        shutil.copy(CLIPBOARD_JS, os.path.join(tmp, "clipboard.mjs"))
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: clipboard.js behaviour test")
            return 1
    print("PASS: clipboard.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
