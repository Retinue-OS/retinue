// The dossier engine: the attention prototype's engine (docs/attention-model.md)
// with docs/dossiers.md underneath — every item is a dossier (a relationship, a
// topic, an Ara-only topic or a standing one), messages belong to exactly one
// of them, events reference messages across them, and Inbox Zero proposes a way
// out for every mail in the inbox. Pure logic, no DOM — the same file runs in
// the browser, in Deno and in Node.
(function (root) {
  'use strict';
  const B = root.Backends;
  const DAY = 1440, H = 60;
  const LEVELS = ['passive', 'active', 'time-sensitive', 'critical'];
  const rank = (l) => LEVELS.indexOf(l);
  const DAYNAMES = ['Thu', 'Fri', 'Sat', 'Sun', 'Mon', 'Tue', 'Wed'];
  const BASE_DAY = 3; // Thursday 3 September 2026

  const pad = (n) => String(n).padStart(2, '0');
  function hhmm(m) { const x = ((Math.round(m) % DAY) + DAY) % DAY; return `${pad(Math.floor(x / 60))}:${pad(x % 60)}`; }
  const dayIndex = (m) => Math.floor(m / DAY);
  function dateText(m) { const d = dayIndex(m); const name = DAYNAMES[((d % 7) + 7) % 7]; const dom = BASE_DAY + d; return dom <= 30 ? `${name} ${dom} Sept` : `${name} ${dom - 30} Oct`; }
  function whenText(m, now) { const d = dayIndex(m), dn = dayIndex(now); if (d === dn) return hhmm(m); if (d === dn + 1) return `tomorrow ${hhmm(m)}`; if (d === dn - 1) return `yesterday ${hhmm(m)}`; if (Math.abs(d - dn) < 7) return `${DAYNAMES[((d % 7) + 7) % 7]} ${hhmm(m)}`; return dateText(m); }
  const nice = (v) => (Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));
  function dur(min) { min = Math.round(min); if (min < 60) return `${min} min`; if (min < DAY) return `${nice(min / 60)} h`; return `${nice(min / DAY)} d`; }

  // A mode is a preset of the two controls (docs/dossiers.md, "Focus and aperture"):
  // the FOCUS — what you are on: nothing (flow), some spheres, or one dossier —
  // and the APERTURE — how far from the focus you see, as rings of distance in
  // the relations the data has: within (the tree), participants, spheres and the
  // spheres within spheres. Urgency is a different question: critical rings
  // always; what is listed rings from time-sensitive; the "below the aperture"
  // line counts the urgent ones out of sight.
  const APERTURES = [
    { id: 'strict', ring: 0, name: 'strictly',   glyph: '◔', blurb: 'the focus itself: the dossier and what is within it, or the sphere' },
    { id: 'people', ring: 1, name: '+ people',   glyph: '◑', blurb: 'plus its parent and siblings, and whatever shares a participant' },
    { id: 'nearby', ring: 2, name: '+ nearby',   glyph: '◕', blurb: 'plus the same sphere and the spheres around it' },
    { id: 'all',    ring: 3, name: 'everything', glyph: '●', blurb: 'no focus: everything pending shows' },
  ];
  // Spheres within spheres (focus.json `within`): the one structure that says which spheres are near.
  const SPHERE_WITHIN = { finance: 'admin' };
  const MODES = {
    off:    { id: 'off',    name: 'Off',       focus: null,                                                   aperture: 'strict', color: '#3a4250', blurb: 'flow, only critical; the digest waits for the morning' },
    home:   { id: 'home',   name: 'Home',      focus: { spheres: ['family', 'health'], tags: [] },            aperture: 'strict', color: '#a86f2c', blurb: 'focus on family and health' },
    deep:   { id: 'deep',   name: 'Deep work', focus: null,                                                   aperture: 'strict', color: '#0f4f57', blurb: 'flow: only critical breaks through' },
    open:   { id: 'open',   name: 'Open',      focus: null,                                                   aperture: 'all',    color: '#8a94a0', blurb: 'no focus, everything shows; time-sensitive rings' },
    work:   { id: 'work',   name: 'Work',      focus: { spheres: ['customers', 'admin', 'health'], tags: ['health'] }, aperture: 'strict', color: '#2f8a90', blurb: 'focus on customers, admin and health' },
    social: { id: 'social', name: 'Social',    focus: { spheres: ['friends', 'family'], tags: ['health'] },   aperture: 'strict', color: '#7a4f96', blurb: 'focus on friends and family' },
  };
  const SCHEDULE = [[0, 'off'], [7 * H, 'home'], [8 * H, 'deep'], [12 * H, 'open'], [13 * H, 'work'], [17 * H, 'open'], [18 * H, 'social'], [22 * H, 'off']];
  const DIGEST_TIMES = [8 * H, 12 * H, 17 * H, 21 * H];
  const SWEEP_EVERY = 30;
  const scheduledId = (m) => { const x = ((m % DAY) + DAY) % DAY; let id = 'off'; for (const [start, mid] of SCHEDULE) if (x >= start) id = mid; return id; };
  const LEVEL_TABLE = [['active', 'time-sensitive', 'time-sensitive'], ['passive', 'active', 'active'], ['passive', 'passive', 'active']];
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const KIND_LABEL = { relationship: 'relationship', group: 'group', ara: 'topic with Ara', topic: 'topic', standing: 'standing' };

  class DossierEngine {
    constructor() { this.listeners = []; this.reset(); }

    on(fn) { this.listeners.push(fn); return this; }
    emit(ev) { if (this.quiet) return; ev.t = this.now; this.version += 1; this.feed.push(ev); for (const fn of this.listeners) fn(ev); }

    reset() {
      this.now = 0; this.items = []; this.byId = new Map(); this.feed = []; this.pushes = []; this.digests = [];
      this.manualMode = null; this.doneToday = 0; this.corrections = 0; this.modeHistory = []; this.version = 0; this.seq = 0;
      this.modes = clone(MODES);
      // The hand controls over the preset: a focus set for this stint, an aperture widened for a few minutes or narrowed for the stint.
      this.override = { focus: undefined, aperture: null };
      this.profile = { priors: { ...B.PRIORS }, leads: { ...B.LEAD_DEFAULTS }, permits: { off: [], home: [], deep: [], open: [], work: [], social: [] }, learned: [] };
      this.eventsByMinute = new Map();
      for (const be of B.all) for (const ev of be.events(0, DAY)) { const m = Math.round(ev.at); if (!this.eventsByMinute.has(m)) this.eventsByMinute.set(m, []); this.eventsByMinute.get(m).push(ev); }
      // Yesterday's arrivals and the dossiers on file: already seen, already released.
      this.quiet = true;
      for (const p of B.topics.initial()) this._ingestEvent({ type: 'topic', at: -DAY, ...p });
      for (const be of B.all) for (const ev of be.events(-Infinity, 0)) this._ingestEvent(ev);
      for (const i of this.items) { if (i.state === 'open' && !i.released) this._release(i, 'yesterday'); i.lastLevel = this.level(i); }
      this.quiet = false; this._trackMode();
    }
    _trackMode() { const id = this.mode().id; const last = this.modeHistory[this.modeHistory.length - 1]; if (!last || last.id !== id) this.modeHistory.push({ from: this.now, id }); }

    // ---- dossiers --------------------------------------------------------------
    _leadFor(kind) { const v = this.profile.leads[kind]; return v != null ? v : this.profile.leads.default; }
    _newItem(o) {
      const item = Object.assign({ tags: [], count: 1, state: 'open', released: false, releasedBy: null, releasedAt: null, snoozedUntil: null, boost: 0, pushed: [], lastLevel: null, due: null, critical: false, actor: 'you', stream: [], chips: null, episode: 0, lastActivity: -DAY, standing: false }, o);
      if (item.lead == null) { item.lead = this._leadFor(item.kindLabel); item.leadFrom = 'kind default'; }
      return item;
    }
    _add(item) { this.items.push(item); this.byId.set(item.id, item); return item; }
    _touch(item, at) { item.lastActivity = Math.max(item.lastActivity != null ? item.lastActivity : -Infinity, at != null ? at : this.now); }
    _ingestEvent(ev) {
      if (ev.type === 'message') return this._onMessage(ev);
      if (ev.type === 'thread') {
        const c = this._add(this._newItem({ id: ev.id, kind: 'ara', agent: ev.agent, title: ev.title, body: ev.body, sphere: ev.sphere, tags: ev.tags || [], importance: ev.importance, importanceFrom: ev.agent, due: ev.due != null ? ev.due : null, critical: !!ev.critical, kindLabel: ev.kind }));
        c.stream.push({ who: 'ara', text: ev.body, t: ev.at, agent: ev.agent }); this._touch(c, ev.at);
        return this._arrive(c, ev.at);
      }
      if (ev.type === 'wake') {
        const p = this.byId.get(ev.id); if (!p) return;
        p.state = 'open'; p.resting = false; p.body = `Woken by recurring-projects: ${ev.recurring} cadence, due ${hhmm(ev.next_due)}.`; p.due = ev.next_due; p.released = false;
        p.stream.push({ who: 'event', author: 'job:recurring-projects', kind: 'status', text: p.body, t: ev.at }); this._touch(p, ev.at);
        return this._arrive(p, ev.at);
      }
      if (ev.type === 'topic') {
        const lead = ev.remind_before != null ? ev.remind_before : null;
        const parent = ev.within ? this.byId.get(ev.within) : null;
        const p = this._add(this._newItem({ id: ev.id, kind: ev.standing ? 'standing' : 'topic', standing: !!ev.standing, title: ev.title, body: ev.body || (ev.current_actor === 'you' ? 'Your move.' : `Parked on ${ev.current_actor}.`), sphere: ev.sphere || (parent ? parent.sphere : 'admin'), tags: ev.tags || [], importance: ev.importance, importanceFrom: 'frontmatter', due: ev.expected_by != null ? ev.expected_by : (ev.next_due != null && !ev.paused ? ev.next_due : null), lead, leadFrom: lead != null ? 'remind_before' : undefined, kindLabel: ev.kind, actor: ev.current_actor, waiting_since: ev.waiting_since != null ? ev.waiting_since : null, nextDue: ev.next_due != null ? ev.next_due : null, recurring: ev.recurring || null, within: parent ? parent.id : null, participants: ev.participants || null }));
        if (p.lead == null) { p.lead = this._leadFor(p.kindLabel); p.leadFrom = 'kind default'; }
        p.lastActivity = ev.waiting_since != null ? ev.waiting_since : (ev.doneAt != null ? ev.doneAt : -3 * DAY);
        if (ev.resolved) { p.state = 'done'; p.doneAt = ev.doneAt != null ? ev.doneAt : -DAY; return; }
        if (ev.paused) { p.state = 'resting'; p.resting = true; return; }
        if (ev.standing) { p.state = 'quiet'; p.actor = 'Ara'; p.inbox = []; return; }
        return this._arrive(p, ev.at);
      }
    }
    _relationship(msg) {
      const c = B.CONTACTS[msg.sender] || {}; const prior = this.profile.priors[msg.sender];
      const item = this._newItem({ id: `rel:${msg.chat}`, kind: c.group ? 'group' : 'relationship', channel: c.channel || 'messenger', mail: c.channel === 'E-mail', org: c.org || null, gate: c.gate || 'batch', title: msg.chat, body: msg.text, sender: msg.sender, sphere: c.sphere || 'friends', tags: [], importance: prior != null ? prior : 2.5, importanceFrom: 'prior', due: null, kindLabel: 'message', state: 'quiet' });
      return item;
    }
    _entry(msg) { return { who: 'in', text: msg.text, t: msg.at, from: msg.from || null, mail: !!msg.mail, subject: msg.subject || null, attachments: msg.attachments || [], inbox: !!msg.mail, judged: !msg.mail, links: [], uid: ++this.seq }; }
    // Put a classification on a relationship's pending state and let it arrive.
    _classify(item, tr, at, by) {
      const fresh = item.state !== 'open';
      if (fresh) Object.assign(item, { state: 'open', released: false, releasedBy: null, snoozedUntil: null, boost: 0, count: 1, pushed: [], kindLabel: tr.kind || 'message', tags: tr.tags || [], due: tr.due != null ? tr.due : null, chips: null, episode: item.episode + 1 });
      if (tr.importance != null) { item.importance = tr.importance; item.importanceFrom = by; } else { item.importance = this.profile.priors[item.sender] != null ? this.profile.priors[item.sender] : item.importance; item.importanceFrom = 'prior'; }
      item.lead = this._leadFor(item.kindLabel); item.leadFrom = 'kind default';
      return this._arrive(item, at);
    }
    _onMessage(msg) {
      const id = `rel:${msg.chat}`; let item = this.byId.get(id); const tr = msg.triage || {};
      const isNew = !item; if (isNew) item = this._add(this._relationship(msg));
      const entry = this._entry(msg); item.stream.push(entry); item.body = msg.text; this._touch(item, msg.at);
      // A mail from a sender who is no VIP: visible at once under Contacts, judged at the next batch. Inbox Zero holds it meanwhile.
      if (msg.mail && item.gate !== 'vip') {
        const iz = this.byId.get('inbox-zero'); if (iz) { iz.inbox.push({ item, entry, triage: tr }); iz.state = 'open'; iz.actor = 'Ara'; iz.body = `${iz.inbox.length} mail${iz.inbox.length === 1 ? '' : 's'} in the inbox, judged at the next batch`; this._touch(iz, msg.at); }
        return this.emit({ kind: 'arrive', item, text: `Mail from ${msg.sender} (“${msg.subject}”): in the inbox and in the relationship at once — no model turn; ${msg.sender} is no VIP, so Inbox Zero judges it at the next batch (${this.nextBreakpointText()}).` });
      }
      if (item.state !== 'open') return this._classify(item, tr, msg.at, 'triage');
      const prevKind = item.kindLabel, prevDue = item.due;
      item.count += 1; item.arrived = msg.at;
      if (tr.importance != null && tr.importance > item.importance) { item.importance = tr.importance; item.importanceFrom = 'triage'; }
      if (tr.due != null) item.due = tr.due;
      const changed = (tr.kind && tr.kind !== prevKind) || (tr.due != null && prevDue == null);
      const policy = this._repeatPolicy(item);
      if (item.released) return this.emit({ kind: 'arrive', item, text: `${item.sender} again (${item.count} messages) — already in your list.` });
      if (policy.escalate || changed) { item.boost = Math.min(item.boost + 1, 2); this.emit({ kind: 'system', item, text: `${item.sender} writes again: ${policy.escalate ? policy.reason : 'the classification changed'} — one level up.` }); return this._reevaluate(item, 'the repeat'); }
      this.emit({ kind: 'arrive', item, text: `${item.sender} writes again (${item.count} messages). The repeat policy for ${item.sphere} is off and the classification is unchanged: nothing escalates; ${this.deliveryText(item)}.` });
    }
    _repeatPolicy(item) { const mode = this.mode(); if (item.sphere === 'family' && mode.id === 'off') return { escalate: true, reason: 'a family repeat breaks through in Off (the repeated-caller rule)' }; return { escalate: false }; }

    // ---- Inbox Zero: the batch ----------------------------------------------------------
    _batch(reason) {
      const iz = this.byId.get('inbox-zero'); if (!iz) return;
      const due = iz.inbox.filter((x) => !x.entry.judged); if (!due.length) return;
      const D = root.Dialogues.INBOX; const lines = [];
      for (const x of due) {
        x.entry.judged = true; const p = D[x.item.sender] || {};
        this._classify(x.item, x.triage, this.now, 'the batch');
        lines.push({ item: x.item, entry: x.entry, proposal: p.proposal || 'archive', chips: p.chips || [] });
      }
      // The batch quotes every mail it judged: the proposal sits under the message itself, so nothing has to be asked for.
      const ev = { who: 'event', author: 'agent:Ara', kind: 'turn', t: this.now, text: `Batch at ${hhmm(this.now)} — ${due.length} mail${due.length === 1 ? '' : 's'} judged:`, proposals: lines.map((l) => ({ quote: this._quote(l.item, l.entry), proposal: l.proposal, importance: l.item.importance, due: l.item.due })), links: [] };
      iz.stream.push(ev); this._touch(iz); iz.state = 'open'; iz.actor = 'you'; iz.released = true; iz.releasedBy = 'batch';
      iz.chips = [].concat(...lines.map((l) => l.chips)); iz.body = this._inboxBody(iz);
      this.emit({ kind: 'system', item: iz, text: `Inbox Zero (${reason}): ${lines.map((l) => `${l.item.title} → ${l.proposal}`).join('; ')}. The proposals are in its stream; what you deal with elsewhere leaves the inbox by itself.` });
    }
    _inboxBody(iz) { const open = iz.inbox.filter((x) => x.entry.inbox); if (!open.length) return 'The inbox is empty.'; const oldest = Math.min(...open.map((x) => x.entry.t)); return `${open.length} mail${open.length === 1 ? '' : 's'} waiting, oldest ${dur(this.now - oldest)}`; }
    // A mail leaves the inbox: replied to, referenced into a dossier, or marked done.
    _leaveInbox(entry, how) {
      if (!entry || !entry.mail || !entry.inbox) return;
      entry.inbox = false; entry.archived = how;
      const iz = this.byId.get('inbox-zero'); if (!iz) return;
      const x = iz.inbox.find((y) => y.entry === entry);
      if (x) { x.entry.judged = true; const D = root.Dialogues.INBOX[x.item.sender] || {}; iz.chips = (iz.chips || []).filter((c) => !(D.chips || []).includes(c)); }
      iz.body = this._inboxBody(iz);
      if (!iz.inbox.some((y) => y.entry.inbox)) { iz.state = 'quiet'; iz.chips = []; }
      iz.stream.push({ who: 'event', author: 'job:gateway', kind: 'status', t: this.now, text: `“${entry.subject}” left the inbox — ${how}.`, links: [] }); this._touch(iz);
      this.emit({ kind: 'system', item: iz, text: `Inbox Zero: “${entry.subject}” archived (${how}) — no proposal needed.` });
    }
    inboxCount() { const iz = this.byId.get('inbox-zero'); return iz ? iz.inbox.filter((x) => x.entry.inbox).length : 0; }

    // ---- the three fields -------------------------------------------------------
    level(item, now = this.now) {
      if (item.critical) return 'critical';
      const row = item.importance >= 3.5 ? 0 : item.importance >= 1.5 ? 1 : 2;
      let col = 0;
      if (item.due != null) { const left = item.due - now; const u = left / Math.max(item.lead, 1); col = left <= 0 ? 2 : u <= 1 / 3 ? 2 : u <= 1 ? 1 : 0; }
      let lvl = LEVEL_TABLE[row][col];
      if (item.boost) lvl = LEVELS[Math.min(rank(lvl) + item.boost, 2)];
      return lvl;
    }
    urgencyText(item) { if (item.critical) return 'critical, declared'; if (item.due == null) return 'no deadline'; const left = item.due - this.now; if (left <= 0) return `overdue by ${dur(-left)}`; return `due ${whenText(item.due, this.now)} · ${dur(left)} left of a ${dur(item.lead)} lead`; }
    urgencyShort(item) { if (item.critical) return 'critical'; if (item.due == null) return 'no deadline'; const left = item.due - this.now; if (left <= 0) return 'overdue'; return `due ${dayIndex(item.due) === dayIndex(this.now) ? 'in ' + dur(left) : whenText(item.due, this.now)}`; }
    mode(now = this.now) { return this.modes[this.manualMode || scheduledId(now)]; }
    scheduledMode(now = this.now) { return this.modes[scheduledId(now)]; }

    // ---- focus and aperture ----------------------------------------------------------
    // What is in force: the mode's preset, with the hand controls laid over it.
    focus() { return this.override.focus !== undefined ? this.override.focus : this.mode().focus; }
    aperture() { const id = this.override.aperture || this.mode().aperture; return APERTURES.find((a) => a.id === id); }
    apertureIndex(id) { return APERTURES.findIndex((a) => a.id === (id || this.aperture().id)); }
    focusText(f = this.focus()) {
      if (!f) return 'flow';
      if (f.dossier) { const d = this.byId.get(f.dossier); return `${d ? d.title : f.dossier}${f.related ? ' + related' : ''}`; }
      return f.spheres.join(', ');
    }
    // Nesting: a topic may sit within one parent topic; the focus on a dossier covers its descendants.
    parentOf(item) { return item.within ? this.byId.get(item.within) : null; }
    ancestors(item) { const out = []; let p = this.parentOf(item); while (p && !out.includes(p)) { out.push(p); p = this.parentOf(p); } return out; }
    children(id) { return this.items.filter((i) => i.within === id); }
    pendingDescendants(item) { const kids = this.children(item.id); return kids.filter((k) => k.state === 'open').length + kids.reduce((n, k) => n + this.pendingDescendants(k), 0); }
    participantsOf(item) { if (item.kind === 'relationship' || item.kind === 'group') return [item.title]; return item.participants || []; }
    // The distance of a dossier from the focus, as a ring: 0 strictly in it, 1 by
    // people (parent, siblings, a shared participant), 2 nearby (the same sphere,
    // or a sphere within it or around it), 3 everything else. No focus: 0 for all.
    sphereNear(a, b) { if (a === b) return true; return SPHERE_WITHIN[a] === b || SPHERE_WITHIN[b] === a || (SPHERE_WITHIN[a] && SPHERE_WITHIN[a] === SPHERE_WITHIN[b]); }
    distance(item, f = this.focus()) {
      if (!f) return 3;
      if (f.spheres) {
        const inSphere = (i) => f.spheres.includes(i.sphere) || i.tags.some((t) => (f.tags || []).includes(t));
        if (inSphere(item)) return 0;
        const ps = this.participantsOf(item);
        if (ps.length && this.items.some((i) => i !== item && i.state === 'open' && inSphere(i) && this.participantsOf(i).some((p) => ps.includes(p)))) return 1;
        if (f.spheres.some((s) => this.sphereNear(s, item.sphere)) || item.tags.some((t) => f.spheres.some((s) => this.sphereNear(s, t)))) return 2;
        return 3;
      }
      const root = this.byId.get(f.dossier); if (!root) return 3;
      if (item.id === root.id || this.ancestors(item).includes(root)) return 0;
      if (root.within && (item.id === root.within || item.within === root.within)) return 1;
      const ps = this.participantsOf(root).concat(...this.children(root.id).map((k) => this.participantsOf(k)));
      if (ps.length && this.participantsOf(item).some((p) => ps.includes(p))) return 1;
      if (this.sphereNear(root.sphere, item.sphere) || item.tags.some((t) => this.sphereNear(root.sphere, t)) || root.tags.some((t) => this.sphereNear(t, item.sphere))) return 2;
      return 3;
    }
    inFocus(item, f = this.focus()) { return !!f && this.distance(item, f) === 0; }
    ringName(d) { return APERTURES[Math.min(d, 3)].name; }
    hasPermit(item) { return !!item.sender && this.profile.permits[this.mode().id].includes(item.sender); }
    // Admitted = listed: within the aperture's ring, or by a permit.
    admitted(item) { return this.distance(item) <= this.aperture().ring || this.hasPermit(item); }
    admissionReason(item) {
      const mode = this.mode(); const f = this.focus(); const a = this.aperture(); const d = this.distance(item);
      if (this.level(item) === 'critical') return 'critical rings in every mode';
      if (!f) return a.ring >= 3 ? 'no focus — everything shows' : `flow — nothing is in focus, and the aperture is ${a.name}`;
      if (d === 0) return f.spheres ? `in focus — ${mode.name} covers ${f.spheres.includes(item.sphere) ? item.sphere : 'the tag ' + item.tags.find((t) => (f.tags || []).includes(t))}` : `in focus — ${item.id === f.dossier ? 'the focused dossier' : 'within ' + this.byId.get(f.dossier).title}`;
      if (this.hasPermit(item)) return `${item.sender} holds a ${mode.name} permit`;
      const how = d === 1 ? 'shares a participant with the focus' : d === 2 ? `${item.sphere} is near the focus` : 'unrelated to the focus';
      return d <= a.ring ? `${this.ringName(d)} — ${how}` : `below the aperture (${a.name}): ${how}`;
    }
    // Rings: critical always; otherwise listed and time-sensitive.
    breaksThrough(item) { const lvl = this.level(item); return lvl === 'critical' || (rank(lvl) >= rank('time-sensitive') && this.admitted(item)); }
    // Set the focus by hand for this stint: null = flow, {spheres} or {dossier, related}.
    setFocus(f) {
      this.override.focus = f; const text = this.focusText(f);
      this.emit({ kind: 'mode', text: `Focus set by hand: ${text}. It stays until the schedule changes the mode or you release it.` });
      for (const i of this.items) if (i.state === 'open' && i.actor === 'you') this._reevaluate(i, 'the focus', this.level(i));
      return true;
    }
    releaseFocus() { if (this.override.focus === undefined) return false; this.override.focus = undefined; this.emit({ kind: 'mode', text: `Focus released to the mode's preset: ${this.focusText()}.` }); return true; }
    // Set the aperture by hand. It holds until the mode changes — by schedule or by hand — and the new preset applies; nothing snaps back on its own.
    setAperture(id) {
      const before = this.aperture(); const after = APERTURES.find((a) => a.id === id); if (!after || after.id === before.id) return false;
      const widening = this.apertureIndex(after.id) > this.apertureIndex(before.id);
      this.override.aperture = after.id;
      this.emit({ kind: 'mode', text: widening ? `Aperture widened to ${after.name}; it holds until the mode changes. Looking is delivery: what shows now will not come back in a digest.` : `Aperture narrowed to ${after.name}; it holds until the mode changes.` });
      if (widening) { const shown = this.items.filter((i) => i.state === 'open' && i.actor === 'you' && !i.released && this.admitted(i) && (i.snoozedUntil == null || i.snoozedUntil <= this.now)); for (const i of shown) this._release(i, 'aperture'); if (shown.length) this.emit({ kind: 'system', text: `Shown by the wider aperture: ${shown.map((i) => i.title).join(', ')}.` }); }
      return true;
    }
    _clearOverrides(why) { if (this.override.focus === undefined && !this.override.aperture) return; this.override = { focus: undefined, aperture: null }; this.emit({ kind: 'mode', text: `${why}: focus and aperture follow the mode's preset again.` }); }
    stateText(item) {
      if (item.kind === 'standing') return item.state === 'open' ? 'ongoing · something waiting' : 'ongoing';
      if (item.kind === 'relationship' || item.kind === 'group') return item.state === 'open' ? 'pending' : 'nothing pending';
      if (item.state === 'done') return `done ${whenText(item.doneAt, this.now)}`;
      if (item.state === 'resting') return item.nextDue != null ? `resting until ${whenText(item.nextDue, this.now)}` : 'resting';
      return item.actor !== 'you' ? `waiting on ${item.actor}` : 'active';
    }
    deliveryText(item) {
      const mode = this.mode(); const lvl = this.level(item);
      if (item.state !== 'open') return this.stateText(item);
      if (item.actor !== 'you') return `waiting on ${item.actor}${item.waiting_since != null ? ` since ${dur(this.now - item.waiting_since)}` : ''}`;
      if (!item.released) { if (item.snoozedUntil != null && item.snoozedUntil > this.now) return `snoozed until ${whenText(item.snoozedUntil, this.now)}`; return `held until ${this.nextBreakpointText()} — ${this.admissionReason(item, mode)}`; }
      if (this.breaksThrough(item, mode)) return `${item.pushed.length ? `pushed ${hhmm(item.pushed[item.pushed.length - 1])}` : 'in Now'} — ${this.admissionReason(item, mode)}`;
      if (!this.admitted(item)) return `listed — ${this.admissionReason(item)}`;
      if (lvl === 'passive') return 'listed — passive never pushes';
      return `listed, ${lvl} — ${this.admissionReason(item)}; rings from time-sensitive`;
    }
    deliveryShort(item) {
      const mode = this.mode();
      if (item.state !== 'open') return this.stateText(item);
      if (item.actor !== 'you') return `waiting on ${item.actor}`;
      if (!item.released) return item.snoozedUntil != null && item.snoozedUntil > this.now ? `snoozed until ${whenText(item.snoozedUntil, this.now)}` : `held until ${this.nextBreakpointText()}`;
      if (this.breaksThrough(item, mode)) return item.pushed.length ? `pushed ${hhmm(item.pushed[item.pushed.length - 1])}` : 'may interrupt';
      if (!this.admitted(item)) return `below the aperture`;
      return this.level(item) === 'passive' ? 'listed' : (this.inFocus(item) ? 'in focus' : this.ringName(this.distance(item)));
    }
    explain(item) { return { importance: `${item.importance}/5 · ${item.importanceFrom}`, urgency: this.urgencyText(item), delivery: this.deliveryText(item) }; }

    // ---- time -------------------------------------------------------------------
    nextBreakpoint(now = this.now) {
      const c = DIGEST_TIMES.filter((x) => x > now);
      if (!this.manualMode) SCHEDULE.forEach(([start], k) => { if (start > now && (k === 0 || SCHEDULE[k - 1][1] !== 'off')) c.push(start); });
      return c.length ? Math.min(...c) : DAY + 8 * H;
    }
    nextBreakpointText() { return whenText(this.nextBreakpoint(), this.now); }
    scheduledUntil(now = this.now) { for (const [start] of SCHEDULE) if (start > now) return start; return DAY + 7 * H; }

    advance(to) { to = Math.min(to, DAY); while (this.now < to) { this.now += 1; this._minute(this.now); } }
    _minute(m) {
      const flips = scheduledId(m) !== scheduledId(m - 1);
      const schedChanged = flips && !this.manualMode;
      const digestTime = DIGEST_TIMES.includes(m);
      if (schedChanged) { this._clearOverrides('The schedule changes the mode'); const mode = this.mode(); this.emit({ kind: 'mode', text: `${mode.name}: ${mode.blurb}.` }); }
      else if (flips) this.emit({ kind: 'mode', text: `The schedule would switch to ${this.scheduledMode().name} now; the mode stays ${this.mode().name}, set by hand.` });
      const leavingOff = schedChanged && scheduledId(m - 1) === 'off';
      if (flips || schedChanged) this._trackMode();
      if ((schedChanged && !leavingOff) || digestTime) this._breakpoint(schedChanged ? `${this.mode().name} begins` : 'digest time');
      else if (m % SWEEP_EVERY === 0) this._sweep();
      for (const ev of this.eventsByMinute.get(m) || []) this._ingestEvent(ev);
    }

    // ---- delivery -------------------------------------------------------------
    _release(item, by) { item.released = true; item.releasedBy = by; item.releasedAt = this.now; item.snoozedUntil = null; }
    _push(item, urgency, why) {
      if (item.muted) { item.pushed.push(this.now); return this.emit({ kind: 'system', item, text: `${item.title} would push (${why}) — muted, so it is only listed.` }); }
      const rec = { at: this.now, kind: 'item', title: item.title, body: item.body, urgency, itemId: item.id }; this.pushes.push(rec); item.pushed.push(this.now); this.emit({ kind: 'push', item, push: rec, text: `Push (Urgency: ${urgency}) — ${item.title}: ${why}.` });
    }
    // The two flags the page bar's menu carries: starred leads every list; muted is listed, never pushed.
    star(id, on) { const item = this.byId.get(id); if (!item || !!item.starred === !!on) return false; item.starred = !!on; this.emit({ kind: 'action', item, text: `${item.title} ${on ? 'starred — it leads its lists' : 'unstarred'}.` }); return true; }
    mute(id, on) { const item = this.byId.get(id); if (!item || !!item.muted === !!on) return false; item.muted = !!on; this.emit({ kind: 'action', item, text: `${item.title} ${on ? 'muted — listed, never pushed' : 'unmuted'}.` }); return true; }
    _arrive(item, at) {
      item.arrived = at; const mode = this.mode(); const lvl = this.level(item); item.lastLevel = lvl;
      const src = item.kind === 'relationship' || item.kind === 'group' ? `${item.channel}, ${item.sender}` : item.kind === 'ara' ? `topic opened by ${item.agent}` : 'topic';
      if (item.actor !== 'you') { this._release(item, 'waiting'); return; }
      if (this.breaksThrough(item, mode)) { this._release(item, 'push'); this._push(item, 'high', `${lvl}; ${this.admissionReason(item, mode)}`); return; }
      if (lvl === 'passive') { this._release(item, 'listed'); return this.emit({ kind: 'arrive', item, text: `${item.title} (${src}): importance ${item.importance}, ${this.urgencyText(item)} → passive. Listed, never pushed.` }); }
      if (this.admitted(item)) { this._release(item, 'listed'); return this.emit({ kind: 'arrive', item, text: `${item.title} (${src}): ${lvl} — ${this.admissionReason(item)} → listed at once, no ring below time-sensitive.` }); }
      this.emit({ kind: 'arrive', item, text: `${item.title} (${src}): ${lvl} — ${this.admissionReason(item, mode)} → held for the ${this.nextBreakpointText()} breakpoint.` });
    }
    _breakpoint(reason) {
      const m = this.now, mode = this.mode();
      if (mode.id !== 'off') this._batch(reason);
      const due = this.items.filter((i) => i.state === 'open' && !i.released && i.actor === 'you' && (i.snoozedUntil == null || i.snoozedUntil <= m));
      const s = (n) => (n === 1 ? '' : 's');
      if (mode.id === 'off') { if (due.length) this.emit({ kind: 'digest', text: `${reason}: Off has no digest — ${due.length} held dossier${s(due.length)} wait for the next breakpoint outside Off.` }); return; }
      if (!due.length) return this.emit({ kind: 'digest', text: `${reason}: nothing was held, so no digest.` });
      for (const i of due) this._release(i, 'digest');
      const rec = { at: m, kind: 'digest', title: `Digest ${hhmm(m)}`, body: due.map((i) => `${i.title} (${this.level(i)})`).join(' · '), urgency: 'normal', topic: 'digest', itemIds: due.map((i) => i.id) };
      this.pushes.push(rec); this.digests.push(rec);
      this.emit({ kind: 'digest', push: rec, text: `${reason}: one digest push (Topic-collapsed, Urgency: normal) with ${due.length} dossier${s(due.length)} — ${due.map((i) => i.title).join(', ')}.` });
    }
    _sweep() {
      const mode = this.mode();
      for (const item of this.items) {
        if (item.state !== 'open' || item.actor !== 'you') continue;
        const lvl = this.level(item); const rose = item.lastLevel != null && rank(lvl) > rank(item.lastLevel);
        if (!item.released) {
          if (item.snoozedUntil != null && item.snoozedUntil > this.now) { item.lastLevel = lvl; continue; }
          if (this.breaksThrough(item, mode)) { this._release(item, 'sweep'); this._push(item, 'high', rose ? `the half-hourly sweep found it in the next urgency band (${item.lastLevel} → ${lvl})` : `the sweep found it admitted now (${this.admissionReason(item, mode)})`); }
          else if (rose) this.emit({ kind: 'sweep', item, text: `${item.title} climbs to ${lvl} (urgency crossed a band) — still held: ${this.admissionReason(item, mode)}.` });
        } else if (rose) {
          if (this.breaksThrough(item, mode) && !item.pushed.length) this._push(item, 'high', `the sweep found it in the next urgency band (${item.lastLevel} → ${lvl}); ${this.admissionReason(item, mode)}`);
          else this.emit({ kind: 'sweep', item, text: `${item.title} climbs to ${lvl}.` });
        }
        item.lastLevel = lvl;
      }
    }
    _reevaluate(item, why, before) {
      if (item.state !== 'open') return; const mode = this.mode(); const lvl = this.level(item);
      if (!item.released && (item.snoozedUntil == null || item.snoozedUntil <= this.now)) {
        if (this.breaksThrough(item, mode)) { this._release(item, why); this._push(item, 'high', `after ${why}: ${lvl}; ${this.admissionReason(item, mode)}`); }
        else this.emit({ kind: 'system', item, text: `${item.title}: ${lvl}, ${this.admissionReason(item, mode)} — still held.` });
      } else if (item.released) {
        if (this.breaksThrough(item, mode) && !item.pushed.length && rank(lvl) >= 2) this._push(item, 'high', `after ${why}: ${lvl}; ${this.admissionReason(item, mode)}`);
        else if (before && before !== lvl) this.emit({ kind: 'system', item, text: `${item.title}: ${before} → ${lvl} after ${why}.` });
      }
      item.lastLevel = lvl;
    }

    // ---- the stream: Ara's turns, replies, references ---------------------------------
    _dialogue(item) {
      const D = root.Dialogues;
      if (item.kind === 'relationship' || item.kind === 'group') { const c = D.RELATIONSHIPS[item.id]; if (!c) return null; const ep = item.episode; return ep <= 1 ? c : (c[`later${ep === 2 ? '' : ep - 1}`] || c.later3 || c); }
      return D.TOPICS[item.id] || null;
    }
    lastIn(item) { return [...item.stream].reverse().find((m) => m.who === 'in'); }
    _fill(text, item) { const last = this.lastIn(item); return text.replace('{time}', hhmm(this.now)).replace('{arrived}', hhmm(item.arrived != null ? item.arrived : this.now)).replace('{last}', last ? last.text : ''); }
    // The one-line description a dossier row shows; Ara's opening restates it.
    preview(item) {
      const d = this._dialogue(item);
      if (item.kind === 'standing') return item.body || '';
      if (item.state === 'open' && d && d.summary) return this._fill(d.summary, item);
      const last = [...item.stream].reverse().find((m) => m.who === 'in' || m.who === 'out' || m.who === 'event' || m.who === 'ara');
      if (!last) return item.body || '';
      if (last.who === 'in') return `${last.from ? last.from + ': ' : ''}${last.mail ? last.subject : last.text}`;
      if (last.who === 'out') return `you: ${last.mail && last.subject ? last.subject : last.text}`;
      return last.text || item.body || '';
    }
    ensureOpening(id) {
      const item = this.byId.get(id); if (!item || item.opened) return item;
      item.opened = true; const d = this._dialogue(item);
      if (item.kind === 'ara' && item.stream.length && !item.chips) { item.chips = d && d.chips ? d.chips.slice() : []; return item; }
      if (item.kind === 'standing') { if (!item.stream.length) item.stream.push({ who: 'ara', text: item.body, t: this.now }); if (!item.chips) item.chips = []; return item; }
      if (item.kind === 'topic' && item.state !== 'open') { item.chips = []; return item; }
      const opening = d && d.opening ? d.opening : (item.kind === 'relationship' || item.kind === 'group' ? null : item.body);
      if (opening) item.stream.push({ who: 'ara', text: this._fill(opening, item), t: item.kind === 'topic' && item.arrived != null && item.arrived > 0 ? item.arrived : this.now, refs: d && d.embed ? d.embed.map((id) => this.ref(id)).filter((x) => this.byId.has(x.id)) : [] });
      item.chips = d && d.chips ? d.chips.slice() : [];
      return item;
    }
    // The user's turn to Ara: a chip label or free text. Returns Ara's reply record.
    say(id, text, attachments = []) {
      const item = this.ensureOpening(id); if (!item) return null;
      item.stream.push({ who: 'you', text, t: this.now, attachments }); this._touch(item);
      const d = this._dialogue(item) || (item.kind === 'standing' ? root.Dialogues.STANDING[item.id] : null);
      const inboxReply = item.kind === 'standing' ? this._inboxReply(item, text) : null;
      const filed = attachments.length ? `Filed ${attachments.map((a) => a.name).join(', ')} with this dossier.` : '';
      const r = inboxReply || (d && d.replies && d.replies[text]) || (text ? (d && d.free) || { text: 'Noted. I keep this with the dossier.' } : { text: filed || 'Noted.' });
      const reply = { who: 'ara', text: (text && filed ? filed + ' ' : '') + this._fill(r.text, item), t: this.now, refs: r.embed ? r.embed.map((id) => this.ref(id)).filter((x) => this.byId.has(x.id)) : [] };
      item.stream.push(reply); if (!inboxReply) item.chips = r.chips ? r.chips.slice() : (r.done || r.later || r.waitOn ? [] : item.chips);
      const short = (x) => (x.length > 70 ? x.slice(0, 67) + '…' : x);
      this.emit({ kind: 'action', item, text: `${item.title} — you, to Ara: “${short(text)}”${attachments.length ? ` + ${attachments.length} file${attachments.length === 1 ? '' : 's'}` : ''} · Ara: “${short(reply.text)}”` });
      if (r.note) this.emit({ kind: 'system', item, text: `(${r.note})` });
      if (r.waitOn) { item.actor = r.waitOn; item.waiting_since = this.now; item.released = true; item.snoozedUntil = null; this.emit({ kind: 'action', item, text: `${item.title} is parked on ${r.waitOn}.` }); }
      if (r.later) { if (item.kind === 'topic' && r.later === 'tomorrow') this.rest(id, DAY + 8 * H); else this.later(id, r.later); }
      if (r.done) this.doIt(id);
      return { reply, draft: r.draft ? this._fill(r.draft, item) : null, draftTo: r.draftTo || null };
    }
    // Inbox Zero's chips act on the mails they propose for.
    _inboxReply(iz, label) {
      const D = root.Dialogues.INBOX; let hit = null;
      for (const sender of Object.keys(D)) { const p = D[sender]; if (p.replies && p.replies[label]) { hit = { sender, r: p.replies[label] }; break; } }
      if (!hit) return null;
      const r = { ...hit.r }; const x = iz.inbox.find((y) => y.item.sender === hit.sender && y.entry.inbox);
      if (r.resolve && x) { this._settle(x.item, 'done'); this._leaveInbox(x.entry, r.resolve); }
      if (r.dossier && x) {
        const spec = r.dossier; const topic = this._add(this._newItem({ id: spec.id, kind: 'topic', title: spec.title, body: spec.body || '', sphere: x.item.sphere, tags: x.item.tags.slice(), importance: x.item.importance, importanceFrom: 'the batch', due: x.item.due, kindLabel: x.item.kindLabel, lead: x.item.lead, leadFrom: x.item.leadFrom, participants: [x.item.title] }));
        topic.stream.push({ who: 'event', author: 'you', kind: 'note', t: this.now, text: `Opened from Inbox Zero.`, comment: spec.comment || '', messages: [this._quote(x.item, x.entry)], links: [] });
        x.entry.links.push({ id: topic.id, title: topic.title });
        this._settle(x.item, 'handed over'); this._leaveInbox(x.entry, `referenced in “${topic.title}”`);
        this._arrive(topic, this.now);
        this.emit({ kind: 'action', item: topic, text: `New dossier “${topic.title}” with the mail from ${x.item.title} as its first event; the mail shows the backlink.` });
        iz.stream.push({ who: 'event', author: 'agent:Ara', kind: 'status', t: this.now, text: `Opened a dossier for it:`, refs: [this.ref(topic.id)], links: [] });
      }
      if (iz.chips) iz.chips = iz.chips.filter((c) => !(D[hit.sender].chips || []).includes(c) || (r.keep && r.keep.includes(c)));
      if (r.chips) iz.chips = [...new Set([...(iz.chips || []), ...r.chips])];
      return r;
    }
    // A reference is an address, never a copy: the renderer embeds the object's own block, live.
    _quote(item, entry) { return { type: 'message', dossier: item.id, uid: entry.uid }; }
    ref(id) { return { type: 'dossier', id }; }
    resolve(r) { if (!r) return null; if (r.type === 'dossier') { const d = this.byId.get(r.id); return d ? { dossier: d } : null; } const d = this.byId.get(r.dossier); const m = d && d.stream.find((x) => x.uid === r.uid); return m ? { dossier: d, message: m } : null; }
    // A relationship's pending state is settled; the dossier stays.
    _settle(item, how) { if (item.state !== 'open') return; item.state = item.kind === 'relationship' || item.kind === 'group' ? 'quiet' : 'done'; item.doneAt = this.now; item.doneHow = how; this.doneToday += 1; }
    // Send to the dossier's human participants (a relationship's channel); the pending state is settled.
    sendChat(id, text, attachments = []) {
      const item = this.byId.get(id); if (!item || !(item.kind === 'relationship' || item.kind === 'group')) return false;
      const last = this.lastIn(item);
      item.stream.push({ who: 'out', text, t: this.now, attachments, mail: !!item.mail, subject: item.mail && last ? `Re: ${last.subject}` : null, links: [] }); this._touch(item);
      for (const m of item.stream) if (m.mail && m.inbox) this._leaveInbox(m, 'replied');
      this._settle(item, 'replied');
      this.emit({ kind: 'action', item, text: `Replied to ${item.sender} on ${item.channel}: “${text}”${attachments.length ? ` + ${attachments.length} file${attachments.length === 1 ? '' : 's'}` : ''} — nothing pending.` });
      return true;
    }
    // The gesture: messages of one dossier referenced from another, as one event with a comment.
    reference(srcId, uids, target, comment) {
      const src = this.byId.get(srcId); if (!src) return null;
      const entries = src.stream.filter((m) => uids.includes(m.uid)); if (!entries.length) return null;
      let tgt = typeof target === 'string' ? this.byId.get(target) : null;
      if (!tgt && target && target.title) {
        const parent = target.within ? this.byId.get(target.within) : null;
        tgt = this._add(this._newItem({ id: `top-${++this.seq}`, kind: 'topic', title: target.title, body: comment || '', sphere: parent ? parent.sphere : src.sphere, tags: [], importance: parent ? parent.importance : src.importance, importanceFrom: parent ? 'inherited' : 'prior', due: null, kindLabel: 'admin chore', participants: [src.title], within: parent ? parent.id : null }));
        if (parent) { parent.stream.push({ who: 'event', author: 'you', kind: 'status', t: this.now, text: 'Opened a sub-dossier:', refs: [this.ref(tgt.id)], links: [] }); this._touch(parent); }
        this._arrive(tgt, this.now);
      }
      if (!tgt) return null;
      const ev = { who: 'event', author: 'you', kind: 'note', t: this.now, text: '', comment: comment || '', messages: entries.map((e) => this._quote(src, e)), links: [] };
      tgt.stream.push(ev); this._touch(tgt);
      for (const e of entries) { e.links.push({ id: tgt.id, title: tgt.title }); if (e.mail && e.inbox) this._leaveInbox(e, `referenced in “${tgt.title}”`); }
      this.emit({ kind: 'action', item: tgt, text: `${entries.length} message${entries.length === 1 ? '' : 's'} from ${src.title} added to “${tgt.title}”${comment ? ` — “${comment}”` : ''}; the messages show the backlink.` });
      return tgt;
    }
    note(id, text) { const item = this.byId.get(id); if (!item) return false; item.stream.push({ who: 'event', author: 'you', kind: 'note', t: this.now, text: '', comment: text, messages: [], links: [] }); this._touch(item); this.emit({ kind: 'action', item, text: `Note in ${item.title}: “${text}”` }); return true; }

    // ---- what the user can do ----------------------------------------------------
    doIt(id) { const item = this.byId.get(id); if (!item || item.state !== 'open' || item.kind === 'standing') return false; for (const m of item.stream) if (m.mail && m.inbox) this._leaveInbox(m, 'marked done'); if (item.recurring) { item.doneToday = true; this.doneToday += 1; item.state = 'resting'; item.resting = true; item.due = null; item.nextDue = item.nextDue != null ? item.nextDue + 30 * DAY : null; this.emit({ kind: 'action', item, text: `${item.title}: done for this month; the cadence rests until ${whenText(item.nextDue, this.now)}.` }); return true; } this._settle(item, 'done'); this.emit({ kind: 'action', item, text: `${item.kind === 'topic' || item.kind === 'ara' ? 'Done' : 'Nothing pending'}: ${item.title}.` }); return true; }
    rest(id, until) { const item = this.byId.get(id); if (!item || item.kind !== 'topic' || item.state !== 'open') return false; item.state = 'resting'; item.resting = true; item.nextDue = until != null ? until : null; item.due = null; this.emit({ kind: 'action', item, text: `${item.title} rests${until != null ? ` until ${whenText(until, this.now)}` : ''}.` }); return true; }
    reopen(id) { const item = this.byId.get(id); if (!item || item.state === 'open') return false; item.state = 'open'; item.resting = false; item.released = false; this.emit({ kind: 'action', item, text: `${item.title} is active again.` }); this._arrive(item, this.now); return true; }
    later(id, when) { const item = this.byId.get(id); if (!item || item.state !== 'open') return false; const until = when === 'tomorrow' ? DAY + 8 * H : this.nextBreakpoint(); item.released = false; item.releasedBy = null; item.snoozedUntil = until; this.emit({ kind: 'action', item, text: `Later: ${item.title} waits until ${whenText(until, this.now)}.` }); return true; }
    pull(id) { const item = this.byId.get(id); if (!item || item.state !== 'open' || item.released) return false; this._release(item, 'you'); this.emit({ kind: 'action', item, text: `You pull ${item.title} into Now ahead of the digest.` }); return true; }
    setManualMode(id) {
      const before = this.mode(); this.manualMode = id; const after = this.mode(); this._trackMode(); this._clearOverrides('The mode changes by hand');
      this.emit({ kind: 'mode', text: id ? `Mode set by hand: ${after.name} — ${after.blurb}. The schedule is suspended until you release it.` : `Mode released to the schedule: ${after.name} — ${after.blurb}.` });
      if (after.id !== before.id) this._breakpoint(`mode ${before.name} → ${after.name}`);
      return true;
    }
    correct(id, patch) {
      const item = this.byId.get(id); if (!item) return false; const before = this.level(item); this.corrections += 1;
      if (patch.importance != null) { item.importance = patch.importance; item.importanceFrom = 'you'; const key = item.sender || item.kindLabel; this.profile.priors[key] = patch.importance; this._learn(`importance prior for ${key} → ${patch.importance}`); }
      if (patch.lead != null) { item.lead = patch.lead; item.leadFrom = 'you'; this.profile.leads[item.kindLabel] = patch.lead; this._learn(`lead time for “${item.kindLabel}” → ${dur(patch.lead)}`); }
      if (patch.due !== undefined) { item.due = patch.due; this.emit({ kind: 'action', item, text: `${item.title}: deadline set to ${patch.due == null ? 'none' : whenText(patch.due, this.now)}.` }); }
      this._reevaluate(item, 'your correction', before); return true;
    }
    permit(sender, modeId, on) {
      const list = this.profile.permits[modeId]; const has = list.includes(sender); if (on === has) return false; this.corrections += 1;
      if (on) list.push(sender); else list.splice(list.indexOf(sender), 1);
      this._learn(`${sender} ${on ? 'may interrupt' : 'may no longer interrupt'} in ${this.modes[modeId].name}`);
      for (const i of this.items) if (i.sender === sender && i.state === 'open') this._reevaluate(i, 'the permit', this.level(i));
      return true;
    }
    // The Focus rule on a mode's preset: a sphere joins or leaves the mode's focus.
    admitSphere(sphere, modeId, on) {
      const mode = this.modes[modeId]; if (!mode.focus || !mode.focus.spheres) mode.focus = { spheres: [], tags: [] };
      const has = mode.focus.spheres.includes(sphere); if (on === has) return false; this.corrections += 1;
      if (on) mode.focus.spheres.push(sphere); else mode.focus.spheres.splice(mode.focus.spheres.indexOf(sphere), 1);
      if (!mode.focus.spheres.length && !mode.focus.tags.length) mode.focus = null;
      this._learn(`${mode.name}'s focus ${on ? 'now covers' : 'no longer covers'} ${sphere}`);
      for (const i of this.items) if (i.sphere === sphere && i.state === 'open') this._reevaluate(i, 'the Focus rule', this.level(i));
      return true;
    }
    _learn(text) { this.profile.learned.push({ at: this.now, text }); this.emit({ kind: 'learn', text: `Profile: ${text}.` }); }

    // ---- views --------------------------------------------------------------------
    // Home: dossiers with something pending — Now · In focus · Outside the focus (above the aperture) · below the aperture (a count) · Waiting.
    home() {
      const mode = this.mode(); const f = this.focus(); const open = this.items.filter((i) => i.state === 'open');
      const now = [], focus = [], outside = [], below = [], waiting = [];
      for (const i of open) { if (i.actor !== 'you') waiting.push(i); else if (i.released && this.breaksThrough(i)) now.push(i); else if (f && this.inFocus(i)) focus.push(i); else if (this.admitted(i)) outside.push(i); else below.push(i); }
      const key = (i) => [this.distance(i), -rank(this.level(i)), -i.importance, i.due == null ? Infinity : i.due - this.now];
      const cmp = (a, b) => { const ka = key(a), kb = key(b); for (let k = 0; k < 4; k++) if (ka[k] !== kb[k]) return ka[k] - kb[k]; return a.title.localeCompare(b.title); };
      now.sort(cmp); focus.sort(cmp); outside.sort(cmp); below.sort(cmp); waiting.sort((a, b) => (a.waiting_since || 0) - (b.waiting_since || 0));
      for (const l of [now, focus, outside, below, waiting]) l.sort((a, b) => (b.starred ? 1 : 0) - (a.starred ? 1 : 0));
      const belowUrgent = below.filter((i) => rank(this.level(i)) >= rank('time-sensitive')).length;
      // The old names, for the deck and the replay: active = listed, other = below the aperture.
      return { now, focus, outside, below, belowUrgent, active: focus.concat(outside), other: below, waiting, mode, focusText: this.focusText(f), aperture: this.aperture() };
    }
    _starredFirst(list) { return list.sort((a, b) => ((b.starred ? 1 : 0) - (a.starred ? 1 : 0)) || (b.lastActivity - a.lastActivity)); }
    sections() { const h = this.home(); return { now: h.now, next: h.active.concat(h.other).filter((i) => i.released), held: h.active.concat(h.other).filter((i) => !i.released), waiting: h.waiting, mode: h.mode }; }
    // Contacts: every relationship, by latest message.
    contacts() { return this._starredFirst(this.items.filter((i) => i.kind === 'relationship' || i.kind === 'group')); }
    // Dossiers: topics, Ara topics and standing ones, by state.
    dossiers(filter = 'active') {
      const all = this.items.filter((i) => i.kind === 'topic' || i.kind === 'ara' || i.kind === 'standing');
      const pick = filter === 'resting' ? all.filter((i) => i.state === 'resting') : filter === 'done' ? all.filter((i) => i.state === 'done') : all.filter((i) => i.state === 'open' || i.state === 'quiet');
      return this._starredFirst(pick);
    }
    // The Dossiers page as a tree: roots of the filter, each with its children (whatever their state) nested.
    dossierTree(filter = 'active') {
      const picked = this.dossiers(filter); const ids = new Set(picked.map((i) => i.id));
      const roots = picked.filter((i) => !i.within || !ids.has(i.within));
      const node = (i, depth) => ({ item: i, depth, children: this.children(i.id).filter((k) => k.state !== 'done' || filter === 'done').sort((a, b) => b.lastActivity - a.lastActivity).map((k) => node(k, depth + 1)) });
      return roots.map((r) => node(r, 0));
    }
    kindLabel(item) { return KIND_LABEL[item.kind] || item.kind; }
    stats() {
      const byUrgency = { high: 0, normal: 0 }; for (const p of this.pushes) byUrgency[p.urgency] = (byUrgency[p.urgency] || 0) + 1;
      const h = this.home();
      return { pushes: this.pushes.length, itemPushes: byUrgency.high, digests: this.digests.length, now: h.now.length, active: h.active.length + h.other.length, held: h.active.concat(h.other).filter((i) => !i.released).length, waiting: h.waiting.length, inbox: this.inboxCount(), done: this.doneToday, corrections: this.corrections };
    }
  }

  root.DossierEngine = DossierEngine;
  root.AttentionEngine = DossierEngine;
  root.AttentionUtil = { hhmm, whenText, dateText, dur, dayIndex, rank, LEVELS, MODES, APERTURES, SCHEDULE, DIGEST_TIMES, scheduledId, DAY, H };
})(globalThis);
