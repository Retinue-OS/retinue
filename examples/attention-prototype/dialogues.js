// Canned conversations with Ara, one per item, standing in for the model turns a
// real deployment would run. A chip is a canned prompt: tapping it is the user's
// turn, exactly as on the dashboard. Effects: done (the item is handled), later
// (parked), waitOn (parked on someone else), draft (Ara stages a reply in the
// chat composer — the send press stays the user's), note (something the real
// system would do outside this page). {time} is the current clock.
(function (root) {
  'use strict';
  const THREADS = {
    'thr-quote': {
      opening: 'Quote 2026-031 for Müller AG is drafted: twelve consulting days at the agreed rate, delivery by end of October, thirty days payment. They expect it today by 17:00.',
      chips: ['Show the draft', 'Send it', 'Change the terms'],
      replies: {
        'Show the draft': { text: 'Position 1: analysis and concept, 4 days. Position 2: implementation, 8 days. Travel at cost. Validity 30 days. The PDF is attached to this thread.', chips: ['Send it', 'Change the terms'] },
        'Send it': { text: 'Sent to Müller AG at {time} with the PDF attached, and filed under the project. I will open this again if they answer.', done: true },
        'Change the terms': { text: 'Tell me what to change and I will redraft.', chips: ['Send it'] },
      },
      free: { text: 'Redrafted with that. Ready when you are.', chips: ['Show the draft', 'Send it'] },
    },
    'thr-card': {
      opening: 'The card on file for the parking app expires Friday night. You used the app twice this year. Renew, or let it lapse?',
      chips: ['Let it lapse', 'Renew it', 'Ask Claude to do it in the browser'],
      replies: {
        'Let it lapse': { text: 'Noted: the subscription ends Friday. I will archive this and remind you if a parking fine turns up.', done: true },
        'Renew it': { text: 'I cannot change payment details myself. The provider’s portal takes about two minutes with the card from your password manager — or hand it to Claude in the browser.', chips: ['Open the portal', 'Ask Claude to do it in the browser', 'Renewed'], note: 'The real dashboard would show the portal as a labeled link.' },
        'Ask Claude to do it in the browser': { text: 'Start Claude Cowork with the Ara connector and ask it to renew the parking-app card. It asks me for the account details, drives the browser while you watch, and you approve each step; the exchange lands here as a quiet cowork thread. Tell me when it is done.', chips: ['Renewed', 'Let it lapse'] },
        'Open the portal': { text: 'The portal is open in your browser; I keep the details here. Tell me when it is done.', chips: ['Renewed'], note: 'This would open the provider’s portal in your browser.' },
        'Renewed': { text: 'Recorded: card renewed at {time}. Archived.', done: true },
      },
      free: { text: 'I can only prepare this one: the payment change needs your session on the provider’s site, by hand or through a Cowork session.', chips: ['Renew it', 'Let it lapse'] },
    },
    'thr-physio': {
      opening: 'The practice confirmed 15:30 today. Bus 33 leaves at 15:02 from the corner; leave by 15:00.',
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
      opening: 'The nightly backup on the home server exited with code 2: the disk is full. 1.2 GB free on the backup volume.',
      chips: ['What is using the disk?', 'Delete old snapshots', 'I will look myself'],
      replies: {
        'What is using the disk?': { text: 'Snapshots older than ninety days take 140 GB; the last three would be enough. The rest is the current data set.', chips: ['Delete old snapshots', 'I will look myself'] },
        'Delete old snapshots': { text: 'Deleted eleven snapshots, 118 GB free, backup restarted at {time}. I will report when it completes.', done: true },
        'I will look myself': { text: 'Fine. The job is paused until you restart it; tell me when.', chips: ['Restarted'] },
        'Restarted': { text: 'Recorded. I will report when it completes.', done: true },
      },
      free: { text: 'Noted. Shall I clean up the snapshots, or leave it to you?', chips: ['Delete old snapshots', 'I will look myself'] },
    },
    'thr-tax': {
      opening: 'A letter from the tax office via ePost: they want the 2025 statement by 30 September. I can draft the cover note; the statement itself comes from your accountant.',
      chips: ['What do they need exactly?', 'Ask the accountant', 'Draft the cover note'],
      replies: {
        'What do they need exactly?': { text: 'The signed 2025 income statement and balance sheet, plus the depreciation schedule. Your accountant has all three.', chips: ['Ask the accountant', 'Draft the cover note'] },
        'Ask the accountant': { text: 'Asked Frau Zeller for the three documents; she usually answers within a day. This waits on her now — I will open it again when they arrive.', waitOn: 'the accountant' },
        'Draft the cover note': { text: 'Drafted: reference to their letter, the three enclosures, your signature line. It is in the thread; I will send it with the documents once they arrive.', chips: ['Ask the accountant'] },
      },
      free: { text: 'Noted. The documents are the accountant’s part; the note is mine.', chips: ['Ask the accountant', 'Draft the cover note'] },
    },
    'prj-vat': {
      opening: 'The Q3 VAT return is due on 25 September. From the invoices: turnover 48 200, input tax 2 310, net payable 1 930. I cannot submit it myself — it goes through the tax administration’s portal with your login. Either file it there, about ten minutes with the figures on screen, or start a Cowork session with the Ara connector: Claude then fills the form in your browser while you watch, and asks me for whatever figures it needs.',
      chips: ['Show the figures', 'Open the portal', 'Cowork session', 'Park until tomorrow'],
      replies: {
        'Show the figures': { text: 'Q3 2026: turnover 48 200, of which 3 100 export and exempt; VAT due 4 240; input tax on purchases 2 310; net payable 1 930. Sources: nine invoices under operations, supplier receipts filed in July and August.', chips: ['Open the portal', 'Cowork session', 'Park until tomorrow'] },
        'Open the portal': { text: 'The portal is open in your browser; I keep the figures here. Tell me when it is filed.', chips: ['Filed', 'Park until tomorrow'], note: 'This would open the tax administration’s portal in your browser.' },
        'Cowork session': { text: 'Start Claude Cowork with the Ara connector and ask it to file the Q3 VAT return. It gets the figures from me, drives the browser, and you approve each step; the exchange lands here as a quiet cowork thread. Tell me when it is filed.', chips: ['Filed', 'Park until tomorrow'] },
        'Filed': { text: 'Recorded: Q3 VAT return filed at {time}. The cadence rests until 25 December.', done: true },
        'Park until tomorrow': { text: 'Parked until tomorrow morning; it will be in the 08:00 digest.', later: 'tomorrow' },
      },
      free: { text: 'I can prepare anything about the return, but the filing itself needs your login on the portal — by hand, or through a Cowork session with the Ara connector driving the browser for you.', chips: ['Show the figures', 'Open the portal', 'Cowork session'] },
    },
    'prj-invoices': {
      opening: 'Three invoices are due this month: Müller AG (twelve days, August), Frei Bau AG (retainer) and Studio Nord (final). The drafts are ready with the standard terms.',
      chips: ['Send all three', 'Show the drafts', 'Hold Studio Nord'],
      replies: {
        'Show the drafts': { text: 'Müller AG 4 800 · Frei Bau AG 2 400 · Studio Nord 2 640, total 9 840, all thirty days net.', chips: ['Send all three', 'Hold Studio Nord'] },
        'Send all three': { text: 'Sent three invoices at {time}, total 9 840, filed under operations. Next run on 3 October.', done: true },
        'Hold Studio Nord': { text: 'Holding Studio Nord. Send the other two?', chips: ['Send the other two'] },
        'Send the other two': { text: 'Sent Müller AG and Frei Bau AG at {time}; Studio Nord stays in drafts.', done: true },
      },
      free: { text: 'Noted. Send now, or hold one of them?', chips: ['Send all three', 'Hold Studio Nord'] },
    },
    'prj-insurance': {
      opening: 'Parked on the insurance office since twelve days; their usual turnaround is three weeks. I will wake this when they answer or on 24 September, whichever comes first.',
      chips: ['Nudge them', 'Fine, wait'],
      replies: {
        'Nudge them': { text: 'I will have the Secretary draft a polite follow-up; you approve it on the sends page before it goes out.', chips: ['Fine, wait'], note: 'The draft would appear in the send queue for approval.' },
        'Fine, wait': { text: 'Waiting.', chips: ['Nudge them'] },
      },
      free: { text: 'Noted; it stays parked on them.', chips: ['Nudge them'] },
    },
    'prj-brochure': {
      opening: 'The Publisher has the brochure since two days; the translation usually takes three. Nothing for you to do yet.',
      chips: ['Ask for a status', 'Fine, wait'],
      replies: {
        'Ask for a status': { text: 'The Publisher reports two of three sections done; the last one tomorrow morning.', chips: ['Fine, wait'] },
        'Fine, wait': { text: 'Waiting.', chips: ['Ask for a status'] },
      },
      free: { text: 'Noted.', chips: ['Ask for a status'] },
    },
  };

  // The companion pane of a messenger chat: the chat's own conversation with Ara.
  const COMPANIONS = {
    'chat:Mum': {
      opening: 'Your mother wrote at {arrived}. Want a draft, or a reminder at nine?',
      chips: ['Draft a reply', 'Remind me at 09:00', 'Just mark it read'],
      replies: {
        'Draft a reply': { text: 'Here is a draft in the composer — send it or change it.', draft: 'Just up ☕ — I’ll call you in ten minutes.' },
        'Remind me at 09:00': { text: 'I will ping you at 09:00 to call her.', later: 'next' },
        'Just mark it read': { text: 'Marked read.', done: true },
      },
      free: { text: 'Noted. A draft, then?', chips: ['Draft a reply'] },
      later: { opening: 'Sunday lunch: your Sunday is free. A yes?', chips: ['Draft a yes', 'Draft a no'], replies: { 'Draft a yes': { text: 'Draft is in the composer.', draft: 'Yes to Sunday lunch — I’ll bring dessert.' }, 'Draft a no': { text: 'Draft is in the composer.', draft: 'Not this Sunday, sorry — next one?' } } },
    },
    'chat:Anna Keller': {
      opening: 'Dinner Friday 19:00 at Anna’s, and she asks for the projector. Your Friday evening is free, and the projector is in the office.',
      chips: ['Draft a yes', 'Draft a no', 'Check the agenda'],
      replies: {
        'Draft a yes': { text: 'Draft is in the composer.', draft: 'Yes to Friday, 19:00 — and I’ll bring the projector.' },
        'Draft a no': { text: 'Draft is in the composer.', draft: 'Sorry, Friday doesn’t work — another evening?' },
        'Check the agenda': { text: 'Friday: nothing after 17:00. Saturday morning is blocked.', chips: ['Draft a yes', 'Draft a no'] },
      },
      free: { text: 'Noted. Shall I draft it?', chips: ['Draft a yes', 'Draft a no'] },
    },
    'chat:Beat Frei': {
      opening: 'Beat asks about clause 7, the liability cap. Your standard position is a cap at the contract value; the draft says so and points to the note in the contract folder.',
      chips: ['Draft the reply', 'Show clause 7', 'I will write myself'],
      replies: {
        'Draft the reply': { text: 'Draft is in the composer.', draft: 'Cap at the contract value, as in our standard terms — I’ll add a short note to clause 7 today.' },
        'Show clause 7': { text: 'Clause 7: liability limited to direct damage, capped at the contract value; gross negligence excluded from the cap.', chips: ['Draft the reply'] },
        'I will write myself': { text: 'Fine — the composer is yours.' },
      },
      free: { text: 'Noted. A draft?', chips: ['Draft the reply'] },
      later: { opening: 'Signing on Monday: 10:00 and 14:00 are free.', chips: ['Propose 10:00', 'Propose 14:00'], replies: { 'Propose 10:00': { text: 'Draft is in the composer.', draft: 'Monday 10:00 works — see you at your office.' }, 'Propose 14:00': { text: 'Draft is in the composer.', draft: 'Monday 14:00 works — see you at your office.' } } },
      later2: { opening: 'The NDA is signed in your drafts folder since Tuesday; I can attach the PDF.', chips: ['Draft with the PDF', 'Show it first'], replies: { 'Draft with the PDF': { text: 'Draft is in the composer, PDF attached.', draft: 'Signed NDA attached. Good night!' }, 'Show it first': { text: 'NDA_FreiBau_2026.pdf, four pages, signed 1 September.', chips: ['Draft with the PDF'] } } },
      later3: { opening: 'A thank-you. No reply needed.', chips: ['Mark read'], replies: { 'Mark read': { text: 'Marked read.', done: true } } },
    },
    'chat:Luca Meier': {
      opening: 'Luca is at the Werdinsel now; nothing in your agenda tonight.',
      chips: ['Draft: on my way', 'Draft: not tonight'],
      replies: {
        'Draft: on my way': { text: 'Draft is in the composer.', draft: 'On my way 🔥' },
        'Draft: not tonight': { text: 'Draft is in the composer.', draft: 'Not tonight — have one for me!' },
      },
      free: { text: 'Noted.', chips: ['Draft: on my way'] },
    },
    'chat:Quartier group': {
      opening: 'Fourteen messages about the street party on 20 September; nothing addressed to you. They still need a grill and two tables.',
      chips: ['Mark read', 'Offer the grill'],
      replies: {
        'Mark read': { text: 'Marked read.', done: true },
        'Offer the grill': { text: 'Draft is in the composer.', draft: 'We can bring the big grill — where should it go?' },
      },
      free: { text: 'Noted.', chips: ['Mark read'] },
    },
  };

  root.Dialogues = { THREADS, COMPANIONS };
})(globalThis);
