#!/usr/bin/env python3
"""Behavioural tests for the Chats page's triage view
(webapp/components/chats.js, docs/chat-triage.md), on the scripted DOM and
fetch of tests/test_webapp_chat_list.py. Pins:

  * the full page offers Triage beside the tabs; opening it is a press —
    POST /chats/triage {} — and Sort again posts {again: true};
  * the sections: action (the row opens the chat on Ara's pane), archive,
    delete, and Not sorted — an unread chat with no word yet, one whose
    sorting failed, and one spoken in since it was sorted; an archived chat
    leaves the view; each row is the list's own row (its swipes and tray);
  * the sorting's note and Ara's — on its way, written, or not come;
  * the status line while sorting and after; a refused press says why;
  * the poll runs while work is open, and the list is fetched again once
    the sorting is done;
  * Archive all archives every row of its section with one Undo that puts
    them all back; Delete all takes two taps and deletes every row;
  * no multi-select inside the view; Close (and back) returns to the tabs.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")
FIXTURE = os.path.join(HERE, "..", "webapp", "data", "chats.json")


def _list_harness() -> str:
    """The chat list test's scripted DOM, fetch and helpers — everything up
    to its own checks."""
    spec = importlib.util.spec_from_file_location(
        "test_webapp_chat_list", os.path.join(HERE, "test_webapp_chat_list.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    harness = mod.HARNESS
    cut = harness.index("const LIST = [")
    return harness[:cut]


CHECKS = r"""
// Every mount starts on a fresh history (a view an earlier check left open
// would otherwise be restored), unless a check sets the history up itself.
let keepHistory = false;
const mountList = mount;
mount = async (doc) => {
  if (!keepHistory) history.stack = [null];
  return mountList(doc);
};
const takeTri = (method, what) => take((w) => w.method === method && w.url === '/chats/triage', what);

const LIST = [
  chat('u1', { unread: 1, list_group: 'unread', inbound_seq: 7, last: { ts: '2026-11-21T09:00:00Z', direction: 'in', text: 'invoice' } }),
  chat('u2', { unread: 2, list_group: 'unread', inbound_seq: 3, last: { ts: '2026-11-20T09:00:00Z', direction: 'in', text: 'receipt' } }),
  chat('u3', { unread: 1, list_group: 'unread', inbound_seq: 4, last: { ts: '2026-11-19T09:00:00Z', direction: 'in', text: 'promo' } }),
  chat('u4', { unread: 1, list_group: 'unread', inbound_seq: 2, last: { ts: '2026-11-18T09:00:00Z', direction: 'in', text: 'new' } }),
  chat('r1', { unread: 0, list_group: 'read', inbound_seq: 9 }),
  chat('a1', { unread: 0, list_group: 'archived', archived: true }),
];
const RUNNING = {
  enabled: true, running: true,
  run: { state: 'running', total: 4, sorted: 1, failed: 0 },
  entries: { u1: { bucket: 'action', note: 'Pay the plumber by Friday.', reply: true, seq: 7, run: 'r', followup: { state: 'working' } } },
};
const DONE = {
  enabled: true, running: false,
  run: { state: 'done', total: 4, sorted: 3, failed: 1, finished: '2026-11-21T09:30:00Z' },
  entries: {
    u1: { bucket: 'action', note: 'Pay the plumber by Friday.', reply: true, seq: 7, run: 'r',
          followup: { state: 'done', text: 'The invoice is CHF 320, due Friday. <b>I staged</b> a reply.' } },
    u2: { bucket: 'archive', note: 'Receipt for the train ticket.', seq: 3, run: 'r' },
    u3: { bucket: 'delete', note: 'Advertising.', seq: 4, run: 'r' },
    u4: { bucket: null, error: 'the sorting did not complete', seq: 2, run: 'r' },
    r1: { bucket: 'archive', note: 'Old.', seq: 8, run: 'r' },
    a1: { bucket: 'delete', note: 'x', seq: 5, run: 'r' },
  },
};
const sectionOf = (h, sec) => {
  const start = h.indexOf(`<section class="tri-sec ${sec}"`);
  if (start < 0) return '';
  return h.slice(start, h.indexOf('</section>', start));
};
const ids = (h) => rowIds(h);

