#!/usr/bin/env python3
"""What a chat attachment becomes on the chat page (webapp/components/chat-page.js).

An attachment's url goes into an href (a file row), a src (an image, a voice
note, a video) and the lightbox. The references behind it come from outside:
the messenger gateways' ledger records and the notify rail carry what their
callers wrote, and the web gateway passes on only the media proxy's URL or an
absolute http(s) URL (_shape_chat_attachments). The page holds to the same
rule on its own, under Node with a scripted DOM:

- a url of the media proxy (/chats/media/<slug>/<media-id>) renders the
  element its type calls for, as before;
- an absolute http(s) URL is a file row, a link to open, whatever its type:
  an image, a voice note or a video is never loaded from another origin into
  the page (the origin's Content-Security-Policy allows media from this
  origin only, scripts/content_policy.py);
- anything else (another scheme, a reference into the dashboard, a proxy path
  with a path step in it, a control character, no url at all) renders the
  attachment's name as text: no href, no src, no lightbox, save the two
  exceptions below, neither of which is ever a link;
- a data:image, data:audio or data:video URL (the reference documents' media)
  renders through the element of its own kind, and an image's through the
  lightbox, when the attachment's type is of that kind; no other data: URL
  renders at all;
- the bubble of a send on its way still previews the user's own staged image
  from the object URL the page created, and a blob: URL that came with a
  message does not.

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

// ── A scripted DOM: just enough for the element module to load ───────────────
const registry = new Map();
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; this.isConnected = true; this.style = {}; this.dataset = {}; }
  attachShadow() { this.shadowRoot = { innerHTML: '', querySelector() { return null; }, querySelectorAll() { return []; } }; return this.shadowRoot; }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} }, title: 'Retinue — Chat',
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/chat.html', search: '', href: 'http://dash/chat.html' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };
globalThis.fetch = () => new Promise(() => {});

await import('./chat-page.js');
const Page = registry.get('retinue-chat-page');
assert.ok(Page, 'the element registered itself');
const el = new Page();

let passed = 0;
function ok(name, fn) { fn(); passed++; console.log('  ok  ' + name); }
const unesc = (h) => h.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const urlsIn = (html) => [...html.matchAll(/(?:href|src|data-lightbox)="([^"]*)"/g)].map((m) => unesc(m[1]));
const TYPES = ['image/jpeg', 'audio/ogg', 'video/mp4', 'application/pdf', ''];
const MID = 'ab'.repeat(16);

ok('the media proxy renders the element its type calls for', () => {
  for (const url of [`/chats/media/signal-gateway/${MID}`, `/chats/media/127.0.0.1/${MID}`]) {
    const img = el._attachmentHtml({ url, type: 'image/jpeg', name: 'p.jpg' });
    assert.ok(img.includes('<img class="att-img'), img);
    assert.deepEqual(urlsIn(img), [url, url], img);
    const audio = el._attachmentHtml({ url, type: 'audio/ogg' });
    assert.ok(audio.startsWith('<audio class="att-audio"'), audio);
    assert.deepEqual(urlsIn(audio), [url]);
    const video = el._attachmentHtml({ url, type: 'video/mp4' });
    assert.ok(video.startsWith('<video class="att-video'), video);
    assert.deepEqual(urlsIn(video), [url]);
    const file = el._attachmentHtml({ url, type: 'application/pdf', name: 'a.pdf', size: 2048 });
    assert.ok(file.startsWith('<a class="att-file" href='), file);
    assert.deepEqual(urlsIn(file), [url]);
  }
});

ok('a web link is a file row whatever its type, never a medium loaded into the page', () => {
  for (const url of ['https://example.org/pic.jpg', 'http://example.org/a?b=1&c=2']) {
    for (const [type, label] of [['image/jpeg', 'Image'], ['audio/ogg', 'Voice message'],
                                 ['video/mp4', 'Video'], ['application/pdf', 'Attachment'], ['', 'Attachment']]) {
      const html = el._attachmentHtml({ url, type, size: 2048 });
      assert.ok(html.startsWith('<a class="att-file" href='), `${type}: ${html}`);
      assert.deepEqual(urlsIn(html), [url], html);
      assert.ok(!/<(img|audio|video|button)[\s>]/.test(html), html);
      assert.ok(!html.includes('data-lightbox'), html);
      assert.ok(html.includes(`<span class="att-name">${label}</span>`), html);
      assert.ok(html.includes('<span class="att-size">2 KB</span>'), html);
      const named = el._attachmentHtml({ url, type, name: 'Bild <1>.jpg' });
      assert.ok(named.includes('<span class="att-name">Bild &lt;1&gt;.jpg</span>'), named);
    }
  }
});

const NOT_URLS = [
  undefined, null, '', 42, { toString: () => 'https://example.org/' },
  'javascript:alert(1)', ' JavaScript:alert(1)', 'java\tscript:alert(1)',
  'data:text/html,<p>x</p>', 'data:application/pdf;base64,AAAA', 'vbscript:x',
  'file:///etc/hosts', 'blob:http://dash/0b6c', 'mailto:a@example.org',
  'urn:retinue:media:signal:' + MID, '/sends/default/1', 'sends', '../x', '#new',
  '//example.org/x', '/\\example.org/x', 'https:example.org', 'https://',
  'https://example.org/\nx', `/chats/media/../${MID}`, `/chats/media/./${MID}`,
  `/chats/media/signal-gateway/${MID}/../../sends`, `/chats/media/signal-gateway/${MID}?x=1`,
  `/chats/media/a/b/${MID}`, `/chats/media/%2e%2e/${MID}`, `/chats/media/sig/${MID}\n`,
];

ok('anything else is the name as text: no href, no src, no lightbox', () => {
  for (const url of NOT_URLS) {
    for (const type of TYPES) {
      const html = el._attachmentHtml({ url, type, name: 'Bericht <1>.pdf', size: 2048 });
      assert.deepEqual(urlsIn(html), [], `${JSON.stringify(url)} ${type}: ${html}`);
      assert.ok(!/<(a|img|audio|video|button)[\s>]/.test(html), `${JSON.stringify(url)} ${type}: ${html}`);
      assert.ok(html.includes('<span class="att-name">Bericht &lt;1&gt;.pdf</span>'), html);
      assert.ok(html.includes('<span class="att-size">2 KB</span>'), html);
    }
  }
  // Without a name, what kind of file it was.
  assert.ok(el._attachmentHtml({ type: 'image/png' }).includes('>Image</span>'));
  assert.ok(el._attachmentHtml({ type: 'audio/ogg' }).includes('>Voice message</span>'));
  assert.ok(el._attachmentHtml({ type: 'video/mp4' }).includes('>Video</span>'));
  assert.ok(el._attachmentHtml({}).includes('>Attachment</span>'));
});

ok('a data: URL of a medium shows through the element of its own kind, and is never a link', () => {
  const pic = 'data:image/svg+xml,<svg/>';
  const img = el._attachmentHtml({ url: pic, type: 'image/svg+xml', name: 'p.svg' });
  assert.ok(img.includes('<img class="att-img') && !/<a[\s>]/.test(img), img);
  assert.ok(img.includes('data-lightbox="data:image/svg+xml,'), img);
  const audio = el._attachmentHtml({ url: 'data:audio/wav;base64,AAAA', type: 'audio/wav' });
  assert.ok(audio.startsWith('<audio class="att-audio"') && !/<a[\s>]/.test(audio), audio);
  const video = el._attachmentHtml({ url: 'data:video/webm;base64,AAAA', type: 'video/webm' });
  assert.ok(video.startsWith('<video class="att-video') && !/<a[\s>]/.test(video), video);
  // Not for a file row, not for an element of another kind, and no other data: type or spelling.
  for (const [url, type] of [
    [pic, 'application/pdf'], [pic, ''], [pic, undefined], [pic, 'audio/ogg'], [pic, 'video/mp4'],
    ['data:audio/wav;base64,AAAA', 'image/png'], ['data:video/webm;base64,AAAA', 'audio/ogg'],
    ['data:text/html,<p>x</p>', 'image/png'], ['data:text/html,<p>x</p>', 'text/html'],
    ['data:image/svg+xml', 'image/svg+xml'], [' data:image/png,x', 'image/png'],
    ['DATA:image/png,x', 'image/png'], ['data:image,x', 'image/png'], ['data:,x', 'image/png'],
  ]) {
    const html = el._attachmentHtml({ url, type, name: 'x' });
    assert.deepEqual(urlsIn(html), [], `${JSON.stringify(url)} ${type}: ${html}`);
    assert.ok(!/<(a|img|audio|video|button)[\s>]/.test(html), `${JSON.stringify(url)} ${type}: ${html}`);
  }
});

ok("the bubble of a send on its way previews the user's own staged image", () => {
  el._localSeq = 3;
  const im = { url: 'blob:http://dash/0b6c', content_type: 'image/jpeg', width: 40, height: 30, name: 'me.jpg' };
  const own = el._ownPreview(im, 0);
  assert.equal(own.id, 'local-att-3-0');
  const html = el._attachmentHtml(own);
  assert.ok(html.includes('<img class="att-img"'), html);
  assert.deepEqual(urlsIn(html), [im.url, im.url]);
  // The same url on an attachment the page did not build is text.
  const copy = { ...own };
  assert.deepEqual(urlsIn(el._attachmentHtml(copy)), []);
  // And the page's own attachment holds to the rule for any other url.
  const odd = el._ownPreview({ ...im, url: 'javascript:alert(1)' }, 1);
  assert.deepEqual(urlsIn(el._attachmentHtml(odd)), []);
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the chat attachment test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # The components are ESM and import each other by relative path; Node
        # treats a bare .js as CommonJS unless the directory says otherwise.
        for name in os.listdir(COMPONENTS):
            if name.endswith(".js"):
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
            print("FAIL: chat attachment test")
            return 1
    print("PASS: chat attachment test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
