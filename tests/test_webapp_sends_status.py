#!/usr/bin/env python3
"""How the status page of a decided send ends (webapp/gateway/sends.js).

The page of a send the user allowed polls `GET /sends/<account>/<id>/status`
while the send is on its way, and then says how it ended. The status
document is `{status, error?, outcome_unknown?, note?, next?}`
(scripts/web-gateway.py, _handle_channel_send_status). These checks run the
script under Node with a stub page and a scripted status endpoint, and pin:

- an `error` whose `outcome_unknown` is true is **not** drawn as a failure:
  the mail server was handed the mail and its answer was lost, so the page
  says "Not confirmed — it may have gone out" with a question mark — never
  the cross, never the "could not deliver" line, which would have the reader
  (or an agent) send the mail a second time. Only the plain boolean counts;
- the gateway's own sentence for the outcome (`note`) is shown in place of
  the page's, as text: the sentence for an unknown outcome, and "sent, but
  not to everyone" for a mail the server refused some recipients of;
- every other ending is as it was: the check and a move on for a send that
  went, the cross with the gateway's error for one that failed, the cross
  for one rejected or retracted; a send still on its way is asked again.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.

    python3 tests/test_webapp_sends_status.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENDS_JS = REPO_ROOT / "webapp" / "gateway" / "sends.js"

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const SRC = fs.readFileSync(process.argv[2], 'utf8');
const NOTES = {
  'data-note-done': 'Sent.',
  'data-note-rejected': 'The message was discarded without sending.',
  'data-note-retracted': 'The agent retracted this message before approval; nothing was sent.',
  'data-note-error': 'The gateway could not deliver the message: ',
};
const UNKNOWN = 'Not confirmed — it may have gone out. Check with the recipient or the Sent folder before it is sent again.';

class Mark {
  constructor() { this.attrs = {}; this.style = {}; this.className = ''; this.textContent = ''; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
}

// One load of a status page that is "sending": the script polls `answers`
// in turn (a document, or 'offline'), and the timers it sets are run by the
// harness. → what the page shows once the answers are used up.
async function run(answers, attrs = {}) {
  const rowAttrs = { 'data-status': 'sending', 'data-poll': '/sends/mail-gateway/r1/status', ...NOTES, ...attrs };
  const row = {
    hasAttribute: (n) => n in rowAttrs,
    getAttribute: (n) => (n in rowAttrs ? rowAttrs[n] : null),
  };
  // The icon as the page holds it: the markup the script wrote, or the
  // element it appended.
  const icon = {
    _html: '<div class="spin" role="status" aria-label="sending"></div>', kids: [],
    set innerHTML(h) { this._html = h; this.kids = []; },
    get innerHTML() { return this._html; },
    set textContent(t) { this._html = String(t); this.kids = []; },
    get textContent() { return this._html; },
    appendChild(c) { this.kids.push(c); return c; },
  };
  const note = { textContent: 'Delivering in the background…' };
  const next = { href: '/sends', style: { display: 'none' } };
  const timers = [];
  const asked = [];
  const out = { icon, note, next, asked, went: null, left: 0 };
  const location = { origin: 'https://dash.example', href: 'https://dash.example/sends/mail-gateway/r1', replace() {} };
  const context = {
    window: { close() { out.left += 1; } }, URL, JSON, Date,
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: {
      body: { hasAttribute: () => false },
      getElementById: (id) => ({ 'st-row': row, 'st-icon': icon, 'st-note': note, 'st-next': next }[id] || null),
      createElement: () => new Mark(),
    },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    fetch: (url) => {
      asked.push(url);
      const a = answers.shift();
      if (a === undefined) return new Promise(() => {});
      if (a === 'offline') return Promise.reject(new TypeError('offline'));
      return Promise.resolve({ json: async () => a });
    },
  };
  // `location = url` in the script is a navigation.
  Object.defineProperty(context, 'location', { get: () => location, set: (u) => { out.went = String(u); } });
  vm.runInNewContext(SRC, context);
  const tick = () => new Promise((r) => setImmediate(r));
  // The first poll, then each one a pending answer asked for.
  for (let i = 0; i < 20 && timers.length && (answers.length || asked.length === 0); i += 1) {
    timers.shift()();
    await tick();
    await tick();
  }
  out.timers = timers;
  // What stands where the spinner stood: 'check', 'cross', 'unsure' or 'spin'.
  out.mark = icon.kids.length ? icon.kids[0].className
    : (/class="([a-z]+)"/.exec(icon.innerHTML) || [null, ''])[1];
  return out;
}

let passed = 0;
async function check(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

await check('an unknown outcome reads "not confirmed — it may have gone out", never as failed', async () => {
  const lost = 'the mail server was handed this mail and its answer was lost; it is not known whether the mail went out';
  const p = await run([{ status: 'sending' }, { status: 'error', error: lost, outcome_unknown: true, next: null }]);
  assert.equal(p.asked.length, 2, 'asked again while it was on its way');
  assert.equal(p.mark, 'unsure');
  const mark = p.icon.kids[0];
  assert.equal(mark.textContent, '?');
  assert.deepEqual(mark.attrs, { role: 'img', 'aria-label': 'not confirmed' }, 'said in words too, not by a shape alone');
  assert.ok(!p.icon.innerHTML.includes('cross') && !p.icon.innerHTML.includes('✕'), 'no cross');
  assert.equal(p.note.textContent, `${UNKNOWN} ${lost}`);
  assert.ok(p.note.textContent.startsWith('Not confirmed — it may have gone out.'));
  assert.ok(!/could not deliver|failed|not sent/i.test(p.note.textContent), p.note.textContent);
  // It stays put to be read: nothing moves on by itself.
  assert.equal(p.went, null);
  assert.equal(p.left, 0);
  assert.equal(p.timers.length, 0, 'no move on, no further poll');
});

await check('an unknown outcome names the next pending send, and stays', async () => {
  const p = await run([{ status: 'error', error: 'No answer.', outcome_unknown: true, next: '/sends/mail-gateway/r2' }]);
  assert.equal(p.mark, 'unsure');
  assert.equal(p.next.href, '/sends/mail-gateway/r2');
  assert.equal(p.next.style.display, '');
  assert.equal(p.went, null);
});

await check('the page\'s own words where the gateway gives none, the gateway\'s where it does', async () => {
  // No error sentence at all.
  let p = await run([{ status: 'error', outcome_unknown: true }]);
  assert.equal(p.note.textContent, UNKNOWN);
  p = await run([{ status: 'error', error: 7, outcome_unknown: true }]);
  assert.equal(p.note.textContent, UNKNOWN);
  // The page's markup may carry the line (`data-note-unknown`).
  p = await run([{ status: 'error', error: 'No answer.', outcome_unknown: true }],
                { 'data-note-unknown': 'Not confirmed — it may have gone out:' });
  assert.equal(p.note.textContent, 'Not confirmed — it may have gone out: No answer.');
  // The gateway's own sentence for the outcome (`note`) is the one shown —
  // as text, on one line.
  p = await run([{ status: 'error', error: 'No answer.', outcome_unknown: true,
                   note: 'Not confirmed — the mail to ann@example.com\n may have gone out. <b>Do not send it again.</b>' }]);
  assert.equal(p.mark, 'unsure');
  assert.equal(p.note.textContent, 'Not confirmed — the mail to ann@example.com may have gone out. <b>Do not send it again.</b>');
  p = await run([{ status: 'error', error: 'No answer.', outcome_unknown: true, note: { html: '<img>' } }]);
  assert.equal(p.note.textContent, `${UNKNOWN} No answer.`);
});

await check('only the plain boolean is "unknown": a failure stays a failure', async () => {
  for (const flag of [undefined, false, 'true', 1, null, 'no']) {
    const doc = { status: 'error', error: 'SMTP send failed: (535) auth' };
    if (flag !== undefined) doc.outcome_unknown = flag;
    const p = await run([doc]);
    assert.equal(p.mark, 'cross', JSON.stringify(flag));
    assert.equal(p.note.textContent, 'The gateway could not deliver the message: SMTP send failed: (535) auth');
    assert.equal(p.icon.kids.length, 0);
  }
  const p = await run([{ status: 'error' }]);
  assert.equal(p.note.textContent, 'The gateway could not deliver the message: unknown error');
  // And `outcome_unknown` on a send that went is nothing to say.
  const sent = await run([{ status: 'approved', outcome_unknown: true }]);
  assert.equal(sent.mark, 'check');
  assert.equal(sent.note.textContent, 'Sent.');
});

await check('the other endings are as they were', async () => {
  let p = await run([{ status: 'pending' }, 'offline', { status: 'approved', next: '/sends/mail-gateway/r2' }]);
  assert.equal(p.asked.length, 3, 'a blip is asked over');
  assert.equal(p.mark, 'check');
  assert.equal(p.note.textContent, 'Sent.');
  assert.equal(p.timers.length, 1, 'it moves on a moment later');
  p.timers.shift()();
  assert.equal(p.went, '/sends/mail-gateway/r2');
  p = await run([{ status: 'approved', next: null }]);
  p.timers.shift()();
  assert.equal(p.went, null);
  assert.equal(p.left, 1, 'nothing left to decide: the way out');
  p = await run([{ status: 'rejected' }]);
  assert.deepEqual([p.mark, p.note.textContent], ['cross', 'The message was discarded without sending.']);
  p = await run([{ status: 'retracted' }]);
  assert.deepEqual([p.mark, p.note.textContent], ['cross', 'The agent retracted this message before approval; nothing was sent.']);
});

await check('a mail sent, but not to everyone, says so in the gateway\'s sentence', async () => {
  const p = await run([{ status: 'approved', refused: ['bob@builder.example'],
                         note: 'Sent — but the mail server refused bob@builder.example.', next: null }]);
  assert.equal(p.mark, 'check');
  assert.equal(p.note.textContent, 'Sent — but the mail server refused bob@builder.example.');
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the send status test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.mjs"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(harness), str(SENDS_JS)],
                              capture_output=True, text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: send status test")
            return 1
    print("PASS: send status test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
