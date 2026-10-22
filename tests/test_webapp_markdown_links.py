#!/usr/bin/env python3
"""Where a rendered link opens (webapp/components/markdown.js), under Node.

The dashboard is installed as a standalone PWA. A target="_blank" link is
handed to a browsing context outside the app window, so a link Ara gives to
another thread used to open the browser instead of the app. This pins the
rule: a link to the dashboard's own origin navigates in place (no target),
every other web link opens in a new tab, mailto:/tel: never carry a target,
and without a `location` (rendering outside a page) every web link counts as
external. Since an in-place link can unload the page, it also pins the guard:
a press that would load another page while this one holds unsent input is held
like a tab of the navigation row (and let through when pressed again), while
a press on another thread of the same page, a fragment change that loses
nothing, never is.

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

// base.js (for esc) defines a custom-element base class on import.
globalThis.HTMLElement = class {};
// markdown.js puts its link guard on the document; keep the listener.
const clicks = [];
globalThis.document = {
  addEventListener: (t, f) => { if (t === 'click') clicks.push(f); },
  getElementById: () => null,
};

const { renderInline } = await import('./markdown.js');
const { registerUnsavedCheck } = await import('./shell.js');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };
const NEW_TAB = ' target="_blank" rel="noopener noreferrer"';

ok('without a page every web link opens in a new tab', () => {
  assert.equal(renderInline('[t](https://ara.example.com/#conversation-1)'),
    `<a href="https://ara.example.com/#conversation-1"${NEW_TAB}>t</a>`);
});

globalThis.location = new URL('https://ara.example.com/#conversation-aaaa');

ok('a link to another thread opens in place', () => {
  assert.equal(renderInline('[the other thread](https://ara.example.com/#conversation-bbbb)'),
    '<a href="https://ara.example.com/#conversation-bbbb" data-in-app>the other thread</a>');
});

ok('any dashboard page opens in place, bare URLs included', () => {
  assert.equal(renderInline('[p](https://ara.example.com/sends/signal/42)'),
    '<a href="https://ara.example.com/sends/signal/42" data-in-app>p</a>');
  assert.equal(renderInline('[p](https://ara.example.com/project.html?id=x&y=1)'),
    '<a href="https://ara.example.com/project.html?id=x&amp;y=1" data-in-app>p</a>');
  assert.equal(renderInline('see https://ara.example.com/gateways.'),
    'see <a href="https://ara.example.com/gateways" data-in-app>https://ara.example.com/gateways</a>.');
});

ok('another origin opens in a new tab', () => {
  for (const u of ['https://github.com/retinue-os/retinue/pull/1',
                   'http://ara.example.com/#conversation-bbbb',
                   'https://ara.example.com:8443/',
                   'https://evil.ara.example.com/']) {
    assert.equal(renderInline(`[x](${u})`), `<a href="${u}"${NEW_TAB}>x</a>`, u);
  }
  assert.equal(renderInline('www.example.org'),
    `<a href="https://www.example.org"${NEW_TAB}>www.example.org</a>`);
});

ok('mailto and tel never carry a target', () => {
  assert.equal(renderInline('[m](mailto:a@example.com)'), '<a href="mailto:a@example.com">m</a>');
  assert.equal(renderInline('[t](tel:+41441234567)'), '<a href="tel:+41441234567">t</a>');
});

// A plain press on a link, as the document listener sees it: the anchor
// inside a shadow root, reached through composedPath(). True when held.
const press = (href, { inApp = true, ...extra } = {}) => {
  let held = false;
  const a = { href, hasAttribute: (n) => inApp && n === 'data-in-app' };
  const ev = { button: 0, defaultPrevented: false, ...extra,
    composedPath: () => [{ hasAttribute: () => false }, a],
    preventDefault() { held = true; } };
  for (const f of clicks) f(ev);
  return held;
};

ok('with nothing unsent no in-app link is held', () => {
  assert.equal(clicks.length, 1);
  assert.equal(press('https://ara.example.com/sends/signal/42'), false);
});

let unsent = true;
registerUnsavedCheck(() => unsent);

ok('another thread on this page is never held, even with a draft', () => {
  assert.equal(press('https://ara.example.com/#conversation-bbbb'), false);
});

ok('another page is held while input is unsent, and let through on a second press', () => {
  assert.equal(press('https://ara.example.com/sends/signal/42'), true);
  assert.equal(press('https://ara.example.com/sends/signal/42'), false);
  // The same page without a fragment reloads it: held like any other page.
  assert.equal(press('https://ara.example.com/'), true);
  assert.equal(press('https://ara.example.com/'), false);
});

ok('modified presses and links out are never held', () => {
  assert.equal(press('https://ara.example.com/sends', { ctrlKey: true }), false);
  assert.equal(press('https://ara.example.com/sends', { button: 1 }), false);
  assert.equal(press('https://github.com/', { inApp: false }), false);
  unsent = false;
  assert.equal(press('https://ara.example.com/sends'), false);
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the Markdown link test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "markdown.js", "shell.js"):
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
            print("FAIL: Markdown link test")
            return 1
    print("PASS: Markdown link test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
