// The attention engine: the mechanism described in docs/attention-model.md,
// running on the example backends. Pure logic, no DOM — the same file runs in
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
  function whenText(m, now) { const d = dayIndex(m), dn = dayIndex(now); if (d === dn) return hhmm(m); if (d === dn + 1) return `tomorrow ${hhmm(m)}`; if (d - dn < 7) return `${DAYNAMES[((d % 7) + 7) % 7]} ${hhmm(m)}`; return dateText(m); }
  const nice = (v) => (Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));
  function dur(min) { min = Math.round(min); if (min < 60) return `${min} min`; if (min < DAY) return `${nice(min / 60)} h`; return `${nice(min / DAY)} d`; }

  const MODES = {
    off:    { id: 'off',    name: 'Off',       admits: [],                                                    admitTags: [],         threshold: 'critical',       color: '#3a4250', blurb: 'only critical; the digest waits for the morning' },
    home:   { id: 'home',   name: 'Home',      admits: ['family', 'health'],                                  admitTags: [],         threshold: 'time-sensitive', color: '#a86f2c', blurb: 'family and health may break through' },
    deep:   { id: 'deep',   name: 'Deep work', admits: [],                                                    admitTags: [],         threshold: 'critical',       color: '#0f4f57', blurb: 'only critical breaks through' },
    open:   { id: 'open',   name: 'Open',      admits: ['customers', 'admin', 'health', 'friends', 'family', 'system'], admitTags: [], threshold: 'time-sensitive', color: '#8a94a0', blurb: 'every sphere admitted; time-sensitive rings' },
    work:   { id: 'work',   name: 'Work',      admits: ['customers', 'admin', 'health'],                      admitTags: ['health'], threshold: 'time-sensitive', color: '#2f8a90', blurb: 'customers, admin and health may break through' },
    social: { id: 'social', name: 'Social',    admits: ['friends', 'family'],                                 admitTags: ['health'], threshold: 'time-sensitive', color: '#7a4f96', blurb: 'friends and family may break through' },
  };
  const SCHEDULE = [[0, 'off'], [7 * H, 'home'], [8 * H, 'deep'], [12 * H, 'open'], [13 * H, 'work'], [17 * H, 'open'], [18 * H, 'social'], [22 * H, 'off']];
  const DIGEST_TIMES = [8 * H, 12 * H, 17 * H, 21 * H];
  const SWEEP_EVERY = 30;
  const scheduledId = (m) => { const x = ((m % DAY) + DAY) % DAY; let id = 'off'; for (const [start, mid] of SCHEDULE) if (x >= start) id = mid; return id; };
  const LEVEL_TABLE = [['active', 'time-sensitive', 'time-sensitive'], ['passive', 'active', 'active'], ['passive', 'passive', 'active']];
  const clone = (o) => JSON.parse(JSON.stringify(o));

  class AttentionEngine {
    constructor() { this.listeners = []; this.reset(); }

    on(fn) { this.listeners.push(fn); return this; }
    emit(ev) { if (this.quiet) return; ev.t = this.now; this.version += 1; this.feed.push(ev); for (const fn of this.listeners) fn(ev); }

    reset() {
      this.now = 0; this.items = []; this.byId = new Map(); this.feed = []; this.pushes = []; this.digests = [];
      this.manualMode = null; this.doneToday = 0; this.corrections = 0; this.modeHistory = []; this.version = 0;
      this.modes = clone(MODES);
      this.profile = { priors: { ...B.PRIORS }, leads: { ...B.LEAD_DEFAULTS }, permits: { off: [], home: [], deep: [], open: [], work: [], social: [] }, learned: [] };
      this.eventsByMinute = new Map();
      for (const be of B.all) for (const ev of be.events(0, DAY)) { const m = Math.round(ev.at); if (!this.eventsByMinute.has(m)) this.eventsByMinute.set(m, []); this.eventsByMinute.get(m).push(ev); }
      // Yesterday's arrivals and the live projects: already seen, already released.
      this.quiet = true;
      for (const be of B.all) for (const ev of be.events(-Infinity, 0)) this._ingestEvent(ev);
      for (const p of B.projects.initial()) this._ingestEvent({ type: 'project', at: -DAY, ...p });
      for (const i of this.items) { if (i.state === 'open' && !i.released) this._release(i, 'yesterday'); i.lastLevel = this.level(i); }
      this.quiet = false; this._trackMode();
    }
    _trackMode() { const id = this.mode().id; const last = this.modeHistory[this.modeHistory.length - 1]; if (!last || last.id !== id) this.modeHistory.push({ from: this.now, id }); }

    // ---- items --------------------------------------------------------------
    _leadFor(kind) { const v = this.profile.leads[kind]; return v != null ? v : this.profile.leads.default; }
    _newItem(o) {
      const item = Object.assign({ tags: [], count: 1, state: 'open', released: false, releasedBy: null, releasedAt: null, snoozedUntil: null, boost: 0, pushed: [], lastLevel: null, due: null, critical: false, actor: 'you', thread: [], chips: null, chat: [], episode: 0 }, o);
      if (item.lead == null) { item.lead = this._leadFor(item.kindLabel); item.leadFrom = 'kind default'; }
      return item;
    }
    _add(item) { this.items.push(item); this.byId.set(item.id, item); return item; }
    _ingestEvent(ev) {
      if (ev.type === 'message') return this._onMessage(ev);
      if (ev.type === 'thread') { const c = this._add(this._newItem({ id: ev.id, kind: 'thread', source: '/conversations', agent: ev.agent, title: ev.title, body: ev.body, sphere: ev.sphere, tags: ev.tags || [], importance: ev.importance, importanceFrom: ev.agent, due: ev.due != null ? ev.due : null, critical: !!ev.critical, kindLabel: ev.kind })); return this._arrive(c, ev.at); }
      if (ev.type === 'wake' || ev.type === 'project') {
        const lead = ev.remind_before != null ? ev.remind_before : null;
        const p = this._add(this._newItem({ id: ev.id, kind: 'project', source: '/projects', title: ev.title, body: ev.type === 'wake' ? `Woken by recurring-projects: ${ev.recurring} cadence, due ${hhmm(ev.next_due)}.` : (ev.current_actor === 'you' ? 'Your move.' : `Parked on ${ev.current_actor}.`), sphere: ev.sphere, tags: ev.tags || [], importance: ev.importance, importanceFrom: 'frontmatter', due: ev.expected_by != null ? ev.expected_by : (ev.next_due != null ? ev.next_due : null), lead, leadFrom: lead != null ? 'remind_before' : undefined, kindLabel: ev.kind, actor: ev.current_actor, waiting_since: ev.waiting_since != null ? ev.waiting_since : null }));
        if (p.lead == null) { p.lead = this._leadFor(p.kindLabel); p.leadFrom = 'kind default'; }
        return this._arrive(p, ev.at);
      }
    }
    _chatItem(msg) {
      const c = B.CONTACTS[msg.sender] || {}; const tr = msg.triage || {}; const prior = this.profile.priors[msg.sender];
      return this._newItem({ id: `chat:${msg.chat}`, kind: 'chat', source: '/chats', channel: c.channel || 'messenger', title: msg.chat, body: msg.text, sender: msg.sender, sphere: c.sphere || 'friends', tags: tr.tags || [], importance: tr.importance != null ? tr.importance : (prior != null ? prior : 2.5), importanceFrom: tr.importance != null ? 'triage' : 'prior', due: tr.due != null ? tr.due : null, kindLabel: tr.kind || 'message' });
    }
    _onMessage(msg) {
      const id = `chat:${msg.chat}`; let item = this.byId.get(id); const tr = msg.triage || {};
      if (!item) { item = this._add(this._chatItem(msg)); item.chat.push({ dir: 'in', text: msg.text, t: msg.at }); return this._arrive(item, msg.at); }
      item.chat.push({ dir: 'in', text: msg.text, t: msg.at });
      if (item.state === 'done') {
        Object.assign(item, { state: 'open', released: false, releasedBy: null, snoozedUntil: null, boost: 0, count: 1, pushed: [], body: msg.text, kindLabel: tr.kind || item.kindLabel, tags: tr.tags || [], due: tr.due != null ? tr.due : null, thread: [], chips: null, episode: item.episode + 1 });
        if (tr.importance != null) { item.importance = tr.importance; item.importanceFrom = 'triage'; } else { item.importance = this.profile.priors[msg.sender] != null ? this.profile.priors[msg.sender] : item.importance; item.importanceFrom = 'prior'; }
        item.lead = this._leadFor(item.kindLabel); item.leadFrom = 'kind default';
        return this._arrive(item, msg.at);
      }
      const prevKind = item.kindLabel, prevDue = item.due;
      item.count += 1; item.body = msg.text; item.arrived = msg.at;
      if (tr.importance != null && tr.importance > item.importance) { item.importance = tr.importance; item.importanceFrom = 'triage'; }
      if (tr.due != null) item.due = tr.due;
      const changed = (tr.kind && tr.kind !== prevKind) || (tr.due != null && prevDue == null);
      const policy = this._repeatPolicy(item);
      if (item.released) return this.emit({ kind: 'arrive', item, text: `${item.sender} again (${item.count} messages) — already in your list.` });
      if (policy.escalate || changed) { item.boost = Math.min(item.boost + 1, 2); this.emit({ kind: 'system', item, text: `${item.sender} writes again: ${policy.escalate ? policy.reason : 'the classification changed'} — one level up.` }); return this._reevaluate(item, 'the repeat'); }
      this.emit({ kind: 'arrive', item, text: `${item.sender} writes again (${item.count} messages). The repeat policy for ${item.sphere} is off and the classification is unchanged: nothing escalates; ${this.deliveryText(item)}.` });
    }
    _repeatPolicy(item) { const mode = this.mode(); if (item.sphere === 'family' && mode.id === 'off') return { escalate: true, reason: 'a family repeat breaks through in Off (the repeated-caller rule)' }; return { escalate: false }; }

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
    admitted(item, mode) { return mode.admits.includes(item.sphere) || item.tags.some((t) => mode.admitTags.includes(t)) || (!!item.sender && this.profile.permits[mode.id].includes(item.sender)); }
    admissionReason(item, mode) {
      if (this.level(item) === 'critical') return 'critical rings in every mode';
      if (mode.admits.includes(item.sphere)) return `${mode.name} admits ${item.sphere}`;
      const tag = item.tags.find((t) => mode.admitTags.includes(t)); if (tag) return `${mode.name} admits the tag ${tag}`;
      if (item.sender && this.profile.permits[mode.id].includes(item.sender)) return `${item.sender} holds a ${mode.name} permit`;
      if (mode.threshold === 'critical') return `${mode.name} admits only critical`;
      return `${mode.name} does not admit ${item.sphere}`;
    }
    breaksThrough(item, mode) { const lvl = this.level(item); return lvl === 'critical' || (rank(lvl) >= rank(mode.threshold) && this.admitted(item, mode)); }
    deliveryText(item) {
      const mode = this.mode(); const lvl = this.level(item);
      if (item.state === 'done') return `done ${hhmm(item.doneAt)}`;
      if (item.actor !== 'you') return `waiting on ${item.actor}${item.waiting_since != null ? ` since ${dur(this.now - item.waiting_since)}` : ''}`;
      if (!item.released) { if (item.snoozedUntil != null && item.snoozedUntil > this.now) return `snoozed until ${whenText(item.snoozedUntil, this.now)}`; return `held until ${this.nextBreakpointText()} — ${this.admissionReason(item, mode)}`; }
      if (this.breaksThrough(item, mode)) return `${item.pushed.length ? `pushed ${hhmm(item.pushed[item.pushed.length - 1])}` : 'in Now'} — ${this.admissionReason(item, mode)}`;
      if (lvl === 'passive') return 'listed — passive never pushes';
      return `in Next — ${rank(lvl) < rank(mode.threshold) ? `${lvl} is below ${mode.name}’s bar` : `${lvl}, but ${this.admissionReason(item, mode)}`}`;
    }
    deliveryShort(item) {
      const mode = this.mode();
      if (item.actor !== 'you') return `waiting on ${item.actor}`;
      if (!item.released) return item.snoozedUntil != null && item.snoozedUntil > this.now ? `snoozed until ${whenText(item.snoozedUntil, this.now)}` : `held until ${this.nextBreakpointText()}`;
      if (this.breaksThrough(item, mode)) return item.pushed.length ? `pushed ${hhmm(item.pushed[item.pushed.length - 1])}` : 'may interrupt';
      return this.level(item) === 'passive' ? 'listed' : (this.admitted(item, mode) ? 'next' : `${mode.name} holds ${item.sphere}`);
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
      if (schedChanged) { const mode = this.mode(); this.emit({ kind: 'mode', text: `${mode.name}: ${mode.blurb}.` }); }
      else if (flips) this.emit({ kind: 'mode', text: `The schedule would switch to ${this.scheduledMode().name} now; the mode stays ${this.mode().name}, set by hand.` });
      const leavingOff = schedChanged && scheduledId(m - 1) === 'off';
      if (flips || schedChanged) this._trackMode();
      if ((schedChanged && !leavingOff) || digestTime) this._breakpoint(schedChanged ? `${this.mode().name} begins` : 'digest time');
      else if (m % SWEEP_EVERY === 0) this._sweep();
      for (const ev of this.eventsByMinute.get(m) || []) this._ingestEvent(ev);
    }

    // ---- delivery -------------------------------------------------------------
    _release(item, by) { item.released = true; item.releasedBy = by; item.releasedAt = this.now; item.snoozedUntil = null; }
    _push(item, urgency, why) { const rec = { at: this.now, kind: 'item', title: item.title, body: item.body, urgency, itemId: item.id }; this.pushes.push(rec); item.pushed.push(this.now); this.emit({ kind: 'push', item, push: rec, text: `Push (Urgency: ${urgency}) — ${item.title}: ${why}.` }); }
    _arrive(item, at) {
      item.arrived = at; const mode = this.mode(); const lvl = this.level(item); item.lastLevel = lvl;
      const src = item.kind === 'chat' ? `${item.channel}, ${item.sender}` : item.kind === 'thread' ? `thread opened by ${item.agent}` : 'project';
      if (item.actor !== 'you') { this._release(item, 'waiting'); return; }
      if (this.breaksThrough(item, mode)) { this._release(item, 'push'); this._push(item, 'high', `${lvl}; ${this.admissionReason(item, mode)}`); return; }
      if (lvl === 'passive') { this._release(item, 'listed'); return this.emit({ kind: 'arrive', item, text: `${item.title} (${src}): importance ${item.importance}, ${this.urgencyText(item)} → passive. Listed, never pushed.` }); }
      this.emit({ kind: 'arrive', item, text: `${item.title} (${src}): ${lvl} — ${this.admissionReason(item, mode)} → held for the ${this.nextBreakpointText()} breakpoint.` });
    }
    _breakpoint(reason) {
      const m = this.now, mode = this.mode();
      const due = this.items.filter((i) => i.state === 'open' && !i.released && i.actor === 'you' && (i.snoozedUntil == null || i.snoozedUntil <= m));
      const s = (n) => (n === 1 ? '' : 's');
      if (mode.id === 'off') { if (due.length) this.emit({ kind: 'digest', text: `${reason}: Off has no digest — ${due.length} held item${s(due.length)} wait for the next breakpoint outside Off.` }); return; }
      if (!due.length) return this.emit({ kind: 'digest', text: `${reason}: nothing was held, so no digest.` });
      for (const i of due) this._release(i, 'digest');
      const rec = { at: m, kind: 'digest', title: `Digest ${hhmm(m)}`, body: due.map((i) => `${i.title} (${this.level(i)})`).join(' · '), urgency: 'normal', topic: 'digest', itemIds: due.map((i) => i.id) };
      this.pushes.push(rec); this.digests.push(rec);
      this.emit({ kind: 'digest', push: rec, text: `${reason}: one digest push (Topic-collapsed, Urgency: normal) with ${due.length} item${s(due.length)} — ${due.map((i) => i.title).join(', ')}.` });
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

    // ---- conversations with Ara, replies in chats -------------------------------------
    _dialogue(item) {
      const D = root.Dialogues;
      if (item.kind === 'chat') { const c = D.COMPANIONS[item.id]; if (!c) return null; const ep = item.episode; return ep === 0 ? c : (c[`later${ep === 1 ? '' : ep}`] || c.later3 || c); }
      return D.THREADS[item.id] || null;
    }
    _fill(text, item) { return text.replace('{time}', hhmm(this.now)).replace('{arrived}', hhmm(item.arrived != null ? item.arrived : this.now)); }
    ensureThread(id) {
      const item = this.byId.get(id); if (!item || item.thread.length) return item;
      const d = this._dialogue(item);
      const opening = d && d.opening ? d.opening : (item.kind === 'chat' ? 'What do you want to do with this?' : item.body || 'What do you want to do with this?');
      item.thread.push({ who: 'ara', text: this._fill(opening, item), t: item.kind === 'chat' ? this.now : (item.arrived != null && item.arrived > 0 ? item.arrived : this.now) });
      item.chips = d && d.chips ? d.chips.slice() : [];
      return item;
    }
    // The user's turn in the thread: a chip label or free text. Returns Ara's reply record.
    say(id, text) {
      const item = this.ensureThread(id); if (!item) return null;
      item.thread.push({ who: 'you', text, t: this.now });
      const d = this._dialogue(item); const r = (d && d.replies && d.replies[text]) || (d && d.free) || { text: 'Noted. I keep this with the item.' };
      const reply = { who: 'ara', text: this._fill(r.text, item), t: this.now };
      item.thread.push(reply); item.chips = r.chips ? r.chips.slice() : (r.done || r.later || r.waitOn ? [] : item.chips);
      const short = (x) => (x.length > 70 ? x.slice(0, 67) + '…' : x);
      this.emit({ kind: 'action', item, text: `${item.kind === 'chat' ? 'Ara pane' : 'Thread'} “${item.title}” — you: “${short(text)}” · Ara: “${short(reply.text)}”` });
      if (r.note) this.emit({ kind: 'system', item, text: `(${r.note})` });
      if (r.waitOn) { item.actor = r.waitOn; item.waiting_since = this.now; item.released = true; item.snoozedUntil = null; this.emit({ kind: 'action', item, text: `${item.title} is parked on ${r.waitOn}.` }); }
      if (r.later) this.later(id, r.later);
      if (r.done) this.doIt(id);
      return { reply, draft: r.draft ? this._fill(r.draft, item) : null };
    }
    // Send a reply in a messenger chat; the chat counts as handled.
    sendChat(id, text) {
      const item = this.byId.get(id); if (!item || item.kind !== 'chat') return false;
      item.chat.push({ dir: 'out', text, t: this.now });
      if (item.state === 'open') { item.state = 'done'; item.doneAt = this.now; item.doneHow = 'replied'; this.doneToday += 1; }
      this.emit({ kind: 'action', item, text: `Replied to ${item.sender} on ${item.channel}: “${text}” — handled.` });
      return true;
    }

    // ---- what the user can do ----------------------------------------------------
    doIt(id) { const item = this.byId.get(id); if (!item || item.state !== 'open') return false; item.state = 'done'; item.doneAt = this.now; this.doneToday += 1; this.emit({ kind: 'action', item, text: `Done: ${item.title}.` }); return true; }
    later(id, when) { const item = this.byId.get(id); if (!item || item.state !== 'open') return false; const until = when === 'tomorrow' ? DAY + 8 * H : this.nextBreakpoint(); item.released = false; item.releasedBy = null; item.snoozedUntil = until; this.emit({ kind: 'action', item, text: `Later: ${item.title} waits until ${whenText(until, this.now)}.` }); return true; }
    pull(id) { const item = this.byId.get(id); if (!item || item.state !== 'open' || item.released) return false; this._release(item, 'you'); this.emit({ kind: 'action', item, text: `You pull ${item.title} out of Held ahead of the digest.` }); return true; }
    setManualMode(id) {
      const before = this.mode(); this.manualMode = id; const after = this.mode(); this._trackMode();
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
    admitSphere(sphere, modeId, on) {
      const mode = this.modes[modeId]; const has = mode.admits.includes(sphere); if (on === has) return false; this.corrections += 1;
      if (on) mode.admits.push(sphere); else mode.admits.splice(mode.admits.indexOf(sphere), 1);
      this._learn(`${mode.name} ${on ? 'now admits' : 'no longer admits'} ${sphere}`);
      for (const i of this.items) if (i.sphere === sphere && i.state === 'open') this._reevaluate(i, 'the Focus rule', this.level(i));
      return true;
    }
    _learn(text) { this.profile.learned.push({ at: this.now, text }); this.emit({ kind: 'learn', text: `Profile: ${text}.` }); }

    // ---- views --------------------------------------------------------------------
    sections() {
      const mode = this.mode(); const open = this.items.filter((i) => i.state === 'open');
      const now = [], next = [], held = [], waiting = [];
      for (const i of open) { if (i.actor !== 'you') waiting.push(i); else if (!i.released) held.push(i); else if (this.breaksThrough(i, mode)) now.push(i); else next.push(i); }
      const key = (i) => [-rank(this.level(i)), -i.importance, i.due == null ? Infinity : i.due - this.now];
      const cmp = (a, b) => { const ka = key(a), kb = key(b); for (let k = 0; k < 3; k++) if (ka[k] !== kb[k]) return ka[k] - kb[k]; return a.title.localeCompare(b.title); };
      now.sort(cmp); next.sort(cmp); held.sort(cmp); waiting.sort((a, b) => (a.waiting_since || 0) - (b.waiting_since || 0));
      return { now, next, held, waiting, mode };
    }
    stats() {
      const byUrgency = { high: 0, normal: 0 }; for (const p of this.pushes) byUrgency[p.urgency] = (byUrgency[p.urgency] || 0) + 1;
      const s = this.sections();
      return { pushes: this.pushes.length, itemPushes: byUrgency.high, digests: this.digests.length, held: s.held.length, now: s.now.length, next: s.next.length, waiting: s.waiting.length, done: this.doneToday, corrections: this.corrections };
    }
  }

  root.AttentionEngine = AttentionEngine;
  root.AttentionUtil = { hhmm, whenText, dateText, dur, dayIndex, rank, LEVELS, MODES, SCHEDULE, DIGEST_TIMES, scheduledId, DAY, H };
})(globalThis);
