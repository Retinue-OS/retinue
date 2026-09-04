// Example-data backends for the attention prototype.
//
// Each backend stands in for one real gateway route (/chats, /conversations,
// /projects): `list()` returns records in that route's shape, `events(from, to)`
// yields what arrived in a window. Times are minutes since 00:00 of the
// simulated day (Thursday 3 September 2026); negative values are yesterday.
// The `triage` block on a message is what the Secretary's live triage turn
// returns for a whitelisted sender: importance, an extracted deadline, the
// kind of item (which supplies the lead-time default) and extra spheres as tags.
(function (root) {
  'use strict';
  const DAY = 1440;
  const t = (h, m = 0) => h * 60 + m;
  const days = (d, h = 0, m = 0) => d * DAY + h * 60 + m;

  const SPHERES = ['customers', 'admin', 'health', 'friends', 'family', 'system'];

  const CONTACTS = {
    'Anna Keller':    { sphere: 'friends',   gate: 'whitelisted', channel: 'WhatsApp' },
    'Beat Frei':      { sphere: 'customers', gate: 'whitelisted', channel: 'Signal', org: 'Frei Bau AG' },
    'Mum':            { sphere: 'family',    gate: 'whitelisted', channel: 'WhatsApp' },
    'Luca Meier':     { sphere: 'friends',   gate: 'whitelisted', channel: 'Telegram' },
    'Quartier group': { sphere: 'friends',   gate: 'group',       channel: 'WhatsApp', group: true },
  };

  // Importance priors per sender (what the attention profile starts with).
  const PRIORS = { 'Beat Frei': 4, 'Anna Keller': 3, 'Mum': 3, 'Luca Meier': 3, 'Quartier group': 1 };

  // Lead-time defaults per kind of item, in minutes.
  const LEAD_DEFAULTS = {
    default: 3 * DAY,
    'customer request': 2 * DAY,
    'invitation': 2 * DAY,
    'family note': 3 * DAY,
    'appointment': 2 * 60,
    'tax filing': 14 * DAY,
    'admin chore': 3 * DAY,
    'system alert': 60,
    'group chatter': 3 * DAY,
    'acknowledgement': 3 * DAY,
    'invoice run': 1 * DAY,
  };

  const MESSAGES = [
    { at: days(-1, 20, 12), chat: 'Quartier group', sender: 'Quartier group', from: 'Petra',   text: 'Street party is on for 20 September — who is in?', triage: { importance: 1, kind: 'group chatter' } },
    { at: days(-1, 20, 14), chat: 'Quartier group', sender: 'Quartier group', from: 'Marco',   text: 'In. We can do the drinks again.' },
    { at: days(-1, 20, 15), chat: 'Quartier group', sender: 'Quartier group', from: 'Sibylle', text: 'Same as last year, from 16:00?' },
    { at: days(-1, 20, 17), chat: 'Quartier group', sender: 'Quartier group', from: 'Petra',   text: 'Yes, 16:00 until whenever. The permit is confirmed.' },
    { at: days(-1, 20, 20), chat: 'Quartier group', sender: 'Quartier group', from: 'Dani',    text: 'Do we have a grill this time? Ours died.' },
    { at: days(-1, 20, 21), chat: 'Quartier group', sender: 'Quartier group', from: 'Marco',   text: 'Ha. No grill here.' },
    { at: days(-1, 20, 24), chat: 'Quartier group', sender: 'Quartier group', from: 'Sibylle', text: 'I can bring one table.' },
    { at: days(-1, 20, 25), chat: 'Quartier group', sender: 'Quartier group', from: 'Petra',   text: 'So we need two tables and a grill. Anyone?' },
    { at: days(-1, 20, 31), chat: 'Quartier group', sender: 'Quartier group', from: 'Ruth',    text: 'I will bake, as always.' },
    { at: days(-1, 20, 33), chat: 'Quartier group', sender: 'Quartier group', from: 'Dani',    text: 'Kids’ corner: I will organise chalk and the water thing.' },
    { at: days(-1, 20, 40), chat: 'Quartier group', sender: 'Quartier group', from: 'Marco',   text: 'Music: I bring the speaker, playlist suggestions welcome.' },
    { at: days(-1, 20, 41), chat: 'Quartier group', sender: 'Quartier group', from: 'Sibylle', text: 'Not the same playlist as last year 😅' },
    { at: days(-1, 20, 55), chat: 'Quartier group', sender: 'Quartier group', from: 'Petra',   text: 'Summary: 20 Sept, 16:00. Drinks Marco, cake Ruth, one table Sibylle, kids Dani. Still missing: a grill and one more table.' },
    { at: days(-1, 21, 10), chat: 'Quartier group', sender: 'Quartier group', from: 'Ruth',    text: 'Reminder for the newcomers: the street is closed from 14:00.' },
    { at: t(6, 40),  chat: 'Mum',         sender: 'Mum',         text: 'Call me when you are up ☕', triage: { importance: 3, due: t(9, 0), kind: 'family note' } },
    { at: t(8, 40),  chat: 'Anna Keller', sender: 'Anna Keller', text: 'Dinner on Friday? 19:00 at ours', triage: { importance: 3, due: days(1, 18, 0), kind: 'invitation' } },
    { at: t(9, 15),  chat: 'Anna Keller', sender: 'Anna Keller', text: 'Also: could you bring the projector?', triage: { importance: 3, due: days(1, 18, 0), kind: 'invitation' } },
    { at: t(10, 5),  chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Question on clause 7 of the contract — I need your view by tomorrow noon', triage: { importance: 4, due: days(1, 12, 0), kind: 'customer request', tags: ['finance'] } },
    { at: t(14, 20), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Thanks. One more: can we sign on Monday?', triage: { importance: 4, due: days(4, 12, 0), kind: 'customer request' } },
    { at: t(15, 30), chat: 'Mum',         sender: 'Mum',         text: 'Are you coming for lunch on Sunday?', triage: { importance: 3, due: days(3, 12, 0), kind: 'family note' } },
    { at: t(19, 5),  chat: 'Luca Meier',  sender: 'Luca Meier',  text: 'We are at the Werdinsel — coming? Fire is on at 19:30', triage: { importance: 4, due: t(19, 30), kind: 'invitation' } },
    { at: t(19, 40), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Could you send the signed NDA tonight? Their lawyer wants it before 22:00', triage: { importance: 4, due: t(22, 0), kind: 'customer request' } },
    { at: t(23, 15), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Got it, thanks — good night', triage: { importance: 2, kind: 'acknowledgement' } },
  ];

  const THREADS = [
    { at: days(-1, 16, 30), id: 'thr-quote',  title: 'Quote for Müller AG', body: 'Draft ready for your review; Müller expects it today by 17:00.', agent: 'Secretary', sphere: 'customers', tags: ['finance'], importance: 4, due: t(17, 0), kind: 'customer request' },
    { at: days(-1, 20, 15), id: 'thr-card',   title: 'Card renewal for the parking app', body: 'The card on file expires Friday night. Renew, or let it lapse?', agent: 'Secretary', sphere: 'admin', importance: 1, due: days(1, 23, 59), kind: 'admin chore' },
    { at: t(7, 40),  id: 'thr-physio', title: 'Physio today 15:30 — leave by 15:00', body: 'Confirmed by the practice. Bus 33 leaves at 15:02.', agent: 'Ara', sphere: 'health', importance: 4, due: t(15, 30), kind: 'appointment' },
    { at: t(10, 40), id: 'thr-backup', title: 'Backup job failed on the home server', body: 'Nightly backup exited with code 2 (disk full).', agent: 'Ara', sphere: 'system', importance: 5, critical: true, kind: 'system alert' },
    { at: t(14, 45), id: 'thr-tax',    title: 'Tax office asks for the 2025 statement', body: 'Letter via ePost: the statement is due 30 September. I can draft the cover note.', agent: 'Secretary', sphere: 'admin', tags: ['finance'], importance: 5, due: days(27, 17, 0), kind: 'tax filing' },
  ];

  const PROJECTS = [
    { id: 'prj-vat',       title: 'Quarterly VAT return', sphere: 'admin', tags: ['finance'], importance: 4, expected_by: days(22, 17, 0), remind_before: 14 * DAY, current_actor: 'you', kind: 'tax filing' },
    { id: 'prj-insurance', title: 'Insurance decision on the therapy', sphere: 'health', tags: ['admin'], importance: 4, current_actor: 'the insurance office', waiting_since: days(-12), kind: 'admin chore' },
    { id: 'prj-brochure',  title: 'Brochure translation', sphere: 'customers', importance: 3, current_actor: 'Publisher', waiting_since: days(-2), kind: 'customer request' },
    // Paused with a cadence: the recurring-projects job wakes it on the day.
    { id: 'prj-invoices',  title: 'Monthly invoice run', sphere: 'customers', tags: ['admin', 'finance'], importance: 4, recurring: 'monthly', next_due: t(18, 0), remind_before: 1 * DAY, current_actor: 'you', paused: true, wakeAt: t(8, 0), kind: 'invoice run' },
  ];

  const inWindow = (at, from, to) => at > from && at <= to;

  const chats = {
    name: 'chats', route: '/chats',
    events(from, to) { return MESSAGES.filter((m) => inWindow(m.at, from, to)).map((m) => ({ type: 'message', ...m })); },
    list(now) {
      const byChat = new Map();
      for (const m of MESSAGES) { if (m.at > now) continue; const c = CONTACTS[m.chat] || {}; byChat.set(m.chat, { id: `${(c.channel || 'chat').toLowerCase()}:~acct:${m.chat}`, channel: c.channel, name: m.chat, group: !!c.group, last: { ts: m.at, text: m.text, direction: 'in', author: m.from || m.sender } }); }
      return [...byChat.values()].sort((a, b) => b.last.ts - a.last.ts);
    },
  };

  const conversations = {
    name: 'conversations', route: '/conversations',
    events(from, to) { return THREADS.filter((x) => inWindow(x.at, from, to)).map((x) => ({ type: 'thread', ...x })); },
    list(now) { return THREADS.filter((x) => x.at <= now).map((x) => ({ id: x.id, title: x.title, initiator: 'agent', kind: 'chat', created: x.at, updated: x.at, unread: true, last_preview: x.body })); },
  };

  const projects = {
    name: 'projects', route: '/projects',
    events(from, to) { return PROJECTS.filter((p) => p.wakeAt != null && inWindow(p.wakeAt, from, to)).map((p) => ({ type: 'wake', at: p.wakeAt, ...p })); },
    initial() { return PROJECTS.filter((p) => !p.paused); },
    list(now) { return PROJECTS.filter((p) => !p.paused || (p.wakeAt != null && p.wakeAt <= now)).map((p) => ({ id: p.id, title: p.title, currentActor: p.current_actor, expected: p.expected_by || p.next_due || null, waitingOn: p.current_actor !== 'you' ? p.current_actor : null, since: p.waiting_since || null })); },
  };

  root.Backends = { DAY, t, days, SPHERES, CONTACTS, PRIORS, LEAD_DEFAULTS, chats, conversations, projects, all: [chats, conversations, projects] };
})(globalThis);
