#!/usr/bin/env python3
"""Behavioural tests for the Contacts page (webapp/components/contacts.js):
a person's e-mail domains, and what the page says about VIP and the
instruction now that mail is gated like a messenger message.

The element is driven under Node with a scripted DOM and a scripted fetch,
and pins:

  * a contact's `domains` (GET /contacts) show in its row, as "anyone at",
    and in its editor, each with what it covers and a remove button;
  * a typed domain is filed as the address book spells it — lowercased, the
    gate's `*@example.org` / `*@*.example.org` and a leading `@` read as what
    they mean — and an address or a word with no dot is refused in place,
    with the text kept in the field and nothing sent;
  * saving an existing contact sends domains as a difference
    (`add_domains` / `remove_domains`), and neither key when nothing
    changed — so a save that touched something else undoes no domain filed
    elsewhere meanwhile;
  * creating a contact sends `domains`, and only when there are some;
  * a domain still in the field at Save is taken along, and one that is no
    domain stops the save;
  * the server's refusal of a domain (a public suffix: 400; somebody else's:
    409) is shown in its own words and the form stays open;
  * a domain is text from a contact file: it is escaped wherever it is shown;
  * the copy: no whitelist and no "frequent run" anywhere, VIP and the
    instruction are said to reach e-mail, and the search names domains.

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

// ── A scripted DOM, small enough to see through ──────────────────────────────
// The element renders into shadowRoot.innerHTML and listens on the shadow
// root; the test reads the markup and calls the element's handlers with the
// event shape they read (target.closest → an element with attributes).
const registry = new Map();
class FakeShadow {
  constructor() { this.innerHTML = ''; this.activeElement = null; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this.shadowRoot = null; }
  attachShadow() { this.shadowRoot = new FakeShadow(); return this.shadowRoot; }
  addEventListener() {}
  removeEventListener() {}
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = { addEventListener() {}, removeEventListener() {} };
globalThis.window = globalThis;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.location = { href: '/contacts.html' };

// ── A scripted gateway ───────────────────────────────────────────────────────
const wire = [];
globalThis.fetch = (url, init = {}) => new Promise((resolve) => {
  wire.push({
    url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null,
    answer: (doc, status = 200) => resolve({ ok: status < 400, status, json: async () => doc }),
  });
});
const settle = () => new Promise((r) => setTimeout(r, 0));
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what} on the wire, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};

const { domainSpec, domainCovers, handleTarget } = await import('./contacts.js');
const Page = registry.get('retinue-contacts');
assert.ok(Page, 'the element registered itself');

const person = (id, extra = {}) => ({
  id, name: 'N ' + id, chamber: 'private', sphere: null, tags: [], importance: null, permits: [],
  vip: false, instruction: null, handles: [], domains: [], ...extra,
});
const BOOK = () => ({
  locations: [{ chamber: 'private' }, { chamber: 'work' }], default: 'private',
  contacts: [
    person('acme', { name: 'Acme Ltd', domains: ['acme.example', '*.shop.example'], instruction: 'File receipts.' }),
    person('mara', { name: 'Mara Keller', handles: [{ channel: 'email', handle: 'mara@example.org' }], vip: true }),
  ],
});
const PROFILE = { focus: { spheres: ['family', 'customers'], modes: { work: { name: 'Work' } } } };

async function mount(book = BOOK()) {
  wire.length = 0;
  const el = new Page();
  el.connectedCallback();
  take((w) => w.url === '/contacts', 'GET /contacts').answer(book);
  take((w) => w.url === '/attention/profile', 'GET /attention/profile').answer(PROFILE);
  await settle();
  return el;
}
const html = (el) => el.shadowRoot.innerHTML;
// An element as the handlers read one: its attributes, and a value.
const target = (attrs, value = '') => ({
  value, closest: () => ({ value, getAttribute: (n) => (n in attrs ? attrs[n] : null) }),
});
const click = (el, act, v) => el._onClick({ target: target({ 'data-act': act, 'data-v': v, 'data-id': v }) });
const type = (el, set, value) => el._onInput({ type: 'input', target: target({ 'data-set': set }, value) });
const editor = (el) => {
  const h = html(el);
  const start = h.indexOf('<div class="editor">');
  assert.ok(start >= 0, 'an editor is open');
  return h.slice(start);
};
const domainRows = (el) => [...editor(el).matchAll(
  /<span class="ch">Domain<\/span><span>([^<]*)<\/span><span class="covers">([^<]*)<\/span>/g)].map((m) => [m[1], m[2]]);
const fieldValue = (el) => /data-set="add-domain" value="([^"]*)"/.exec(editor(el))[1];

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

await ok('a typed domain is filed as the address book spells it', async () => {
  assert.deepEqual(domainSpec('  Example.ORG '), { spec: 'example.org' });
  assert.deepEqual(domainSpec('*.Example.org'), { spec: '*.example.org' });
  // The gate's spelling of the same two, and a leading @.
  assert.deepEqual(domainSpec('*@example.org'), { spec: 'example.org' });
  assert.deepEqual(domainSpec('*@*.example.org'), { spec: '*.example.org' });
  assert.deepEqual(domainSpec('@example.org'), { spec: 'example.org' });
  // Nothing typed is nothing to add, not an error.
  assert.deepEqual(domainSpec('   '), { spec: '' });
  assert.deepEqual(domainSpec(null), { spec: '' });
  // The two mistakes caught before the round trip.
  assert.match(domainSpec('mara@example.org').error, /address/);
  assert.match(domainSpec('*@mara@example.org').error, /address/);
  for (const junk of ['example', 'ex*ample.org', '*.*.example.org', 'two words.org', '*.']) {
    assert.ok(domainSpec(junk).error, junk);
  }
  assert.equal(domainCovers('example.org'), 'every address @example.org');
  assert.equal(domainCovers('*.example.org'), 'example.org and every domain below it');
});

await ok('a row shows the domains as "anyone at", beside the handles', async () => {
  const el = await mount();
  const h = html(el);
  assert.ok(h.includes('<div class="reach"><span>@acme.example</span><span>@*.shop.example</span></div>'), h);
  // A person with no domain shows only handles — each one a link to their chats.
  assert.ok(/<div class="reach"><button type="button" class="go" data-act="go" data-id="mara" data-ch="email" data-h="mara@example.org"[^>]*><span class="via">E-mail<\/span> mara@example.org<\/button><\/div>/.test(h), h);
  const bare = await mount({ ...BOOK(), contacts: [person('x')] });
  assert.ok(html(bare).includes('<div class="reach"><span>no handles</span></div>'));
});

await ok('the editor lists each domain with what it covers, and removes one', async () => {
  const el = await mount();
  click(el, 'open', 'acme');
  assert.deepEqual(domainRows(el), [
    ['acme.example', 'every address @acme.example'],
    ['*.shop.example', 'shop.example and every domain below it'],
  ]);
  assert.ok(editor(el).includes('aria-label="Remove the domain acme.example"'), 'the remove button says what it removes');
  click(el, 'drop-domain', '0');
  assert.deepEqual(domainRows(el).map((r) => r[0]), ['*.shop.example']);
  const none = await mount();
  click(none, 'open', 'mara');
  assert.deepEqual(domainRows(none), []);
  assert.ok(editor(none).includes('No domain.'), 'an empty list says so');
});

await ok('adding a domain normalises it, refuses an address in place, and adds nothing twice', async () => {
  const el = await mount();
  click(el, 'open', 'mara');
  type(el, 'add-domain', '*@Example.ORG');
  click(el, 'add-domain');
  assert.deepEqual(domainRows(el).map((r) => r[0]), ['example.org']);
  assert.equal(fieldValue(el), '', 'the field is cleared once its text is taken');
  type(el, 'add-domain', 'example.org');
  click(el, 'add-domain');
  assert.deepEqual(domainRows(el).map((r) => r[0]), ['example.org'], 'the same domain is not listed twice');
  type(el, 'add-domain', 'boss@work.example');
  click(el, 'add-domain');
  assert.deepEqual(domainRows(el).map((r) => r[0]), ['example.org']);
  assert.equal(fieldValue(el), 'boss@work.example', 'what was typed stays in the field');
  assert.ok(/<div class="err">That is an address, not a domain/.test(editor(el)), editor(el));
  assert.equal(wire.length, 0, 'nothing was sent');
  // A good one clears the complaint.
  type(el, 'add-domain', 'work.example');
  click(el, 'add-domain');
  assert.ok(!editor(el).includes('class="err"'));
});

await ok('saving sends domains as a difference, and no key when none changed', async () => {
  const el = await mount();
  click(el, 'open', 'acme');
  click(el, 'drop-domain', '1');
  type(el, 'add-domain', 'acme.co.example');
  click(el, 'add-domain');
  click(el, 'save');
  const post = take((w) => w.method === 'POST', 'the save');
  assert.equal(post.url, '/contacts/acme');
  assert.deepEqual(post.body.add_domains, ['acme.co.example']);
  assert.deepEqual(post.body.remove_domains, ['*.shop.example']);
  assert.ok(!('domains' in post.body), 'a change never restates the whole list');
  post.answer({ contact: {} });
  await settle();
  take((w) => w.url === '/contacts', 'the reload').answer(BOOK());
  await settle();
  assert.ok(!html(el).includes('<div class="editor">'), 'the editor closes on success');

  // Something else changed, the domains did not: neither key is sent.
  click(el, 'open', 'acme');
  click(el, 'vip');
  click(el, 'save');
  const other = take((w) => w.method === 'POST', 'the second save');
  assert.equal(other.body.vip, true);
  assert.ok(!('add_domains' in other.body) && !('remove_domains' in other.body) && !('domains' in other.body),
    JSON.stringify(other.body));
});

await ok('a new contact sends its domains, and only when it has some', async () => {
  const el = await mount();
  click(el, 'new');
  type(el, 'name', 'Spitex Team');
  type(el, 'add-domain', 'Spitex.example');
  click(el, 'add-domain');
  click(el, 'save');
  const post = take((w) => w.method === 'POST', 'the create');
  assert.equal(post.url, '/contacts');
  assert.deepEqual(post.body.domains, ['spitex.example']);
  assert.equal(post.body.chamber, 'private');
  assert.deepEqual(post.body.handles, []);

  const plain = await mount();
  click(plain, 'new');
  type(plain, 'name', 'Nadia');
  click(plain, 'save');
  const bare = take((w) => w.method === 'POST', 'the second create');
  assert.ok(!('domains' in bare.body), JSON.stringify(bare.body));
});

await ok('a domain still in the field at Save is taken along; one that is none stops the save', async () => {
  const el = await mount();
  click(el, 'open', 'mara');
  type(el, 'add-domain', '@keller.example');
  click(el, 'save');
  const post = take((w) => w.method === 'POST', 'the save');
  assert.deepEqual(post.body.add_domains, ['keller.example']);

  const bad = await mount();
  click(bad, 'open', 'mara');
  type(bad, 'add-domain', 'not a domain');
  click(bad, 'save');
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'nothing is saved around a refused field');
  assert.ok(editor(bad).includes('class="err"'));
  assert.equal(fieldValue(bad), 'not a domain');
});

await ok("the server's refusal of a domain is shown in its words and the form stays", async () => {
  for (const [status, error] of [[400, "'co.uk' is a public suffix, not one sender's domain"],
                                 [409, 'acme.example already belongs to Acme Ltd']]) {
    const el = await mount();
    click(el, 'open', 'mara');
    type(el, 'add-domain', 'acme.example');
    click(el, 'save');
    take((w) => w.method === 'POST', 'the save').answer({ error }, status);
    await settle();
    const h = editor(el);
    assert.ok(h.includes('Could not save ('), h);
    assert.ok(h.includes(error.replace(/'/g, '&#39;')), h);
    assert.deepEqual(domainRows(el).map((r) => r[0]), ['acme.example'], 'what was entered is still there');
    assert.equal(wire.filter((w) => w.url === '/contacts').length, 0, 'no reload after a refusal');
  }
});

await ok('a domain is escaped wherever it is shown', async () => {
  const hostile = '"><img src=x onerror=alert(1)>.example';
  const el = await mount({ ...BOOK(), contacts: [person('h', { domains: [hostile] })] });
  assert.ok(!html(el).includes('<img'), html(el));
  click(el, 'open', 'h');
  const h = editor(el);
  assert.ok(!h.includes('<img') && h.includes('&lt;img src=x onerror=alert(1)&gt;.example'), h);
  assert.ok(!/aria-label="Remove the domain "/.test(h), 'the label cannot be closed from inside the value');
});

await ok('the copy: no whitelist, e-mail reached by VIP and the instruction, domains searched', async () => {
  const el = await mount();
  click(el, 'open', 'acme');
  const h = html(el).replace(/\s+/g, ' ');
  assert.ok(!/whitelist/i.test(h) && !/frequent run/i.test(h), 'the retired e-mail triage is not described');
  assert.ok(h.includes('placeholder="Search names, e-mail addresses and domains"'));
  assert.ok(/A VIP's messages ring at once.*e-mail and its domains included/.test(h), h);
  assert.ok(!h.includes('rings nothing'), 'a prior and permits can ring a mail: the page never says it cannot');
  assert.ok(/neither a VIP nor instructed buys no model turn; by default it is only listed, and it waits for the digest or rings only as their importance and the modes they may interrupt in say/.test(h), h);
  assert.ok(/longer than a minute \(ten for a mail\), the message rings anyway/.test(h), 'the mail verdict timeout is its own');
  assert.ok(!h.includes('(never sends)') && h.includes('it sends a mail only where this says to, under the mailbox'), 'an instructed mail turn may send');
  assert.ok(/works each of their messages by this, on every channel above — a mail from one of their addresses or domains included/.test(h), h);
  assert.ok(!h.includes('every messenger channel above'), 'the instruction is not said to be messenger-only');
  assert.ok(h.includes('Writes e-mail from'));
});

// ── A handle leads to its conversation ──────────────────────────────────────
const TAPPABLE = () => ({
  locations: [{ chamber: 'private' }], default: 'private',
  contacts: [person('mara', { name: 'Mara Keller', handles: [
    { channel: 'whatsapp', handle: '+41794456312' },
    { channel: 'email', handle: 'mara@example.org' },
    { channel: 'sms', handle: 'HBL' },
  ] })],
});
const tap = (el, channel, handle, id = 'mara') => el._onClick({
  target: target({ 'data-act': 'go', 'data-id': id, 'data-ch': channel, 'data-h': handle }) });
const pickClick = (el, act, id) => el._onClick({ target: target({ 'data-act': act, 'data-id': id }) });
const lookup = (channel, handle) => take((w) => w.url.startsWith('/contacts/chats?')
  && new URL(w.url, 'http://x').searchParams.get('channel') === channel
  && new URL(w.url, 'http://x').searchParams.get('handle') === handle
  && new URL(w.url, 'http://x').searchParams.get('person') === 'mara', `GET /contacts/chats for ${handle}`);
const WA = 'whatsapp:~%2B41790000000:41794456312@s.whatsapp.net';

await ok('every handle of a row is a link to its chats; a domain is none', async () => {
  const el = await mount(TAPPABLE());
  const h = html(el);
  assert.ok(h.includes('data-act="go" data-id="mara" data-ch="whatsapp" data-h="+41794456312"'), h);
  assert.ok(h.includes('data-act="go" data-id="mara" data-ch="email" data-h="mara@example.org"'), h);
  click(el, 'open', 'mara');
  assert.ok(editor(el).includes('data-ch="whatsapp" data-h="+41794456312"'), 'the editor links them too');
  // One added in the form, not saved yet, is text: nothing to look up.
  type(el, 'add-channel', 'signal');
  type(el, 'add-handle', '+41790001122');
  click(el, 'add-handle');
  assert.ok(editor(el).includes('<span>+41790001122</span>'), editor(el));
  assert.ok(!editor(el).includes('data-h="+41790001122"'));
});

await ok('a messenger handle with one chat opens it at once', async () => {
  const el = await mount(TAPPABLE());
  location.href = '/contacts.html';
  tap(el, 'whatsapp', '+41794456312');
  assert.ok(html(el).includes('Looking up their chats'));
  lookup('whatsapp', '+41794456312').answer({ handle: { channel: 'whatsapp', handle: '+41794456312' },
    chats: [{ id: WA, channel: 'whatsapp', account: '+41790000000', name: 'Mara', ts: '2026-11-20T10:00:00Z' }],
    new: [{ id: WA, account: '+41790000000' }] });
  await settle();
  assert.equal(location.href, `/chat.html?id=${encodeURIComponent(WA)}`);
  assert.ok(!html(el).includes('class="scrim"'), 'nothing is left open');
});

await ok('a messenger handle with no chat starts one, under its account', async () => {
  const el = await mount(TAPPABLE());
  location.href = '/contacts.html';
  tap(el, 'whatsapp', '+41794456312');
  lookup('whatsapp', '+41794456312').answer({ chats: [], new: [{ id: WA, account: '+41790000000' }] });
  await settle();
  assert.equal(location.href, `/chat.html?id=${encodeURIComponent(WA)}`);
});

await ok('two accounts and no chat: the user picks which to write from', async () => {
  const el = await mount(TAPPABLE());
  location.href = '/contacts.html';
  tap(el, 'whatsapp', '+41794456312');
  const other = 'whatsapp:~%2B41791111111:41794456312@s.whatsapp.net';
  lookup('whatsapp', '+41794456312').answer({ chats: [],
    new: [{ id: WA, account: '+41790000000' }, { id: other, account: '+41791111111' }] });
  await settle();
  const h = html(el);
  assert.equal(location.href, '/contacts.html', 'nothing is opened unasked');
  assert.ok(h.includes('New WhatsApp chat <span class="meta">as +41790000000</span>'), h);
  assert.ok(h.includes('as +41791111111'), h);
  pickClick(el, 'pick-chat', other);
  assert.equal(location.href, `/chat.html?id=${encodeURIComponent(other)}`);
});

await ok('an e-mail address asks: each thread it is in, newest first, and a new mail', async () => {
  const el = await mount(TAPPABLE());
  location.href = '/contacts.html';
  tap(el, 'email', 'mara@example.org');
  lookup('email', 'mara@example.org').answer({ handle: { channel: 'email', handle: 'mara@example.org' },
    chats: [
      { id: 'email:~me%40x.org:<b@x>', channel: 'email', account: 'me@x.org', name: 'Re: <b>Dinner</b>', ts: '2026-11-21T10:00:00Z' },
      { id: 'email:~me%40x.org:<a@x>', channel: 'email', account: 'me@x.org', name: 'Invoice', ts: '2026-10-01T10:00:00Z', list_group: 'archived' },
    ],
    new: [{ compose: true, accounts: ['me@x.org'] }] });
  await settle();
  const h = html(el);
  assert.equal(location.href, '/contacts.html', 'even a single thread is a choice, beside a new mail');
  assert.ok(h.includes('Mail with mara@example.org'), h);
  assert.ok(h.includes('Re: &lt;b&gt;Dinner&lt;/b&gt;'), 'a subject is escaped');
  assert.ok(h.indexOf('Dinner') < h.indexOf('Invoice'), 'in the order the server gave');
  assert.ok(h.includes('archived'));
  assert.ok(h.includes('data-act="pick-compose">New e-mail</button>'), h);
  pickClick(el, 'pick-chat', 'email:~me%40x.org:<a@x>');
  assert.equal(location.href, `/chat.html?id=${encodeURIComponent('email:~me%40x.org:<a@x>')}`);
});

await ok('what a tap does, decided from the answer', async () => {
  const mailNew = { compose: true, accounts: ['me@x.org'] };
  assert.deepEqual(handleTarget({ chats: [], new: [mailNew] }, 'email'), { compose: mailNew });
  assert.ok(handleTarget({ chats: [{ id: 'email:a' }], new: [mailNew] }, 'email').ask);
  assert.match(handleTarget({ chats: [], new: [] }, 'email').none, /No mailbox/);
  assert.deepEqual(handleTarget({ chats: [{ id: 'signal:+41' }], new: [{ id: 'signal:+41' }] }, 'signal'), { go: 'signal:+41' });
  assert.ok(handleTarget({ chats: [{ id: 'a' }, { id: 'b' }], new: [] }, 'signal').ask);
  assert.match(handleTarget({ chats: [], new: [] }, 'sms').none, /none can be started/);
});

await ok('a handle nothing can be sent to says so, and the sheet closes', async () => {
  const el = await mount(TAPPABLE());
  location.href = '/contacts.html';
  tap(el, 'sms', 'HBL');
  lookup('sms', 'HBL').answer({ chats: [], new: [] });
  await settle();
  assert.ok(html(el).includes('none can be started from here'), html(el));
  pickClick(el, 'pick-dismiss');
  assert.ok(!html(el).includes('class="scrim"'));
  // A failed lookup is shown in place too.
  tap(el, 'whatsapp', '+41794456312');
  lookup('whatsapp', '+41794456312').answer({ error: 'life store unreachable' }, 502);
  await settle();
  assert.ok(html(el).includes('Could not look up their chats (life store unreachable)'), html(el));
  assert.equal(location.href, '/contacts.html');
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the contacts page test needs it")
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
            print("FAIL: contacts page test")
            return 1
    print("PASS: contacts page test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
