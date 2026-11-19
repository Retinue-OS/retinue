// Canned conversations with Ara, one per dossier, standing in for the model
// turns a real deployment would run. A chip is a canned prompt: tapping it is
// the user's turn, exactly as on the dashboard. Effects: done (the dossier is
// handled / nothing pending), later (parked), waitOn (parked on someone else),
// draft (Ara stages a reply in the composer, addressed to the participants —
// the send press stays the user's), note (something the real system would do
// outside this page). Inbox Zero's chips carry resolve (the mail leaves the
// inbox) or dossier (a topic is opened with the mail as its first event).
// {time} is the current clock.
(function (root) {
  'use strict';
  const DAY = 1440;
  const days = (d, h = 0, m = 0) => d * DAY + h * 60 + m;

  const TOPICS = {
    'thr-quote': {
      summary: 'Draft ready; Müller expects it by 17:00',
      chips: ['Show the draft', 'Send it', 'Change the terms'],
      replies: {
        'Show the draft': { text: 'Position 1: analysis and concept, 4 days. Position 2: implementation, 8 days. Travel at cost. Validity 30 days. The PDF is attached to this dossier.', chips: ['Send it', 'Change the terms'] },
        'Send it': { text: 'Sent to Müller AG at {time} with the PDF attached, and filed here. I will reopen this dossier if they answer.', done: true },
        'Change the terms': { text: 'Tell me what to change and I will redraft.', chips: ['Send it'] },
      },
      free: { text: 'Redrafted with that. Ready when you are.', chips: ['Show the draft', 'Send it'] },
    },
    'thr-physio': {
      summary: 'Confirmed for 15:30; bus 33 at 15:02',
      chips: ['Add to the agenda', 'Remind me at 14:45', 'Reschedule'],
      replies: {
        'Add to the agenda': { text: 'In your agenda: physio 15:30–16:15, bus at 15:02. I will ping you at 14:45.', chips: ['Reschedule'] },
        'Remind me at 14:45': { text: 'I will ping you at 14:45.', chips: ['Add to the agenda'] },
        'Reschedule': { text: 'The practice offers Monday 10:00 or Wednesday 16:00. Which one?', chips: ['Monday 10:00', 'Wednesday 16:00', 'Keep today'] },
        'Monday 10:00': { text: 'Asked the practice for Monday 10:00; I will confirm when they answer.', waitOn: 'the practice' },
        'Wednesday 16:00': { text: 'Asked the practice for Wednesday 16:00; I will confirm when they answer.', waitOn: 'the practice' },
        'Keep today': { text: 'Keeping 15:30 today.', chips: ['Add to the agenda', 'Remind me at 14:45'] },
      },
      free: { text: 'Noted. Anything else about the appointment?', chips: ['Add to the agenda', 'Reschedule'] },
    },
    'thr-backup': {
      summary: 'Exit code 2, disk full; 1.2 GB free',
      chips: ['What is using the disk?', 'Delete old snapshots', 'I will look myself'],
      replies: {
        'What is using the disk?': { text: 'Snapshots older than ninety days take 140 GB; the last three would be enough. The rest is the current data set.', chips: ['Delete old snapshots', 'I will look myself'] },
        'Delete old snapshots': { text: 'Deleted eleven snapshots, 118 GB free, backup restarted at {time}. I will report when it completes.', done: true },
        'I will look myself': { text: 'Fine. The job is paused until you restart it; tell me when.', chips: ['Restarted'] },
        'Restarted': { text: 'Recorded. I will report when it completes.', done: true },
      },
      free: { text: 'Noted. Shall I clean up the snapshots, or leave it to you?', chips: ['Delete old snapshots', 'I will look myself'] },
    },
    // Opened from Inbox Zero when the user wants the tax letter to become a matter of its own.
    'top-tax': {
      summary: '2025 statement due 30 September; the accountant has the documents',
      opening: 'The tax office wants the signed 2025 income statement, the balance sheet and the depreciation schedule by 30 September. Your accountant has all three; I can draft the cover note.',
      chips: ['Ask the accountant', 'Draft the cover note', 'What do they need exactly?'],
      replies: {
        'What do they need exactly?': { text: 'The signed 2025 income statement and balance sheet, plus the depreciation schedule — the letter is quoted above.', chips: ['Ask the accountant', 'Draft the cover note'] },
        'Ask the accountant': { text: 'Asked Frau Zeller for the three documents; she usually answers within a day. This dossier waits on her now — her reply will land here.', waitOn: 'the accountant' },
        'Draft the cover note': { text: 'Drafted: reference to their letter, the three enclosures, your signature line. I will send it with the documents once they arrive.', chips: ['Ask the accountant'] },
      },
      free: { text: 'Noted. The documents are the accountant’s part; the note is mine.', chips: ['Ask the accountant', 'Draft the cover note'] },
    },
    'prj-vat': {
      summary: 'Q3 figures ready; the filing needs your login',
      opening: 'The Q3 VAT return is due on 25 September. From the invoices: turnover 48 200, input tax 2 310, net payable 1 930. I cannot submit it myself — it goes through the tax administration’s portal with your login. Either file it there, about ten minutes with the figures on screen, or start a Cowork session with the Ara connector: Claude then fills the form in your browser while you watch, and asks me for whatever figures it needs.',
      chips: ['Show the figures', 'Open the portal', 'Cowork session', 'Rest until tomorrow'],
      replies: {
        'Show the figures': { text: 'Q3 2026: turnover 48 200, of which 3 100 export and exempt; VAT due 4 240; input tax on purchases 2 310; net payable 1 930. Sources: nine invoices under operations, supplier receipts filed in July and August.', chips: ['Open the portal', 'Cowork session', 'Rest until tomorrow'] },
        'Open the portal': { text: 'The portal is open in your browser; I keep the figures here. Tell me when it is filed.', chips: ['Filed', 'Rest until tomorrow'], note: 'This would open the tax administration’s portal in your browser.' },
        'Cowork session': { text: 'Start Claude Cowork with the Ara connector and ask it to file the Q3 VAT return. It gets the figures from me, drives the browser, and you approve each step; the exchange lands here as events. Tell me when it is filed.', chips: ['Filed', 'Rest until tomorrow'] },
        'Filed': { text: 'Recorded: Q3 VAT return filed at {time}. The cadence rests until 25 December.', done: true },
        'Rest until tomorrow': { text: 'Resting until tomorrow morning; it will be in the 08:00 digest.', later: 'tomorrow' },
      },
      free: { text: 'I can prepare anything about the return, but the filing itself needs your login on the portal — by hand, or through a Cowork session with the Ara connector driving the browser for you.', chips: ['Show the figures', 'Open the portal', 'Cowork session', 'Rest until tomorrow'] },
    },
    'prj-invoices': {
      summary: 'Three drafts ready with the standard terms',
      opening: 'Three invoices are due this month: Müller AG (twelve days, August), Frei Bau AG (retainer) and Studio Nord (final). The drafts are ready with the standard terms.',
      chips: ['Send all three', 'Show the drafts', 'Hold Studio Nord'],
      replies: {
        'Show the drafts': { text: 'Müller AG 4 800 · Frei Bau AG 2 400 · Studio Nord 2 640, total 9 840, all thirty days net.', chips: ['Send all three', 'Hold Studio Nord'] },
        'Send all three': { text: 'Sent three invoices at {time}, total 9 840, filed under operations. The cadence rests until 3 October.', done: true },
        'Hold Studio Nord': { text: 'Holding Studio Nord. Send the other two?', chips: ['Send the other two'] },
        'Send the other two': { text: 'Sent Müller AG and Frei Bau AG at {time}; Studio Nord stays in drafts.', done: true },
      },
      free: { text: 'Noted. Send now, or hold one of them?', chips: ['Send all three', 'Hold Studio Nord'] },
    },
    'prj-insurance': {
      summary: 'Their usual turnaround is three weeks',
      opening: 'Waiting on the insurance office since twelve days; their usual turnaround is three weeks. I will wake this when they answer or on 24 September, whichever comes first.',
      chips: ['Nudge them', 'Fine, wait'],
      replies: {
        'Nudge them': { text: 'I will have the Secretary draft a polite follow-up; you approve it on the sends page before it goes out.', chips: ['Fine, wait'], note: 'The draft would appear in the send queue for approval.' },
        'Fine, wait': { text: 'Waiting.', chips: ['Nudge them'] },
      },
      free: { text: 'Noted; it stays with them.', chips: ['Nudge them'] },
    },
    'prj-brochure': {
      summary: 'Two of three sections done',
      opening: 'The Publisher has the brochure since two days; the translation usually takes three. Nothing for you to do yet.',
      chips: ['Ask for a status', 'Fine, wait'],
      replies: {
        'Ask for a status': { text: 'The Publisher reports two of three sections done; the last one tomorrow morning.', chips: ['Fine, wait'] },
        'Fine, wait': { text: 'Waiting.', chips: ['Ask for a status'] },
      },
      free: { text: 'Noted.', chips: ['Ask for a status'] },
    },
    'prj-garden': { summary: 'Resting, no date', opening: 'Resting since July, no date. Wake it when you want to.', chips: [] },
    'prj-website': { summary: 'Done on 25 August', opening: 'Done on 25 August; the site is live.', chips: [] },
  };

  // Ara in a relationship dossier: her remarks sit in the stream beside the
  // messages; a draft goes into the composer, addressed to the person.
  const RELATIONSHIPS = {
    'rel:Mum': {
      summary: '“{last}”',
      opening: 'Your mother wrote at {arrived}: “{last}” Want a draft, or a reminder at nine?',
      chips: ['Draft a reply', 'Remind me at 09:00', 'Nothing pending'],
      replies: {
        'Draft a reply': { text: 'Here is a draft in the composer — send it or change it.', draft: 'Just up ☕ — I’ll call you in ten minutes.' },
        'Remind me at 09:00': { text: 'I will ping you at 09:00 to call her.', later: 'next' },
        'Nothing pending': { text: 'Settled.', done: true },
      },
      free: { text: 'Noted. A draft, then?', chips: ['Draft a reply'] },
      later: { summary: '“{last}”', opening: 'Your mother asks: “{last}” Your Sunday is free. A yes?', chips: ['Draft a yes', 'Draft a no'], replies: { 'Draft a yes': { text: 'Draft is in the composer.', draft: 'Yes to Sunday lunch — I’ll bring dessert.' }, 'Draft a no': { text: 'Draft is in the composer.', draft: 'Not this Sunday, sorry — next one?' } } },
    },
    'rel:Anna Keller': {
      summary: 'Dinner Friday 19:00 at Anna’s, plus the projector',
      opening: 'Dinner Friday 19:00 at Anna’s, and she asks for the projector. Your Friday evening is free, and the projector is in the office.',
      chips: ['Draft a yes', 'Draft a no', 'Check the agenda'],
      replies: {
        'Draft a yes': { text: 'Draft is in the composer.', draft: 'Yes to Friday, 19:00 — and I’ll bring the projector.' },
        'Draft a no': { text: 'Draft is in the composer.', draft: 'Sorry, Friday doesn’t work — another evening?' },
        'Check the agenda': { text: 'Friday: nothing after 17:00. Saturday morning is blocked.', chips: ['Draft a yes', 'Draft a no'] },
      },
      free: { text: 'Noted. Shall I draft it?', chips: ['Draft a yes', 'Draft a no'] },
    },
    'rel:Beat Frei': {
      summary: 'Clause 7, the liability cap, by tomorrow noon',
      opening: 'Beat asks about clause 7, the liability cap. Your standard position is a cap at the contract value; the draft says so and points to the note in the contract folder.',
      chips: ['Draft the reply', 'Show clause 7', 'I will write myself'],
      replies: {
        'Draft the reply': { text: 'Draft is in the composer.', draft: 'Cap at the contract value, as in our standard terms — I’ll add a short note to clause 7 today.' },
        'Show clause 7': { text: 'Clause 7: liability limited to direct damage, capped at the contract value; gross negligence excluded from the cap.', chips: ['Draft the reply'] },
        'I will write myself': { text: 'Fine — the composer is yours.' },
      },
      free: { text: 'Noted. A draft?', chips: ['Draft the reply'] },
      later: { summary: 'Signing on Monday? 10:00 and 14:00 are free', opening: 'Beat asks: “{last}” Monday 10:00 and 14:00 are free.', chips: ['Propose 10:00', 'Propose 14:00'], replies: { 'Propose 10:00': { text: 'Draft is in the composer.', draft: 'Monday 10:00 works — see you at your office.' }, 'Propose 14:00': { text: 'Draft is in the composer.', draft: 'Monday 14:00 works — see you at your office.' } } },
      later2: { summary: 'Signed NDA wanted before 22:00; it is in your drafts', opening: 'Beat asks: “{last}” The NDA is signed in your drafts folder since Tuesday; I can attach the PDF.', chips: ['Draft with the PDF', 'Show it first'], replies: { 'Draft with the PDF': { text: 'Draft is in the composer, PDF attached.', draft: 'Signed NDA attached. Good night!' }, 'Show it first': { text: 'NDA_FreiBau_2026.pdf, four pages, signed 1 September.', chips: ['Draft with the PDF'] } } },
      later3: { summary: 'A thank-you; no reply needed', opening: '“{last}” — a thank-you, no reply needed.', chips: ['Nothing pending'], replies: { 'Nothing pending': { text: 'Settled.', done: true } } },
    },
    'rel:Luca Meier': {
      summary: 'At the Werdinsel now; fire on at 19:30',
      opening: 'Luca is at the Werdinsel now; nothing in your agenda tonight.',
      chips: ['Draft: on my way', 'Draft: not tonight'],
      replies: {
        'Draft: on my way': { text: 'Draft is in the composer.', draft: 'On my way 🔥' },
        'Draft: not tonight': { text: 'Draft is in the composer.', draft: 'Not tonight — have one for me!' },
      },
      free: { text: 'Noted.', chips: ['Draft: on my way'] },
    },
    'rel:Quartier group': {
      summary: '14 messages about the street party — a grill and a table still missing',
      opening: 'Fourteen messages about the street party on 20 September, nothing addressed to you. Petra’s summary: 16:00 start, drinks, cake, one table and the kids’ corner are covered; still missing a grill and one more table. Ruth adds that the street closes at 14:00.',
      chips: ['Nothing pending', 'Offer the grill', 'Offer a table', 'Who said what?'],
      replies: {
        'Nothing pending': { text: 'Settled.', done: true },
        'Offer the grill': { text: 'Draft is in the composer.', draft: 'We can bring the big grill — where should it go?' },
        'Offer a table': { text: 'Draft is in the composer.', draft: 'One table from us as well.' },
        'Who said what?': { text: 'Petra organises and holds the permit; Marco does drinks and music; Sibylle brings a table; Ruth bakes; Dani runs the kids’ corner. The messages are above.', chips: ['Offer the grill', 'Offer a table', 'Nothing pending'] },
      },
      free: { text: 'Noted.', chips: ['Nothing pending'] },
    },
    'rel:Parking app': {
      summary: 'Card expires Friday night; renew, or let it lapse',
      opening: 'The card on file for the parking app expires Friday night. You used the app twice this year. Renew, or let it lapse?',
      chips: ['Let it lapse', 'Renew it', 'Ask Claude to do it in the browser'],
      replies: {
        'Let it lapse': { text: 'Noted: the subscription ends Friday. The mail leaves the inbox; I will tell you if a parking fine turns up.', done: true },
        'Renew it': { text: 'I cannot change payment details myself. The provider’s portal takes about two minutes with the card from your password manager — or hand it to Claude in the browser.', chips: ['Open the portal', 'Ask Claude to do it in the browser', 'Renewed'], note: 'The real dashboard would show the portal as a labeled link.' },
        'Ask Claude to do it in the browser': { text: 'Start Claude Cowork with the Ara connector and ask it to renew the parking-app card. It asks me for the account details, drives the browser while you watch, and you approve each step. Tell me when it is done.', chips: ['Renewed', 'Let it lapse'] },
        'Open the portal': { text: 'The portal is open in your browser; I keep the details here. Tell me when it is done.', chips: ['Renewed'], note: 'This would open the provider’s portal in your browser.' },
        'Renewed': { text: 'Recorded: card renewed at {time}. Nothing pending.', done: true },
      },
      free: { text: 'I can only prepare this one: the payment change needs your session on the provider’s site, by hand or through a Cowork session.', chips: ['Renew it', 'Let it lapse'] },
    },
    'rel:Tax office': {
      summary: '2025 statement due 30 September',
      opening: 'The tax office wants the 2025 statement by 30 September. Inbox Zero proposes to make this a dossier of its own; the accountant has the documents.',
      chips: ['Make it a dossier', 'Nothing pending'],
      replies: {
        'Make it a dossier': { text: 'Use the proposal in Inbox Zero, or long-press the letter and add it to a new dossier.', chips: ['Nothing pending'] },
        'Nothing pending': { text: 'Settled.', done: true },
      },
      free: { text: 'Noted.', chips: ['Make it a dossier'] },
    },
  };

  // Inbox Zero: per mail sender, the batch's proposal and the chips the user
  // answers with. `resolve` makes the mail leave the inbox; `dossier` opens a
  // topic with the mail as its first event (the letter becomes a matter).
  const INBOX = {
    'Parking app': {
      proposal: 'archive once decided — card expires Friday; lapse or renew',
      chips: ['Let the card lapse', 'Renew the card'],
      replies: {
        'Let the card lapse': { text: 'Noted: the subscription ends Friday. The mail leaves the inbox; I will tell you if a parking fine turns up.', resolve: 'let it lapse' },
        'Renew the card': { text: 'The provider’s portal takes two minutes with the card from your password manager; tell me when it is done and the mail leaves the inbox.', chips: ['Card renewed'], keep: ['Card renewed'], note: 'The real dashboard would show the portal as a labeled link.' },
        'Card renewed': { text: 'Recorded: card renewed at {time}. The mail leaves the inbox.', resolve: 'renewed' },
      },
    },
    'Tax office': {
      proposal: 'open a dossier — 2025 statement due 30 September, importance 5',
      chips: ['Open the tax dossier', 'Just archive the letter'],
      replies: {
        'Open the tax dossier': { text: 'Opened “Tax statement 2025” with the letter as its first event; the letter leaves the inbox and shows the backlink.', dossier: { id: 'top-tax', title: 'Tax statement 2025', body: 'The 2025 statement for the tax office, due 30 September.', comment: 'Letter via ePost, 3 Sept. The accountant has the documents.' } },
        'Just archive the letter': { text: 'Archived. The deadline stays on the tax office’s relationship until you settle it.', resolve: 'archived' },
      },
    },
  };

  const STANDING = {
    'inbox-zero': {
      free: { text: 'Inbox Zero proposes, for every mail still in the inbox, what takes it out; what you deal with elsewhere leaves by itself. Nothing to decide right now beyond the chips above.' },
    },
  };

  root.Dialogues = { TOPICS, RELATIONSHIPS, INBOX, STANDING, THREADS: TOPICS, COMPANIONS: RELATIONSHIPS, days };
})(globalThis);