async function openTriage(el, answer = RUNNING) {
  el._openTriage();
  const post = takeTri('POST', 'the press');
  assert.deepEqual(post.body, {}, 'opening sorts what is new');
  post.answer(JSON.parse(JSON.stringify(answer)), answer.running ? 202 : 200);
  await settle();
  clearTimeout(el._triTimer);  // the test drives every poll itself
  el._triTimer = null;
  return post;
}

await ok('the full page offers Triage beside the tabs', async () => {
  const el = await mount(payload(LIST));
  assert.ok(/<div class="list-head">.*data-triage="open"/s.test(html(el)), html(el));
});

await ok('opening it presses: the sections, the notes and the status while it sorts', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el);
  const h = html(el);
  assert.ok(!h.includes('role="tablist"'), 'the view replaces the tabs');
  assert.ok(h.includes('Sorting 1 of 4 chats… Ara is still writing her note on 1 chat.'), h);
  const action = sectionOf(h, 'action');
  assert.deepEqual(ids(action), ['u1']);
  assert.ok(action.includes('href="/chat.html?id=u1&pane=ara"'), 'an action chat opens on Ara\'s pane');
  assert.ok(action.includes('Pay the plumber by Friday.'));
  assert.ok(action.includes('Ara is looking at it…'));
  const unsorted = sectionOf(h, 'unsorted');
  assert.deepEqual(ids(unsorted), ['u2', 'u3', 'u4'], 'unread chats with no word yet');
  assert.ok(unsorted.includes('Not sorted yet.'), 'still sorting: not "came in after"');
  assert.ok(!unsorted.includes('Came in after the sorting.'));
  assert.ok(!ids(h).includes('r1'), 'a read chat with no word is no triage row');
  assert.ok(!ids(h).includes('a1'));
  assert.ok(h.includes('data-triage="again" disabled'), 'no second press while it sorts');
  // The list's own row: its swipes and tray, by the tab it is on.
  assert.deepEqual(rowAttrs(h, 'u1'), { quick: 'read', left: 'archive' });
  assert.deepEqual(trayOf(h, 'u1'), ['star', 'read', 'archive', 'mute', 'delete']);
});

await ok('the poll: buckets, Ara\'s note escaped, not sorted with reasons, the list fetched again', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el);
  el._pollTriage();
  takeTri('GET', 'the poll').answer(JSON.parse(JSON.stringify(DONE)));
  await settle();
  takeList().answer(payload(LIST));  // the sorting finished: the list too
  await settle();
  const h = html(el);
  assert.ok(/Sorted \S+ \S+ ago\. 1 could not be sorted\./.test(h), h);
  assert.ok(!h.includes('ago ago'), h);
  assert.ok(!h.includes('data-triage="again" disabled'));
  assert.deepEqual(ids(sectionOf(h, 'action')), ['u1']);
  assert.ok(sectionOf(h, 'action').includes('<b>Ara:</b> The invoice is CHF 320, due Friday. &lt;b&gt;I staged&lt;/b&gt; a reply.'));
  assert.deepEqual(ids(sectionOf(h, 'archive')), ['u2']);
  assert.deepEqual(ids(sectionOf(h, 'delete')), ['u3'], 'an archived chat leaves the view');
  const unsorted = sectionOf(h, 'unsorted');
  assert.deepEqual(ids(unsorted), ['r1', 'u4'], 'newest first, as everywhere');
  assert.ok(unsorted.includes('Not sorted: the sorting did not complete.'));
  assert.ok(unsorted.includes('A new message came in since it was sorted.'));
  assert.equal(el._triTimer, null, 'nothing open: no further poll');
});

await ok('no second press while Ara still writes a note, though the sorting is done', async () => {
  const el = await mount(payload(LIST));
  const writing = JSON.parse(JSON.stringify(DONE));
  writing.entries.u1.followup = { state: 'working' };
  writing.busy = true;
  await openTriage(el, writing);
  assert.ok(html(el).includes('data-triage="again" disabled'), html(el));
  const idle = JSON.parse(JSON.stringify(DONE));
  idle.busy = false;
  el._tri = idle;
  el._rerender();
  assert.ok(!html(el).includes('data-triage="again" disabled'));
});

