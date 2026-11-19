// The scripted day and the simulation runner. The same twenty-four hours as the
// attention prototype — the same messages, mails, topics and wake-ups at the
// same minutes — lived under docs/dossiers.md: every row is a dossier, the
// contacts page is the chat list, mail is in the open from the moment it
// arrives, and Inbox Zero proposes what takes each mail out of the inbox.
(function (root) {
  'use strict';
  const { t, DAY } = root.Backends;

  const SCRIPT = [
    { at: t(0, 0),   who: 'narrator', text: 'Thursday, 3 September, midnight. You are asleep; the mode is Off, so only a critical alert could ring before 07:00. Pending from yesterday, every one a dossier: a customer quote due at 17:00 (a topic with Ara), the quarterly VAT return (a topic), two topics waiting on other people, and the Quartier group’s chatter. A mail from the parking app sits in the inbox: visible under Contacts already, judged by Inbox Zero at the 08:00 batch — the sender is no VIP, so no model turn until then.' },
    { at: t(6, 40),  who: 'narrator', text: 'Your mother writes. Her relationship dossier gets a pending state (a call, by nine). Family is admitted in Off only at critical level, so it is held; the morning digest would carry it.' },
    { at: t(7, 0),   who: 'you', text: 'You are up. Home mode until 08:00: family and health may break through.' },
    { at: t(7, 5),   who: 'you', action: { type: 'pull', id: 'rel:Mum' }, text: 'Coffee, phone. The home shows one held row — your mother. You pull it into Now before any digest: the system proposes a rhythm, it never locks the door.' },
    { at: t(7, 8),   who: 'you', action: { type: 'reply', id: 'rel:Mum', text: 'Just up ☕ — I’ll call you in ten minutes.' }, text: 'The dossier opens on her message, Ara’s remark beside it, and a composer addressed to her. You reply, then call her. Nothing pending — the row leaves the home and sits in Contacts like any quiet chat; nothing to archive.' },
    { at: t(7, 40),  who: 'narrator', text: 'Ara opens a topic: the physio appointment at 15:30, confirmed. Importance 4 with a two-hour lead time: eight hours out it is merely active, so it waits for the 08:00 digest instead of ringing.' },
    { at: t(8, 0),   who: 'narrator', text: 'The 08:00 batch and the morning digest, one push. Inbox Zero judged the parking mail: importance 1, archive once you have decided about the card — the proposal is in its stream, nowhere else. Deep work begins: nothing but critical rings before 12:00. The Müller quote is time-sensitive and tops the Active list; Deep work admits no customers, so it did not ring, but it is the obvious place to start.' },
    { at: t(8, 20),  who: 'you', text: 'You work on the quote. This is the nudge working as intended: the most urgent important thing, visible, not shouting.' },
    { at: t(8, 40),  who: 'narrator', text: 'Anna asks about dinner on Friday. Friends are not admitted in Deep work; the invitation is held for 12:00.' },
    { at: t(9, 15),  who: 'narrator', text: 'Anna writes again, into the same dossier. The repeat policy for friends is off and her message classifies the same, so nothing escalates; a fixed five-minute window would have rewarded impatience.' },
    { at: t(10, 5),  who: 'narrator', text: 'Beat Frei, a customer, wants a view on a contract clause by tomorrow noon. Time-sensitive, but Deep work admits no customers: held for 12:00. The half-hourly sweep re-checks it every thirty minutes; a permit for Beat would have let it ring.' },
    { at: t(10, 40), who: 'narrator', text: 'The home server’s backup failed; Ara opens a topic. Critical is declared, never derived, and it rings in every mode.' },
    { at: t(10, 42), who: 'you', action: { type: 'later', id: 'thr-backup', when: 'next' }, text: 'You glance at it and tap Later: the alert waits for the 12:00 breakpoint. Even a critical dossier bends to you.' },
    { at: t(11, 30), who: 'you', action: { type: 'chip', id: 'thr-quote', label: 'Send it' }, text: 'You open the quote: Ara has the draft ready and offers Show, Send, Change. You tap Send it; she sends the PDF to Müller AG and files it as an event. Done, five and a half hours before the deadline — the topic moves to Done on the Dossiers page.' },
    { at: t(12, 0),  who: 'narrator', text: 'Midday digest: one push instead of four. Open mode for lunch admits every sphere, so Now shows exactly what may interrupt: Beat’s clause, the invoice run and the snoozed backup alert. Anna’s dinner, merely active, sits below.' },
    { at: t(12, 10), who: 'you', action: { type: 'reply', id: 'rel:Anna Keller', text: 'Yes to Friday, 19:00 — and I’ll bring the projector.' }, text: 'You are in the mood for people, not contracts: you open Anna’s dossier, take Ara’s drafted yes from the composer and send it. The clause keeps its place; nothing nags.' },
    { at: t(12, 40), who: 'you', action: { type: 'chip', id: 'thr-backup', label: 'Delete old snapshots' }, text: 'In the backup topic Ara names the culprit: old snapshots. One tap and she deletes them and restarts the job. Done.' },
    { at: t(13, 0),  who: 'narrator', text: 'Work mode until 17:00: customers, admin and health may break through.' },
    { at: t(13, 5),  who: 'you', action: { type: 'reply', id: 'rel:Beat Frei', text: 'Cap at the contract value, as in our standard terms — I’ll add a short note to clause 7 today.' }, text: 'Beat’s dossier: Ara recalls your standard position on the liability cap and drafts the reply beside his message; you send it.' },
    { at: t(13, 30), who: 'narrator', text: 'The sweep finds the physio appointment inside its two-hour lead time: it climbs to time-sensitive, health is admitted in Work, and it pushes — two hours ahead, as the lead time intended.' },
    { at: t(13, 40), who: 'you', action: { type: 'chip', id: 'prj-invoices', label: 'Send all three' }, text: 'The invoice run, woken at 08:00 by its cadence: Ara has three drafts ready; you send all three. The topic rests again until next month.' },
    { at: t(14, 20), who: 'narrator', text: 'Beat asks about signing on Monday. His dossier gets a new pending state. Admitted in Work, but only active: it waits for the 17:00 digest. If you want it earlier, the row is one tap away.' },
    { at: t(14, 45), who: 'narrator', text: 'A letter from the tax office arrives as a mail via ePost. It is in the open at once — under Contacts, in the tax office’s relationship, with the PDF — and in the inbox. No model turn: the sender is no VIP, so Inbox Zero judges it at the 17:00 batch. Had you been told “we sent you a letter”, you would find it now, not after a triage run.' },
    { at: t(14, 58), who: 'you', action: { type: 'doIt', id: 'thr-physio' }, text: 'You mark the appointment done and pack up.' },
    { at: t(15, 0),  who: 'you', action: { type: 'mode', id: 'off' }, text: 'You leave for physio and switch the mode to Off by hand. The schedule is suspended until you release it.' },
    { at: t(15, 30), who: 'narrator', text: 'Your mother asks about Sunday lunch. Off: held.' },
    { at: t(16, 15), who: 'you', action: { type: 'mode', id: null }, text: 'Back at the desk you release the mode to the schedule. Work resumes; the change is a breakpoint, so the batch runs and what was held meanwhile arrives as a small digest. Inbox Zero judged the tax letter: importance 5, due 30 September — and proposes a dossier of its own, since a letter with a deadline and documents to gather is a matter, not a mail.' },
    { at: t(16, 20), who: 'you', action: { type: 'chip', id: 'inbox-zero', label: 'Open the tax dossier' }, text: 'You open Inbox Zero and take the proposal: a new topic “Tax statement 2025” opens with the letter as its first event and a comment; the letter leaves the inbox by itself, and under the tax office’s mail a backlink now points to the topic. Nothing was copied — the mail is referenced.' },
    { at: t(16, 22), who: 'you', action: { type: 'chip', id: 'top-tax', label: 'Ask the accountant' }, text: 'In the new dossier Ara knows what the office wants and who has it. You have her ask the accountant; the dossier now waits on her and leaves the Active list.' },
    { at: t(16, 28), who: 'you', action: { type: 'say', id: 'prj-vat', text: 'Can you file the VAT return for me?' }, text: 'The VAT return has sat in Active all day. You open it and ask Ara to file it. She has the figures ready but cannot submit: the portal needs your login. By hand in ten minutes, or a Cowork session with the Ara connector, where Claude fills the form in your browser and asks her for the figures.' },
    { at: t(16, 30), who: 'you', action: { type: 'lead', id: 'prj-vat', lead: 28 * DAY }, text: 'Two weeks is too short a lead for a filing like this, so you correct the lead time to four weeks in the details. The profile learns it for every tax filing, and with the deadline now inside the lead time the return climbs to time-sensitive and moves to Now.' },
    { at: t(16, 35), who: 'you', action: { type: 'chip', id: 'prj-vat', label: 'Rest until tomorrow' }, text: 'You look through the figures and let it rest until tomorrow morning.' },
    { at: t(17, 0),  who: 'narrator', text: 'Nothing was held, so the 17:00 breakpoint passes without a digest: an open hour before Social. The inbox holds one mail — the parking card, still undecided.' },
    { at: t(17, 10), who: 'you', action: { type: 'reply', id: 'rel:Beat Frei', text: 'Monday 10:00 works — see you at your office.' }, text: 'Monday signing: Ara shows the free slots in Beat’s dossier; you propose 10:00.' },
    { at: t(18, 0),  who: 'narrator', text: 'Social mode: friends and family may break through; customers wait for 21:00.' },
    { at: t(19, 5),  who: 'narrator', text: 'Luca: fire on at the Werdinsel at 19:30. Time-sensitive and friends are admitted, so it rings. The same message at 10:00 would have waited for a digest.' },
    { at: t(19, 8),  who: 'you', action: { type: 'reply', id: 'rel:Luca Meier', text: 'On my way 🔥' }, text: 'On your way.' },
    { at: t(19, 40), who: 'narrator', text: 'Beat needs a signed NDA before 22:00. Time-sensitive, but Social does not admit customers: held for 21:00 — unless Beat holds a permit. Try it yourself: open his row, tap ⓘ, and let Beat interrupt in Social.' },
    { at: t(20, 30), who: 'you', action: { type: 'permit', sender: 'Beat Frei', mode: 'social', on: true }, text: 'You decide Beat may interrupt you in Social. A delivery correction: the permit is stored per mode, importance untouched. The NDA request moves to Now and pushes.' },
    { at: t(20, 45), who: 'you', action: { type: 'reply', id: 'rel:Beat Frei', text: 'Signed NDA attached. Good night!' }, text: 'Ara attaches the signed NDA from your drafts; you send it.' },
    { at: t(21, 0),  who: 'narrator', text: 'The 21:00 breakpoint passes without a digest too. The parking card is still passive: it crosses into the last third of its lead time at midnight, climbs to active, and appears in tomorrow’s morning digest — never the bell. Inbox Zero will restate it then; it does not nag twice.' },
    { at: t(21, 5),  who: 'you', action: { type: 'reply', id: 'rel:Mum', text: 'Yes to Sunday lunch — I’ll bring dessert.' }, text: 'Sunday lunch: yes.' },
    { at: t(21, 10), who: 'you', action: { type: 'reference', id: 'rel:Quartier group', from: 'Petra', contains: 'Summary:', target: { title: 'Street party 20 Sept' }, comment: 'Petra’s summary — we could offer the grill.' }, text: 'Scrolling the Quartier group, you long-press Petra’s summary and add it to a new dossier, “Street party 20 Sept”, with a comment. One event, the message referenced, a backlink under Petra’s bubble. That is the gesture: any message, any dossier, a sentence of your own.' },
    { at: t(22, 0),  who: 'narrator', text: 'Off. Whatever arrives now sleeps until 08:00.' },
    { at: t(23, 15), who: 'narrator', text: 'Beat says thanks. Importance 2, no deadline: passive, listed in Contacts, silent.' },
    { at: t(23, 59), who: 'narrator', summary: true, text: 'Midnight. The day in numbers is in the panel on the right: how often the bell rang, how much waited for a digest, what is in the inbox, and what the profile learned from your two corrections. Nothing was archived all day; two dossiers were opened by you.' },
  ];

  class Simulation {
    constructor(engine, opts = {}) {
      this.engine = engine; this.script = SCRIPT; this.index = 0; this.feed = [];
      this.speed = opts.speed || 4.8;          // simulated minutes per real second
      this.beatHold = opts.beatHold != null ? opts.beatHold : 3200; // ms to dwell on a beat while playing
      this.playing = false; this.hold = 0; this.viewerDriving = false; this.ended = false; this.last = null; this.raf = null;
      this.onChange = opts.onChange || (() => {});
      this.raf = opts.raf || null;
      engine.on((ev) => this.feed.push({ t: ev.t, who: ev.push ? 'push' : ev.kind === 'learn' ? 'learn' : ev.kind === 'action' ? 'you' : 'system', text: ev.text, item: ev.item, push: ev.push }));
    }
    get now() { return this.engine.now; }
    nextBeat() { return this.script[this.index] || null; }

    // Apply a scripted user action; false when the precondition no longer holds.
    apply(a) {
      const e = this.engine; const item = a.id ? e.byId.get(a.id) : null;
      switch (a.type) {
        case 'pull': return item && item.state === 'open' && !item.released ? e.pull(a.id) : false;
        case 'doIt': return item && item.state === 'open' ? e.doIt(a.id) : false;
        case 'later': return item && item.state === 'open' ? e.later(a.id, a.when) : false;
        case 'mode': return e.setManualMode(a.id);
        case 'lead': return item && item.state === 'open' ? e.correct(a.id, { lead: a.lead }) : false;
        case 'importance': return item && item.state === 'open' ? e.correct(a.id, { importance: a.importance }) : false;
        case 'permit': return e.permit(a.sender, a.mode, a.on);
        case 'reply': return item && item.state === 'open' ? e.sendChat(a.id, a.text) : false;
        case 'chip': case 'say': return item && item.state === 'open' && (!item.chips || item.chips.includes(a.label) || a.type === 'say') ? !!e.say(a.id, a.type === 'chip' ? a.label : a.text) : false;
        case 'reference': { if (!item) return false; const m = item.stream.find((x) => x.who === 'in' && (!a.from || x.from === a.from) && (!a.contains || (x.text || '').includes(a.contains))); return m && !m.links.length ? !!e.reference(a.id, [m.uid], a.target, a.comment) : false; }
        default: return false;
      }
    }
    runBeat(b) {
      if (b.action) { const ok = this.apply(b.action); if (!ok) { this.feed.push({ t: b.at, who: 'narrator', skipped: true, text: `(${b.text.split('.')[0]} — skipped: you already handled this yourself.)` }); return; } }
      this.feed.push({ t: b.at, who: b.who, text: b.text, summary: !!b.summary });
    }
    // Advance simulated time, running beats in order. With `withHolds`, stop after
    // each beat so the viewer can read it; the clock resumes after `beatHold`.
    advanceTo(target, withHolds) {
      target = Math.min(target, DAY);
      while (this.index < this.script.length && this.script[this.index].at <= target) {
        const b = this.script[this.index]; this.engine.advance(b.at); this.runBeat(b); this.index += 1;
        if (withHolds) { this.hold = this.beatHold; this.onChange(); return; }
      }
      this.engine.advance(target);
      if (this.engine.now >= DAY) { this.ended = true; this.playing = false; }
      this.onChange();
    }
    seek(minute) {
      const wasPlaying = this.playing; this.stopLoop();
      this.engine.reset(); this.feed = []; this.index = 0; this.viewerDriving = false; this.ended = false; this.hold = 0;
      this.advanceTo(Math.max(0, Math.min(minute, DAY)), false);
      if (wasPlaying && !this.ended) this.play();
    }
    step() { const b = this.nextBeat(); if (!b) return this.advanceTo(DAY, false); this.viewerDriving = false; this.advanceTo(b.at, false); }
    play() { if (this.ended) return; this.playing = true; this.viewerDriving = false; this.last = null; this.startLoop(); this.onChange(); }
    pause() { this.playing = false; this.stopLoop(); this.onChange(); }
    toggle() { this.playing ? this.pause() : this.play(); }
    viewerActed() { this.viewerDriving = true; if (this.playing) this.pause(); else this.onChange(); }
    resume() { this.viewerDriving = false; this.play(); }
    tick(dtMs) {
      if (!this.playing) return;
      if (this.hold > 0) { this.hold -= dtMs; return; }
      this.advanceTo(this.engine.now + (dtMs / 1000) * this.speed, true);
    }
    startLoop() {
      if (typeof requestAnimationFrame !== 'function') return;
      const frame = (ts) => { if (!this.playing) return; if (this.last != null) this.tick(Math.min(ts - this.last, 250)); this.last = ts; this.raf = requestAnimationFrame(frame); };
      this.raf = requestAnimationFrame(frame);
    }
    stopLoop() { if (this.raf != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.raf); this.raf = null; this.last = null; }
  }

  root.SCRIPT = SCRIPT;
  root.Simulation = Simulation;
})(globalThis);
