// The scripted day and the simulation runner. The script is the human layer:
// what you are doing, and the choices you make — following the nudge or not.
// The engine narrates its own side (arrivals, digests, pushes) as it runs.
(function (root) {
  'use strict';
  const { t, DAY } = root.Backends;

  const SCRIPT = [
    { at: t(0, 0),   who: 'narrator', text: 'Thursday, 3 September, midnight. You are asleep; the mode is Off, so only a critical alert could ring before 07:00. Open from yesterday: a customer quote due at 17:00, the quarterly VAT return, a card renewal, a chatty group, and two projects waiting on other people.' },
    { at: t(6, 40),  who: 'narrator', text: 'Your mother writes. Family is admitted in Off only at critical level, so the message is held; the morning digest would carry it.' },
    { at: t(7, 0),   who: 'you', text: 'You are up. Home mode until 08:00: family and health may break through.' },
    { at: t(7, 5),   who: 'you', action: { type: 'pull', id: 'chat:Mum' }, text: 'Coffee, phone. Held shows one item. You open it and pull your mother’s message out before any digest: the system proposes a rhythm, it never locks the door.' },
    { at: t(7, 8),   who: 'you', action: { type: 'doIt', id: 'chat:Mum' }, text: 'You call her back. Done.' },
    { at: t(7, 40),  who: 'narrator', text: 'Ara confirms the physio appointment for 15:30. Importance 4 with a two-hour lead time: eight hours out it is merely active, so it waits for the 08:00 digest instead of ringing.' },
    { at: t(8, 0),   who: 'narrator', text: 'The morning digest lands as one push and Deep work begins: nothing but critical rings before 12:00. The Müller quote is time-sensitive and tops Next; Deep work admits no customers, so it did not ring, but it is the obvious place to start.' },
    { at: t(8, 20),  who: 'you', text: 'You work on the quote. This is the nudge working as intended: the most urgent important thing, visible, not shouting.' },
    { at: t(8, 40),  who: 'narrator', text: 'Anna asks about dinner on Friday. Friends are not admitted in Deep work; the invitation is held for 12:00.' },
    { at: t(9, 15),  who: 'narrator', text: 'Anna writes again. The repeat policy for friends is off and her message classifies the same, so nothing escalates; a fixed five-minute window would have rewarded impatience.' },
    { at: t(10, 5),  who: 'narrator', text: 'Beat Frei, a customer, wants a view on a contract clause by tomorrow noon. Time-sensitive, but Deep work admits no customers: held for 12:00. The half-hourly sweep re-checks it every thirty minutes; a permit for Beat would have let it ring.' },
    { at: t(10, 40), who: 'narrator', text: 'The home server’s backup failed. Critical is declared, never derived, and it rings in every mode.' },
    { at: t(10, 42), who: 'you', action: { type: 'later', id: 'thr-backup', when: 'next' }, text: 'You glance at it and tap Later: the alert waits for the 12:00 breakpoint. Even a critical item bends to you.' },
    { at: t(11, 30), who: 'you', action: { type: 'doIt', id: 'thr-quote' }, text: 'The quote goes out, five and a half hours before the deadline. Done.' },
    { at: t(12, 0),  who: 'narrator', text: 'Midday digest: one push instead of four. Open mode for lunch admits every sphere, so Now shows exactly what may interrupt: the contract clause, the invoice run and the snoozed backup alert. Anna’s dinner, merely active, sits in Next.' },
    { at: t(12, 10), who: 'you', action: { type: 'doIt', id: 'chat:Anna Keller' }, text: 'You are in the mood for people, not contracts: you answer Anna first — yes to Friday, projector too. The clause keeps its place; nothing nags.' },
    { at: t(12, 40), who: 'you', action: { type: 'doIt', id: 'thr-backup' }, text: 'You free the disk and restart the backup. Done.' },
    { at: t(13, 0),  who: 'narrator', text: 'Work mode until 17:00: customers, admin and health may break through.' },
    { at: t(13, 5),  who: 'you', action: { type: 'doIt', id: 'chat:Beat Frei' }, text: 'You answer Beat on clause 7.' },
    { at: t(13, 30), who: 'narrator', text: 'The sweep finds the physio appointment inside its two-hour lead time: it climbs to time-sensitive, health is admitted in Work, and it pushes — two hours ahead, as the lead time intended.' },
    { at: t(13, 40), who: 'you', action: { type: 'doIt', id: 'prj-invoices' }, text: 'You run the monthly invoices while the afternoon is quiet. Done.' },
    { at: t(14, 20), who: 'narrator', text: 'Beat asks about signing on Monday. Admitted in Work, but only active: it waits for the 17:00 digest. If you want it earlier, Held is one tap away.' },
    { at: t(14, 45), who: 'narrator', text: 'The Secretary triaged a letter from the tax office: statement due 30 September, importance 5, lead time two weeks. Twenty-seven days out it is active, so it waits for the digest too.' },
    { at: t(14, 58), who: 'you', action: { type: 'doIt', id: 'thr-physio' }, text: 'You mark the appointment as handled and pack up.' },
    { at: t(15, 0),  who: 'you', action: { type: 'mode', id: 'off' }, text: 'You leave for physio and switch the mode to Off by hand. The schedule is suspended until you release it.' },
    { at: t(15, 30), who: 'narrator', text: 'Your mother asks about Sunday lunch. Off: held.' },
    { at: t(16, 15), who: 'you', action: { type: 'mode', id: null }, text: 'Back at the desk you release the mode to the schedule. Work resumes; the change is a breakpoint, so what was held meanwhile arrives as a small digest.' },
    { at: t(16, 30), who: 'you', action: { type: 'lead', id: 'prj-vat', lead: 28 * DAY }, text: 'The VAT return has sat in Next all day. Two weeks is too short a lead for a filing like this, so you correct the lead time to four weeks. The profile learns it for every tax filing, and with the deadline now inside the lead time the return climbs to time-sensitive and moves to Now.' },
    { at: t(16, 35), who: 'you', action: { type: 'later', id: 'prj-vat', when: 'tomorrow' }, text: 'You do thirty minutes on it and park it until tomorrow morning.' },
    { at: t(17, 0),  who: 'narrator', text: 'Nothing was held, so the 17:00 breakpoint passes without a digest: an open hour before Social.' },
    { at: t(17, 10), who: 'you', action: { type: 'doIt', id: 'chat:Beat Frei' }, text: 'Monday signing confirmed. Done.' },
    { at: t(18, 0),  who: 'narrator', text: 'Social mode: friends and family may break through; customers wait for 21:00.' },
    { at: t(19, 5),  who: 'narrator', text: 'Luca: fire on at the Werdinsel at 19:30. Time-sensitive and friends are admitted, so it rings. The same message at 10:00 would have waited for a digest.' },
    { at: t(19, 8),  who: 'you', action: { type: 'doIt', id: 'chat:Luca Meier' }, text: 'On your way.' },
    { at: t(19, 40), who: 'narrator', text: 'Beat needs a signed NDA before 22:00. Time-sensitive, but Social does not admit customers: held for 21:00 — unless Beat holds a permit. Try it yourself: open Held, tap the item, and let Beat interrupt in Social.' },
    { at: t(20, 30), who: 'you', action: { type: 'permit', sender: 'Beat Frei', mode: 'social', on: true }, text: 'You decide Beat may interrupt you in Social. A delivery correction: the permit is stored per mode, importance untouched. The NDA request moves to Now and pushes.' },
    { at: t(20, 45), who: 'you', action: { type: 'doIt', id: 'chat:Beat Frei' }, text: 'NDA signed and sent.' },
    { at: t(21, 0),  who: 'narrator', text: 'The 21:00 breakpoint passes without a digest too. The card renewal is still passive: it crosses into the last third of its lead time at midnight, climbs to active, and appears in tomorrow’s morning digest — never the bell.' },
    { at: t(21, 5),  who: 'you', action: { type: 'doIt', id: 'chat:Mum' }, text: 'Sunday lunch: yes.' },
    { at: t(22, 0),  who: 'narrator', text: 'Off. Whatever arrives now sleeps until 08:00.' },
    { at: t(23, 15), who: 'narrator', text: 'Beat says thanks. Importance 2, no deadline: passive, listed, silent.' },
    { at: t(23, 59), who: 'narrator', summary: true, text: 'Midnight. The day in numbers is in the panel on the right: how often the bell rang, how much waited for a digest, and what the profile learned from your two corrections.' },
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