await ok('Archive all: one request per row, one Undo for all', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  const run = el._triageAll('archive');
  await settle();
  const post = take((w) => w.method === 'POST' && w.url === '/chats/u2/flags', 'the archive');
  assert.deepEqual(post.body, { archived: true });
  post.answer({ id: 'u2', archived: true, muted: false, starred: false });
  await run;
  await settle();
  assert.ok(!ids(html(el)).includes('u2'), 'the archived row leaves the view');
  assert.ok(toast && toast.undo && /Archived 1/.test(toast.text), JSON.stringify(toast));
  toast.undo();
  await settle();
  const back = take((w) => w.method === 'POST' && w.url === '/chats/u2/flags', 'the undo');
  assert.deepEqual(back.body, { archived: false, muted: false, starred: false });
  back.answer({ id: 'u2', archived: false, muted: false, starred: false });
  await settle();
  await settle();
  assert.deepEqual(ids(sectionOf(html(el), 'archive')), ['u2'], 'Undo puts it back');
  dismissUndo();
});

await ok('Delete all takes two taps and deletes every row of the section', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  await el._triageAll('delete');
  assert.ok(!wire.some((w) => w.url.endsWith('/delete')), 'the first tap only arms');
  assert.ok(sectionOf(html(el), 'delete').includes('Delete 1 chat? For good'), html(el));
  const run = el._triageAll('delete');
  await settle();
  take((w) => w.method === 'POST' && w.url === '/chats/u3/delete', 'the delete').answer({ deleted: true });
  await run;
  await settle();
  assert.ok(!ids(html(el)).includes('u3'));
  assert.ok(!html(el).includes('tri-sec delete'), 'an empty section is not shown');
});

await ok('without a follow-up an action row opens on its messages, and the heading names no Ara', async () => {
  const el = await mount(payload(LIST));
  const plain = JSON.parse(JSON.stringify(DONE));
  delete plain.entries.u1.followup;
  await openTriage(el, plain);
  const action = sectionOf(html(el), 'action');
  assert.ok(action.includes('href="/chat.html?id=u1"'), action);
  assert.ok(!action.includes('pane=ara'));
  assert.ok(/<h3 id="tri-action">Action suggested <span/.test(action), action);
  assert.ok(!action.includes('Ara is looking') && !action.includes('<b>Ara:</b>'));
  // With a follow-up: the pane, and "see Ara".
  const el2 = await mount(payload(LIST));
  await openTriage(el2, DONE);
  assert.ok(sectionOf(html(el2), 'action').includes('Action suggested — see Ara'));
});

await ok('a follow-up that left no note neither links to Ara nor heads the section with her', async () => {
  for (const state of ['skipped', 'failed', 'interrupted']) {
    const el = await mount(payload(LIST));
    const gone = JSON.parse(JSON.stringify(DONE));
    gone.entries.u1.followup = { state };
    await openTriage(el, gone);
    const action = sectionOf(html(el), 'action');
    assert.ok(action.includes('href="/chat.html?id=u1"') && !action.includes('pane=ara'), `${state}: ${action}`);
    assert.ok(!action.includes('see Ara'), state);
  }
});

await ok('a row that leaves the delete section and comes back must be armed for again', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  await el._triageAll('delete');
  assert.equal(el._triArmed, 'delete');
  // u3 is archived elsewhere: the section is gone…
  const u3 = el._data.chats.find((c) => c.id === 'u3');
  u3.archived = true; u3.list_group = 'archived';
  el._rerender();
  assert.ok(!html(el).includes('tri-sec delete'));
  // …and its Undo brings it back: disarmed, one tap arms, nothing deleted.
  u3.archived = false; u3.list_group = 'unread';
  el._rerender();
  assert.ok(sectionOf(html(el), 'delete').includes('>Delete all</button>'));
  await el._triageAll('delete');
  assert.ok(!wire.some((w) => w.url.endsWith('/delete')), 'the tap after the return only arms');
});

await ok('the armed Delete all says what happens to a mix of mails and messenger chats', async () => {
  const { deleteManyText } = await import('./chats.js');
  const mailChat = { channel: 'email' };
  const sig = { channel: 'signal' };
  assert.equal(deleteManyText([mailChat, mailChat]), 'Delete 2 chats? Mails move to the Trash');
  assert.equal(deleteManyText([sig]), 'Delete 1 chat? For good');
  assert.equal(deleteManyText([mailChat, sig, sig]),
    'Delete 3 chats? Mails move to the Trash, other chats are deleted for good');
});

