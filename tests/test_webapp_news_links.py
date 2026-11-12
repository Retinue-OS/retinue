#!/usr/bin/env python3
"""Where a news headline leads (webapp/components/news.js), under Node.

A news item is a reference, and its link is text that came from outside: a
feed's <link>, a channel post, a newsletter's header. The headline on the
news card and page leads wherever that link says. This pins the page's side
of the rule the gateway files and serves items by (news_store.web_link): a
headline is a link only when its item carries an absolute http(s) URL with a
host, opened in a new tab; with anything else (no link at all, a relative
reference, another scheme, a control character in it) the headline is plain
text and the row holds no anchor. An item without a link used to render as
href="", a link to the dashboard page itself.

The cases are checked on both sides: the page's webLink() under Node and the
store's web_link() here, so the two cannot drift apart unnoticed.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
page's half reports a skip and the test passes on the store's half.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")
SCRIPTS = os.path.join(HERE, "..", "scripts")

# A link an item may carry, and the link it is taken for ("" for none).
CASES = [
    # Web links, kept as they are (bar surrounding whitespace).
    ("https://example.org/fest", "https://example.org/fest"),
    ("http://example.org", "http://example.org"),
    ("HTTPS://Example.org/A", "HTTPS://Example.org/A"),
    ("  https://example.org/x \n", "https://example.org/x"),
    ("https://example.org:8443/x?y=1&z=2#top", "https://example.org:8443/x?y=1&z=2#top"),
    ("https://user@example.org/", "https://user@example.org/"),
    ("https://[2001:db8::1]/x", "https://[2001:db8::1]/x"),
    ("https://example.org/a b", "https://example.org/a b"),
    ("https://zürich.example/straße", "https://zürich.example/straße"),
    ("https://t.me/quartier/1234", "https://t.me/quartier/1234"),
    # Nothing there.
    (None, ""), ("", ""), ("   ", ""), (42, ""), ({}, ""), ([], ""), (True, ""),
    # A reference relative to the page it is shown on would lead into the
    # dashboard.
    ("/sends/default/752", ""), ("news.html", ""), ("./x", ""), ("../x", ""),
    ("?scope=all", ""), ("#new", ""), ("//example.org/x", ""), ("/\\example.org/x", ""),
    ("example.org/fest", ""), ("www.example.org", ""),
    # A scheme without "//" and a host is one too, on a page of that scheme.
    ("https:example.org", ""), ("https:/example.org", ""), ("http:x", ""),
    ("https:\\\\example.org", ""), ("https://example.org\\@other.example/", ""),
    # No host.
    ("https://", ""), ("https:///x", ""), ("https://?x", ""), ("https://#x", ""),
    ("https://:80/", ""), ("https://@/x", ""), ("https://example.org:port/", ""),
    ("https://exa mple.org/", ""),
    # Any other kind of address.
    ("javascript:alert(1)", ""), ("JaVaScRiPt:alert(1)", ""), (" javascript:alert(1)", ""),
    ("data:text/html,<p>x</p>", ""), ("vbscript:x", ""), ("file:///etc/hosts", ""),
    ("blob:https://example.org/1", ""), ("about:blank", ""), ("ftp://example.org/x", ""),
    ("intent://example.org/#Intent;scheme=https;end", ""), ("content://x/y", ""),
    ("mailto:a@example.org", ""), ("tel:+41441234567", ""), ("sms:+41441234567", ""),
    ("view-source:https://example.org/", ""), ("retinue-shell://open?url=x", ""),
    # A control character anywhere inside.
    ("https://example.org/\nx", ""), ("https://exa\tmple.org/", ""),
    ("ht\ntps://example.org/", ""), ("java\tscript:alert(1)", ""),
    ("\u0000https://example.org/", ""), ("https://example.org/\u007f", ""),
    ("https://example.org/\r\nx", ""),
]

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';

// base.js defines a custom-element base class on import, and news.js
// registers its element.
globalThis.HTMLElement = class {};
const registry = new Map();
globalThis.customElements = { define: (name, cls) => registry.set(name, cls) };
globalThis.document = { addEventListener() {}, getElementById: () => null };

const { webLink, itemLi } = await import('./news.js');
assert.ok(registry.get('retinue-news'), 'the element registered itself');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };
const cases = JSON.parse(fs.readFileSync('cases.json', 'utf8'));
const TITLE = 'Sommerfest <30. Juli>';
const row = (url, extra = {}) => itemLi(
  { id: 'a1b2', title: TITLE, source: 'Quartierverein', url, ...extra },
  { top: false, summary: true, speech: false, reading: false });
const unesc = (h) => h.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const hrefs = (html) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => unesc(m[1]));
const TEXT_HEADLINE = '<span class="headline">Sommerfest &lt;30. Juli&gt;</span>';

ok('a link is a web link or none', () => {
  for (const [given, link] of cases) {
    assert.equal(webLink(given), link, JSON.stringify(given));
  }
  assert.equal(webLink(undefined), '');
  assert.equal(webLink(), '');
});

ok('an item with a web link: the headline leads there, in a new tab', () => {
  for (const [given, link] of cases.filter(([, l]) => l)) {
    const html = row(given);
    assert.deepEqual(hrefs(html), [link], JSON.stringify(given));
    assert.ok(html.includes('<a class="headline" href="'), html);
    assert.ok(html.includes(' target="_blank" rel="noopener noreferrer" data-act="read"'
      + ' data-id="a1b2">Sommerfest &lt;30. Juli&gt;</a>'), html);
    assert.ok(!html.includes(TEXT_HEADLINE), html);
  }
});

ok('an item without one: the headline is text, and the row holds no link', () => {
  for (const [given] of cases.filter(([, l]) => !l)) {
    const html = row(given);
    assert.ok(html.includes(TEXT_HEADLINE), `${JSON.stringify(given)} -> ${html}`);
    assert.ok(!/<a[\s>]/.test(html) && !html.includes('href'),
      `${JSON.stringify(given)} -> ${html}`);
    // Nothing on a text headline records the item as opened.
    assert.ok(!html.includes('data-act="read"'), html);
  }
  const html = itemLi({ id: 'a1b2', title: TITLE }, { summary: false });
  assert.ok(html.includes(TEXT_HEADLINE) && !html.includes('href'), html);
});

ok('the rest of the row is the same either way', () => {
  for (const url of ['https://example.org/fest', '']) {
    const html = row(url, { summary: 'Open-air, 18:00.', reason: 'Your neighbourhood' });
    assert.ok(html.startsWith('<li class="" data-id="a1b2">'), html);
    assert.ok(html.includes('<span class="meta">Quartierverein</span>'), html);
    assert.ok(html.includes('<span class="why">Your neighbourhood</span>'), html);
    assert.ok(html.includes('<span class="summary">Open-air, 18:00.</span>'), html);
    for (const act of ['up', 'down', 'hide']) {
      assert.ok(html.includes(`<button data-act="${act}" data-id="a1b2"`), html);
    }
  }
});

ok('what an item says is text, wherever it is put', () => {
  const html = itemLi({
    id: 'a"b', title: '<img src=x onerror=1>', source: '<b>s</b>', summary: '<i>x</i>',
    reason: '"quoted"', url: 'https://example.org/?q="><script>x</script>',
  }, { summary: true });
  assert.ok(!/<(img|b|i|script)[\s>]/.test(html), html);
  assert.deepEqual(hrefs(html), ['https://example.org/?q="><script>x</script>']);
});

console.log(`${passed} checks passed`);
"""


def _load_store():
    spec = importlib.util.spec_from_file_location(
        "news_store_for_links", os.path.join(SCRIPTS, "news_store.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    # The store's half: the same cases, by the rule items are filed and
    # served by.
    store = _load_store()
    wrong = [(given, store.web_link(given)) for given, link in CASES
             if store.web_link(given) != link]
    if wrong:
        print("FAIL: news_store.web_link disagrees on " + repr(wrong))
        return 1
    print(f"ok - news_store.web_link agrees on all {len(CASES)} cases")

    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the news link test needs it for the page")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "device-store.js", "markdown.js", "speech.js", "news.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "cases.json"), "w", encoding="utf-8") as fh:
            json.dump(CASES, fh)
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: news link test")
            return 1
    print("PASS: news link test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
