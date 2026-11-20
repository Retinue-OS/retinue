// Example data for the dossier prototype — the attention prototype's day
// (examples/attention-prototype/backends.js), unchanged in what arrives and
// when, re-addressed under docs/dossiers.md: every sender is a relationship
// dossier, every agent-opened thread an Ara-only topic, every project a topic.
// Two of the old threads were letters the Secretary had triaged; here they are
// what they were — e-mails — and land in the sender's relationship, with Inbox
// Zero proposing what takes them out of the mailbox.
// Times are minutes since 00:00 of the simulated day (Thursday 3 September
// 2026); negative values are yesterday.
(function (root) {
  'use strict';
  const DAY = 1440;
  const t = (h, m = 0) => h * 60 + m;
  const days = (d, h = 0, m = 0) => d * DAY + h * 60 + m;

  const SPHERES = ['customers', 'admin', 'health', 'friends', 'family', 'system'];

  // Relationships: one per person or durable group. `gate` is the sender axis
  // (vip = a model turn on arrival; everyone else is shown at once and judged
  // in the next batch). `firstSeen` marks a sender the contacts had not met
  // before the day — their relationship is created by the arrival itself.
  const CONTACTS = {
    'Anna Keller':    { sphere: 'friends',   gate: 'vip',   channel: 'WhatsApp' },
    'Beat Frei':      { sphere: 'customers', gate: 'vip',   channel: 'Signal', org: 'Frei Bau AG' },
    'Mum':            { sphere: 'family',    gate: 'vip',   channel: 'WhatsApp' },
    'Luca Meier':     { sphere: 'friends',   gate: 'vip',   channel: 'Telegram' },
    'Quartier group': { sphere: 'friends',   gate: 'group', channel: 'WhatsApp', group: true },
    'Parking app':    { sphere: 'admin',     gate: 'batch', channel: 'E-mail', org: 'noreply@parking.example' },
    'Tax office':     { sphere: 'admin',     gate: 'batch', channel: 'E-mail', org: 'Steuerverwaltung, via ePost' },
  };

  // Importance priors per relationship (what the attention profile starts with).
  const PRIORS = { 'Beat Frei': 4, 'Anna Keller': 3, 'Mum': 3, 'Luca Meier': 3, 'Quartier group': 1, 'Parking app': 1, 'Tax office': 5 };

  // Lead-time defaults per kind of pending thing, in minutes.
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

  // Messages belong to exactly one dossier: a 1:1 chat or a single-sender mail
  // to the relationship, a group chat to the group's. `triage` is the
  // classification a model turn (VIP: on arrival; batch: at the next digest)
  // puts on the relationship's pending state.
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
    // The card renewal: a mail from the parking app, in the inbox since last night. The 08:00 batch judges it.
    { at: days(-1, 20, 15), chat: 'Parking app', sender: 'Parking app', mail: true, subject: 'Your card on file expires on Friday', text: 'The card ending 4821 on your parking account expires on 4 September. Update it to keep parking without interruption.', triage: { importance: 1, due: days(1, 23, 59), kind: 'admin chore' } },
    { at: t(6, 40),  chat: 'Mum',         sender: 'Mum',         text: 'Call me when you are up ☕', triage: { importance: 3, due: t(9, 0), kind: 'family note' } },
    { at: t(8, 40),  chat: 'Anna Keller', sender: 'Anna Keller', text: 'Dinner on Friday? 19:00 at ours', triage: { importance: 3, due: days(1, 18, 0), kind: 'invitation' } },
    { at: t(9, 15),  chat: 'Anna Keller', sender: 'Anna Keller', text: 'Also: could you bring the projector?', triage: { importance: 3, due: days(1, 18, 0), kind: 'invitation' } },
    { at: t(10, 5),  chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Question on clause 7 of the contract — I need your view by tomorrow noon', triage: { importance: 4, due: days(1, 12, 0), kind: 'customer request', tags: ['finance'] } },
    { at: t(14, 20), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Thanks. One more: can we sign on Monday?', triage: { importance: 4, due: days(4, 12, 0), kind: 'customer request' } },
    // The tax office's letter via ePost: a mail, judged by the batch at 17:00 — the sender is no VIP.
    { at: t(14, 45), chat: 'Tax office',  sender: 'Tax office',  mail: true, subject: 'Steuererklärung 2025 — Einreichung der Jahresrechnung', text: 'Please submit the signed 2025 income statement, the balance sheet and the depreciation schedule by 30 September 2026.', attachments: [{ name: 'Aufforderung_2025.pdf', size: 184320 }], triage: { importance: 5, due: days(27, 17, 0), kind: 'tax filing', tags: ['finance'] } },
    { at: t(15, 30), chat: 'Mum',         sender: 'Mum',         text: 'Are you coming for lunch on Sunday?', triage: { importance: 3, due: days(3, 12, 0), kind: 'family note' } },
    { at: t(19, 5),  chat: 'Luca Meier',  sender: 'Luca Meier',  text: 'We are at the Werdinsel — coming? Fire is on at 19:30', triage: { importance: 4, due: t(19, 30), kind: 'invitation' } },
    { at: t(19, 40), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Could you send the signed NDA tonight? Their lawyer wants it before 22:00', triage: { importance: 4, due: t(22, 0), kind: 'customer request' } },
    { at: t(23, 15), chat: 'Beat Frei',   sender: 'Beat Frei',   text: 'Got it, thanks — good night', triage: { importance: 2, kind: 'acknowledgement' } },
  ];

  // Topics Ara opens herself (today's agent threads): the only participant
  // besides the user is Ara. `at` is when the topic is opened.
  const ARA_TOPICS = [
    { at: days(-1, 16, 30), id: 'thr-quote',  title: 'Quote for Müller AG', body: 'Draft ready for your review; Müller expects it today by 17:00.', agent: 'Secretary', sphere: 'customers', tags: ['finance'], importance: 4, due: t(17, 0), kind: 'customer request' },
    { at: t(7, 40),  id: 'thr-physio', title: 'Physio today 15:30 — leave by 15:00', body: 'Confirmed by the practice. Bus 33 leaves at 15:02.', agent: 'Ara', sphere: 'health', importance: 4, due: t(15, 30), kind: 'appointment' },
    { at: t(10, 40), id: 'thr-backup', title: 'Backup job failed on the home server', body: 'Nightly backup exited with code 2 (disk full).', agent: 'Ara', sphere: 'system', importance: 5, critical: true, kind: 'system alert' },
  ];

  // Topics in the chambers (project files). States per docs/dossiers.md: active,
  // waiting (derived from the actor), resting (paused, with a wake), done.
  const TOPICS = [
    { id: 'prj-vat',       title: 'Quarterly VAT return', sphere: 'admin', tags: ['finance'], importance: 4, expected_by: days(22, 17, 0), remind_before: 14 * DAY, current_actor: 'you', kind: 'tax filing' },
    { id: 'prj-insurance', title: 'Insurance decision on the therapy', sphere: 'health', tags: ['admin'], importance: 4, current_actor: 'the insurance office', waiting_since: days(-12), kind: 'admin chore' },
    { id: 'prj-brochure',  title: 'Brochure translation', sphere: 'customers', importance: 3, current_actor: 'Publisher', waiting_since: days(-2), kind: 'customer request' },
    // Resting with a cadence: the recurring-projects job wakes it on the day.
    { id: 'prj-invoices',  title: 'Monthly invoice run', sphere: 'customers', tags: ['admin', 'finance'], importance: 4, recurring: 'monthly', next_due: t(18, 0), remind_before: 1 * DAY, current_actor: 'you', paused: true, wakeAt: t(8, 0), kind: 'invoice run' },
    // A parent topic with two sub-topics (`within`): the engagement, and the pieces of work inside it.
    { id: 'prj-crr',       title: 'CRR data platform', sphere: 'customers', importance: 4, current_actor: 'you', kind: 'customer request', participants: ['Beat Frei', 'Studio Nord'], body: 'The engagement with Frei Bau AG and Studio Nord: one dossier for the whole, the pieces of work within it.' },
    { id: 'prj-crr-import', title: 'CRR: import pipeline', within: 'prj-crr', importance: 4, expected_by: days(8, 17, 0), remind_before: 5 * DAY, current_actor: 'you', kind: 'customer request', participants: ['Beat Frei'] },
    { id: 'prj-crr-docs',   title: 'CRR: handover documentation', within: 'prj-crr', importance: 3, current_actor: 'Studio Nord', waiting_since: days(-4), kind: 'customer request', participants: ['Studio Nord'] },
    // Resting without a date ("someday") and a done one: visible on the Dossiers page only.
    { id: 'prj-garden',    title: 'Rain barrel for the garden', sphere: 'family', importance: 2, current_actor: 'you', paused: true, kind: 'admin chore' },
    { id: 'prj-website',   title: 'Website relaunch', sphere: 'customers', importance: 3, current_actor: 'you', resolved: true, doneAt: days(-9, 15, 0), kind: 'customer request' },
  ];

  // Standing dossiers: never done. Inbox Zero keeps the mailbox empty; its
  // events are the batches' proposals.
  const STANDING = [
    { id: 'inbox-zero', title: 'Inbox Zero', sphere: 'admin', importance: 2, current_actor: 'Ara', kind: 'admin chore', body: 'Every mail still in the inbox gets a proposed way out; what you dealt with leaves by itself.' },
  ];

  const inWindow = (at, from, to) => at > from && at <= to;

  const messages = {
    name: 'messages', route: '/ledger',
    events(from, to) { return MESSAGES.filter((m) => inWindow(m.at, from, to)).map((m) => ({ type: 'message', ...m })); },
    list(now) {
      const byChat = new Map();
      for (const m of MESSAGES) { if (m.at > now) continue; const c = CONTACTS[m.chat] || {}; byChat.set(m.chat, { id: `${(c.channel || 'chat').toLowerCase()}:~acct:${m.chat}`, channel: c.channel, name: m.chat, group: !!c.group, last: { ts: m.at, text: m.text, direction: 'in', author: m.from || m.sender } }); }
      return [...byChat.values()].sort((a, b) => b.last.ts - a.last.ts);
    },
  };

  const araTopics = {
    name: 'Ara topics', route: '/dossiers?kind=ara',
    events(from, to) { return ARA_TOPICS.filter((x) => inWindow(x.at, from, to)).map((x) => ({ type: 'thread', ...x })); },
    list(now) { return ARA_TOPICS.filter((x) => x.at <= now).map((x) => ({ id: x.id, title: x.title, initiator: 'agent', created: x.at, updated: x.at, last_preview: x.body })); },
  };

  const topics = {
    name: 'topics', route: '/dossiers',
    events(from, to) { return TOPICS.filter((p) => p.wakeAt != null && inWindow(p.wakeAt, from, to)).map((p) => ({ type: 'wake', at: p.wakeAt, ...p })); },
    initial() { return TOPICS.concat(STANDING.map((s) => ({ ...s, standing: true }))); },
    list(now) { return TOPICS.filter((p) => !p.paused || (p.wakeAt != null && p.wakeAt <= now)).concat(STANDING).map((p) => ({ id: p.id, title: p.title, currentActor: p.current_actor, expected: p.expected_by || p.next_due || null, waitingOn: p.current_actor !== 'you' ? p.current_actor : null, since: p.waiting_since || null })); },
  };

  root.Backends = { DAY, t, days, SPHERES, CONTACTS, PRIORS, LEAD_DEFAULTS, MESSAGES, messages, araTopics, topics, chats: messages, conversations: araTopics, projects: topics, all: [messages, araTopics, topics] };
})(globalThis);