await ok('an armed Delete all is disarmed when a poll changes its rows, and never deletes unseen rows', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  await el._triageAll('delete');
  assert.ok(sectionOf(html(el), 'delete').includes('Delete 1 chat? For good'));
  // A poll sorts u2 into delete too.
  const more = JSON.parse(JSON.stringify(DONE));
  more.entries.u2.bucket = 'delete';
  el._tri = more;
  el._rerender();
  const sec = sectionOf(html(el), 'delete');
  assert.deepEqual(ids(sec), ['u2', 'u3']);
  assert.ok(sec.includes('>Delete all</button>'), 'disarmed: the rows changed');
  // A tap on a stale arm (rows changed, not yet re-rendered) only re-arms.
  el._triArmed = 'delete';
  el._triArmedKey = 'u3';
  await el._triageAll('delete');
  assert.ok(!wire.some((w) => w.url.endsWith('/delete')), 'nothing deleted');
  assert.ok(sectionOf(html(el), 'delete').includes('Delete 2 chats? For good'));
  const run = el._triageAll('delete');
  await settle();
  take((w) => w.url === '/chats/u2/delete', 'u2').answer({ deleted: true });
  await settle();
  take((w) => w.url === '/chats/u3/delete', 'u3').answer({ deleted: true });
  await run;
});

await ok('Sort again presses with again; a refused press says why', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  el._startTriage(true);
  const post = takeTri('POST', 'the second press');
  assert.deepEqual(post.body, { again: true });
  post.answer({ error: 'chat triage is switched off (CHAT_TRIAGE)' }, 409);
  await settle();
  await settle();
  assert.ok(html(el).includes('Triage could not start: chat triage is switched off (CHAT_TRIAGE).'), html(el));
});

await ok('feedback: the saved notes, a send that saves one and sorts again, a ✕ that takes one back', async () => {
  const el = await mount(payload(LIST));
  const withNotes = JSON.parse(JSON.stringify(DONE));
  withNotes.notes = [{ id: 'n1', text: 'Bank mails <never> delete', at: '2026-11-25T10:00:00Z' }];
  await openTriage(el, withNotes);
  let h = html(el);
  assert.ok(h.includes('Feedback on the sorting <span class="tri-n">1</span>'), h);
  assert.ok(h.includes('Bank mails &lt;never&gt; delete'));
  assert.ok(h.includes('data-tri-note-rm="n1"'));
  assert.ok(/data-triage="feedback" disabled/.test(h), 'nothing typed: nothing to send');
  el._triFb = '  Invoices are always action.  ';
  el._rerender();
  assert.ok(!/data-triage="feedback" disabled/.test(html(el)));
  el._sendTriageFeedback();
  const post = takeTri('POST', 'the feedback');
  assert.deepEqual(post.body, { feedback: 'Invoices are always action.' });
  const after = JSON.parse(JSON.stringify(RUNNING));
  after.started = true;
  after.notes = [...withNotes.notes, { id: 'n2', text: 'Invoices are always action.', at: 'x' }];
  post.answer(after, 202);
  await settle();
  clearTimeout(el._triTimer); el._triTimer = null;
  h = html(el);
  assert.equal(el._triFb, '', 'the field is emptied once the server has it');
  assert.ok(h.includes('Invoices are always action.') && h.includes('<span class="tri-n">2</span>'));
  assert.ok(/data-triage="feedback" disabled/.test(h), 'the field is empty again');
  // Busy (sorting): a typed note can still be saved, for the next sort.
  el._triFb = 'Receipts are archive.';
  el._rerender();
  assert.ok(/data-triage="feedback">Save for the next sort</.test(html(el)), html(el));
  // A refused save keeps the words.
  el._triFb = 'x'.repeat(5);
  el._sendTriageFeedback();
  takeTri('POST', 'the second feedback').answer({ error: 'the feedback is empty' }, 400);
  await settle(); await settle();
  assert.equal(el._triFb, 'xxxxx');
  assert.ok(html(el).includes('Feedback not saved: the feedback is empty.'));
  // ✕
  el._removeTriageNote('n1');
  const rm = take((w) => w.method === 'POST' && w.url === '/chats/triage/notes', 'the removal');
  assert.deepEqual(rm.body, { remove: 'n1' });
  rm.answer({ ...after, notes: after.notes.slice(1) });
  await settle(); await settle();
  assert.ok(!html(el).includes('data-tri-note-rm="n1"'));
});

await ok('the list refresh does not rebuild the view under typing in the feedback field', async () => {
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  const before = html(el);
  el.shadowRoot.activeElement = { matches: (sel) => sel === '[data-tri-fb]' };
  el.load();
  takeList().answer(payload([...LIST, chat('u9', { unread: 1, list_group: 'unread', last: { ts: '2026-11-22T09:00:00Z', direction: 'in', text: 'new' } })]));
  await settle();
  assert.equal(html(el), before, 'not rebuilt while typing');
  el.shadowRoot.activeElement = null;
  el.load();
  takeList().answer(payload([...LIST, chat('u9', { unread: 1, list_group: 'unread', last: { ts: '2026-11-22T09:00:00Z', direction: 'in', text: 'new' } })]));
  await settle();
  assert.ok(ids(html(el)).includes('u9'), 'the next refresh shows it');
});

await ok('the view is in the URL: opening pushes #triage over the list\'s own entry', async () => {
  history.stack = [null];
  const urls = [];
  const push = history.pushState;
  history.pushState = function (st, _t, url) { urls.push(url); return push.call(this, st); };
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  history.pushState = push;
  assert.deepEqual(urls, ['/chats.html#triage']);
  assert.equal(history.state.triageEntry, true);
});

await ok('back from a chat opened in the view: restored from the last sorting, nothing pressed', async () => {
  history.stack = [null, { chatsTriage: true, triageEntry: true }];
  location.hash = '#triage';
  keepHistory = true;
  try {
    const el = await mount(payload(LIST));
    assert.equal(el._triage, true);
    assert.ok(html(el).includes('Loading the last sorting…'), html(el));
    takeTri('GET', 'the last sorting').answer(JSON.parse(JSON.stringify(DONE)));
    await settle(); await settle();
    assert.ok(!wire.some((w) => w.url === '/chats/triage' && w.method === 'POST'), 'no press');
    assert.deepEqual(ids(sectionOf(html(el), 'archive')), ['u2']);
    assert.ok(/Sorted .* ago/.test(html(el)), html(el));
    // Close: the list's own entry lies below, so back.
    el._closeTriage();
    assert.equal(el._triage, false);
    assert.equal(history.stack.length, 1);
  } finally {
    location.hash = '';
    keepHistory = false;
  }
});

await ok('a restore whose answer fails says so and asks again', async () => {
  history.stack = [null, { chatsTriage: true, triageEntry: true }];
  location.hash = '#triage';
  keepHistory = true;
  try {
    const el = await mount(payload(LIST));
    takeTri('GET', 'the restore').answer({ error: 'down' }, 502);
    await settle(); await settle();
    assert.ok(html(el).includes('The last sorting could not be loaded — trying again…'), html(el));
    assert.ok(el._triTimer, 'another ask is scheduled');
    clearTimeout(el._triTimer); el._triTimer = null;
    el._pollTriage();
    takeTri('GET', 'the retry').answer(JSON.parse(JSON.stringify(DONE)));
    await settle(); await settle();
    assert.deepEqual(ids(sectionOf(html(el), 'archive')), ['u2']);
    assert.equal(el._triTimer, null, 'loaded and nothing open: no further ask');
  } finally {
    location.hash = '';
    keepHistory = false;
  }
});

await ok('an unread chat with no word came in after the sorting only where a sorting finished', async () => {
  const el = await mount(payload(LIST));
  const cases = [
    [{ enabled: true, running: false, run: null, entries: {}, notes: [] }, 'Not sorted yet.'],
    [{ enabled: true, running: false, run: { state: 'interrupted' }, entries: {}, notes: [] }, 'Not sorted yet.'],
    [{ enabled: true, running: false, run: { state: 'done', total: 0, sorted: 0, failed: 0 }, entries: {}, notes: [] },
      'Came in after the sorting.'],
  ];
  el._triage = true;
  for (const [doc, line] of cases) {
    el._tri = doc;
    el._rerender();
    const unsorted = sectionOf(html(el), 'unsorted');
    assert.ok(unsorted.includes(line), `${doc.run && doc.run.state}: ${unsorted}`);
  }
});

await ok('a chat the user wrote in after the sorting leaves its bucket', async () => {
  const list = [
    // Answered after the sorting, and read since: no triage row at all.
    chat('d1', { unread: 0, list_group: 'read', inbound_seq: 5,
      last: { ts: '2026-11-21T10:00:00Z', direction: 'out', text: 'Lieber Donat' } }),
    // Answered after the sorting, still unread: not sorted, saying why.
    chat('d2', { unread: 1, list_group: 'unread', inbound_seq: 6,
      last: { ts: '2026-11-21T10:00:00Z', direction: 'out', text: 'ok' } }),
    // Answered before the sorting: the sorting saw it, its word stands.
    chat('d3', { unread: 0, list_group: 'read', inbound_seq: 7,
      last: { ts: '2026-11-21T08:00:00Z', direction: 'out', text: 'ok' } }),
    // A send that failed is no answer.
    chat('d4', { unread: 0, list_group: 'read', inbound_seq: 8,
      last: { ts: '2026-11-21T10:00:00Z', direction: 'out', failed: true, text: 'ok' } }),
  ];
  const at = '2026-11-21T09:00:00Z';
  const el = await mount(payload(list));
  el._triage = true;
  el._tri = { enabled: true, running: false, run: { state: 'done', total: 4, sorted: 4, failed: 0 }, notes: [],
    entries: Object.fromEntries(['d1', 'd2', 'd3', 'd4'].map((id, i) =>
      [id, { bucket: 'action', note: 'Answer.', reply: true, seq: 5 + i, at, run: 'r' }])) };
  el._rerender();
  const h = html(el);
  assert.deepEqual(ids(sectionOf(h, 'action')), ['d4', 'd3'], h);
  const unsorted = sectionOf(h, 'unsorted');
  assert.deepEqual(ids(unsorted), ['d2'], unsorted);
  assert.ok(unsorted.includes('You wrote in it since it was sorted.'), unsorted);
  assert.ok(!ids(h).includes('d1'));
});

await ok('opened cold on #triage: closing drops the hash in place instead of leaving the app', async () => {
  history.stack = [null];
  location.hash = '#triage';
  const replaced = [];
  const rep = history.replaceState;
  history.replaceState = function (st, _t, url) { replaced.push([st, url]); this.stack[this.stack.length - 1] = st; };
  keepHistory = true;
  try {
    const el = await mount(payload(LIST));
    takeTri('GET', 'the last sorting').answer(JSON.parse(JSON.stringify(DONE)));
    await settle(); await settle();
    assert.equal(history.state.chatsTriage, true);
    el._closeTriage();
    assert.equal(history.stack.length, 1, 'no back out of the app');
    const last = replaced[replaced.length - 1];
    assert.equal(last[1], '/chats.html');
    assert.ok(!last[0].chatsTriage);
  } finally {
    history.replaceState = rep;
    location.hash = '';
    keepHistory = false;
  }
});

await ok('no multi-select in the view; Close and back return to the tabs', async () => {
  history.stack = [null];  // the earlier checks' views were never closed
  const el = await mount(payload(LIST));
  await openTriage(el, DONE);
  el._enterSelect('u1');
  assert.equal(el._selecting, false);
  assert.equal(history.state && history.state.chatsTriage, true, 'the view is a history entry');
  el._closeTriage();
  assert.equal(el._triage, false);
  assert.ok(html(el).includes('role="tablist"'));
  await openTriage(el, DONE);
  history.back();
  assert.equal(el._triage, false, 'the back gesture closes it');
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the chat triage test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in os.listdir(COMPONENTS):
            if name.endswith(".js"):
                shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(_list_harness() + CHECKS)
        env = dict(os.environ, CHATS_FIXTURE=os.path.abspath(FIXTURE))
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120, env=env)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: chat triage view test")
            return 1
    print("PASS: chat triage view test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
