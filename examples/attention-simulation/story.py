"""The example day: contacts, messages, threads and projects, the script of
beats, and Ara's canned dialogues — the prototype's data
(examples/attention-prototype/backends.js, simulation.js, dialogues.js), now
addressed the way the real gateway addresses things: chats by channel and
key, threads by the id the gateway assigns when they are opened, projects by
their URI.

Times are minutes since 00:00 of the simulated day; negative values are
yesterday. Everything here is fictional.
"""
from __future__ import annotations

DAY = 1440


def t(h: int, m: int = 0) -> int:
    return h * 60 + m


def days(d: int, h: int = 0, m: int = 0) -> int:
    return d * DAY + h * 60 + m


# -- who writes ---------------------------------------------------------------
# `chat` is the key the channel gateway records (kb:chat); the chat's id on
# the dashboard is "<channel>:<key>" (no account: the mock ledger predates
# kb:account, like real history does).
CONTACTS = {
    "Anna Keller":    {"channel": "whatsapp", "chat": "+41791000001", "sphere": "friends"},
    "Beat Frei":      {"channel": "signal",   "chat": "+41791000002", "sphere": "customers"},
    "Mum":            {"channel": "whatsapp", "chat": "+41791000003", "sphere": "family"},
    "Luca Meier":     {"channel": "telegram", "chat": "123456789",    "sphere": "friends"},
    "Quartier group": {"channel": "whatsapp", "chat": "120363000000000001@g.us", "sphere": "friends", "group": True},
}


def chat_id(name: str) -> str:
    c = CONTACTS[name]
    return f"{c['channel']}:{c['chat']}"


# The attention profile the day starts with: importance priors per sender
# and the senders' spheres (what a deployment learns from corrections and the
# contact groups; here given, so the day is the brief's).
PRIORS = {"Beat Frei": 4, "Anna Keller": 3, "Mum": 3, "Luca Meier": 3, "Quartier group": 1}
SPHERES = {name: c["sphere"] for name, c in CONTACTS.items()}

# -- messages -------------------------------------------------------------------
# `triage` is what the Secretary's triage turn classifies for a whitelisted
# sender — importance, an extracted deadline, the kind — and rides on the rail
# as the message's `attention`. `stage` picks the companion dialogue.
MESSAGES = [
    {"at": days(-1, 20, 12), "chat": "Quartier group", "from": "Petra",   "text": "Street party is on for 20 September — who is in?", "triage": {"importance": 1, "kind": "group chatter"}},
    {"at": days(-1, 20, 14), "chat": "Quartier group", "from": "Marco",   "text": "In. We can do the drinks again."},
    {"at": days(-1, 20, 15), "chat": "Quartier group", "from": "Sibylle", "text": "Same as last year, from 16:00?"},
    {"at": days(-1, 20, 17), "chat": "Quartier group", "from": "Petra",   "text": "Yes, 16:00 until whenever. The permit is confirmed."},
    {"at": days(-1, 20, 20), "chat": "Quartier group", "from": "Dani",    "text": "Do we have a grill this time? Ours died."},
    {"at": days(-1, 20, 21), "chat": "Quartier group", "from": "Marco",   "text": "Ha. No grill here."},
    {"at": days(-1, 20, 24), "chat": "Quartier group", "from": "Sibylle", "text": "I can bring one table."},
    {"at": days(-1, 20, 25), "chat": "Quartier group", "from": "Petra",   "text": "So we need two tables and a grill. Anyone?"},
    {"at": days(-1, 20, 31), "chat": "Quartier group", "from": "Ruth",    "text": "I will bake, as always."},
    {"at": days(-1, 20, 33), "chat": "Quartier group", "from": "Dani",    "text": "Kids’ corner: I will organise chalk and the water thing."},
    {"at": days(-1, 20, 40), "chat": "Quartier group", "from": "Marco",   "text": "Music: I bring the speaker, playlist suggestions welcome."},
    {"at": days(-1, 20, 41), "chat": "Quartier group", "from": "Sibylle", "text": "Not the same playlist as last year 😅"},
    {"at": days(-1, 20, 55), "chat": "Quartier group", "from": "Petra",   "text": "Summary: 20 Sept, 16:00. Drinks Marco, cake Ruth, one table Sibylle, kids Dani. Still missing: a grill and one more table."},
    {"at": days(-1, 21, 10), "chat": "Quartier group", "from": "Ruth",    "text": "Reminder for the newcomers: the street is closed from 14:00."},
    {"at": t(6, 40),  "chat": "Mum",         "text": "Call me when you are up ☕", "triage": {"importance": 3, "due": t(9, 0), "kind": "family note"}, "stage": "base"},
    {"at": t(8, 40),  "chat": "Anna Keller", "text": "Dinner on Friday? 19:00 at ours", "triage": {"importance": 3, "due": days(1, 18, 0), "kind": "invitation"}, "stage": "base"},
    {"at": t(9, 15),  "chat": "Anna Keller", "text": "Also: could you bring the projector?", "triage": {"importance": 3, "due": days(1, 18, 0), "kind": "invitation"}, "stage": "base"},
    {"at": t(10, 5),  "chat": "Beat Frei",   "text": "Question on clause 7 of the contract — I need your view by tomorrow noon", "triage": {"importance": 4, "due": days(1, 12, 0), "kind": "customer request", "tags": ["finance"]}, "stage": "base"},
    {"at": t(14, 20), "chat": "Beat Frei",   "text": "Thanks. One more: can we sign on Monday?", "triage": {"importance": 4, "due": days(4, 12, 0), "kind": "customer request"}, "stage": "later"},
    {"at": t(15, 30), "chat": "Mum",         "text": "Are you coming for lunch on Sunday?", "triage": {"importance": 3, "due": days(3, 12, 0), "kind": "family note"}, "stage": "later"},
    {"at": t(19, 5),  "chat": "Luca Meier",  "text": "We are at the Werdinsel — coming? Fire is on at 19:30", "triage": {"importance": 4, "due": t(19, 30), "kind": "invitation"}, "stage": "base"},
    {"at": t(19, 40), "chat": "Beat Frei",   "text": "Could you send the signed NDA tonight? Their lawyer wants it before 22:00", "triage": {"importance": 4, "due": t(22, 0), "kind": "customer request"}, "stage": "later2"},
    {"at": t(23, 15), "chat": "Beat Frei",   "text": "Got it, thanks — good night", "triage": {"importance": 2, "kind": "acknowledgement"}, "stage": "later3"},
]

# -- threads agents open ---------------------------------------------------------
THREADS = [
    {"at": days(-1, 16, 30), "id": "thr-quote",  "title": "Quote for Müller AG", "agent": "Secretary", "attention": {"importance": 4, "sphere": "customers", "tags": ["finance"], "due": t(17, 0), "kind": "customer request"}},
    {"at": days(-1, 20, 15), "id": "thr-card",   "title": "Card renewal for the parking app", "agent": "Secretary", "attention": {"importance": 1, "sphere": "admin", "due": days(1, 23, 59), "kind": "admin chore"}},
    {"at": t(7, 40),  "id": "thr-physio", "title": "Physio today 15:30 — leave by 15:00", "agent": "Ara", "attention": {"importance": 4, "sphere": "health", "due": t(15, 30), "kind": "appointment"}},
    {"at": t(10, 40), "id": "thr-backup", "title": "Backup job failed on the home server", "agent": "Ara", "attention": {"importance": 5, "sphere": "system", "kind": "system alert", "critical": True}},
    {"at": t(14, 45), "id": "thr-tax",    "title": "Tax office asks for the 2025 statement", "agent": "Secretary", "attention": {"importance": 5, "sphere": "admin", "tags": ["finance"], "due": days(27, 17, 0), "kind": "tax filing"}},
]

# -- projects (the life store's rows) ------------------------------------------------
PROJECTS = [
    {"id": "urn:retinue:project:vat-q3",       "story": "prj-vat",       "title": "Quarterly VAT return", "sphere": "admin", "tags": ["finance"], "importance": 4, "expected": days(22, 17, 0), "remind_before": "14d", "actor": "you", "kind": "tax filing", "next": "File the Q3 return on the portal"},
    {"id": "urn:retinue:project:therapy-cover", "story": "prj-insurance", "title": "Insurance decision on the therapy", "sphere": "health", "tags": ["admin"], "importance": 4, "actor": "urn:retinue:actor:insurance-office", "since": days(-12), "kind": "admin chore", "next": "Wait for their decision"},
    {"id": "urn:retinue:project:brochure",     "story": "prj-brochure",  "title": "Brochure translation", "sphere": "customers", "importance": 3, "actor": "urn:retinue:actor:publisher", "since": days(-2), "kind": "customer request", "next": "Publisher translates the three sections"},
    # Paused with a cadence: recurring-projects wakes it at 08:00 (a thread
    # with the reminder, and the project turns active in the store).
    {"id": "urn:retinue:project:invoice-run",  "story": "prj-invoices",  "title": "Monthly invoice run", "sphere": "customers", "tags": ["admin", "finance"], "importance": 4, "expected": t(18, 0), "remind_before": "1d", "actor": "you", "paused": True, "wake_at": t(8, 0), "kind": "invoice run", "next": "Send this month's three invoices"},
]

# -- the script ---------------------------------------------------------------------
# who: narrator | you. An action is what you do, run through the real API; the
# text is what you are doing and why. The engine's side — arrivals, digests,
# pushes, holds — is narrated by the runner from the gateway's answers.
SCRIPT = [
    {"at": t(0, 0),   "who": "narrator", "text": "Midnight. You are asleep; the mode is Off, so only a critical alert could ring before 07:00. Open from yesterday: a customer quote due at 17:00, the quarterly VAT return, a card renewal, a chatty group, and two projects waiting on other people."},
    {"at": t(6, 40),  "who": "narrator", "text": "Your mother writes. Family is admitted in Off only at critical level, so the message is held; the morning digest would carry it."},
    {"at": t(7, 0),   "who": "you", "text": "You are up. Home mode until 08:00: family and health may break through."},
    {"at": t(7, 5),   "who": "you", "action": {"type": "pull", "id": "chat:Mum"}, "text": "Coffee, phone. Held shows one item. You open it and pull your mother’s message out before any digest: the system proposes a rhythm, it never locks the door."},
    {"at": t(7, 8),   "who": "you", "action": {"type": "reply", "id": "chat:Mum", "text": "Just up ☕ — I’ll call you in ten minutes."}, "text": "The chat opens with her message and a composer. You reply, then call her. Handled."},
    {"at": t(7, 40),  "who": "narrator", "text": "Ara confirms the physio appointment for 15:30. Importance 4 with a two-hour lead time: eight hours out it is merely active, so it waits for the 08:00 digest instead of ringing."},
    {"at": t(8, 0),   "who": "narrator", "text": "The morning digest lands as one push and Deep work begins: nothing but critical rings before 12:00. The Müller quote is time-sensitive and tops Next; Deep work admits no customers, so it did not ring, but it is the obvious place to start."},
    {"at": t(8, 20),  "who": "you", "text": "You work on the quote. This is the nudge working as intended: the most urgent important thing, visible, not shouting."},
    {"at": t(8, 40),  "who": "narrator", "text": "Anna asks about dinner on Friday. Friends are not admitted in Deep work; the invitation is held for 12:00."},
    {"at": t(9, 15),  "who": "narrator", "text": "Anna writes again. The repeat policy for friends is off and her message classifies the same, so nothing escalates; a fixed five-minute window would have rewarded impatience."},
    {"at": t(10, 5),  "who": "narrator", "text": "Beat Frei, a customer, wants a view on a contract clause by tomorrow noon. Time-sensitive, but Deep work admits no customers: held for 12:00. The half-hourly sweep re-checks it every thirty minutes; a permit for Beat would have let it ring."},
    {"at": t(10, 40), "who": "narrator", "text": "The home server’s backup failed. Critical is declared, never derived, and it rings in every mode."},
    {"at": t(10, 42), "who": "you", "action": {"type": "later", "id": "thr-backup", "when": "next"}, "text": "You glance at it and tap Later: the alert waits for the 12:00 breakpoint. Even a critical item bends to you."},
    {"at": t(11, 30), "who": "you", "action": {"type": "chip", "id": "thr-quote", "label": "Send it"}, "text": "You open the thread: Ara has the draft ready and offers Show, Send, Change. You tap Send it; she sends the PDF to Müller AG and files it. Done, five and a half hours before the deadline."},
    {"at": t(12, 0),  "who": "narrator", "text": "Midday digest: one push instead of four. Open mode for lunch admits every sphere, so Now shows exactly what may interrupt: the contract clause, the invoice run and the snoozed backup alert. Anna’s dinner, merely active, sits in Next."},
    {"at": t(12, 10), "who": "you", "action": {"type": "reply", "id": "chat:Anna Keller", "text": "Yes to Friday, 19:00 — and I’ll bring the projector."}, "text": "You are in the mood for people, not contracts: you open Anna’s chat, take Ara’s drafted yes from the composer and send it. The clause keeps its place; nothing nags."},
    {"at": t(12, 40), "who": "you", "action": {"type": "chip", "id": "thr-backup", "label": "Delete old snapshots"}, "text": "In the backup thread Ara names the culprit: old snapshots. One tap and she deletes them and restarts the job. Done."},
    {"at": t(13, 0),  "who": "narrator", "text": "Work mode until 17:00: customers, admin and health may break through."},
    {"at": t(13, 5),  "who": "you", "action": {"type": "reply", "id": "chat:Beat Frei", "text": "Cap at the contract value, as in our standard terms — I’ll add a short note to clause 7 today."}, "text": "Beat’s chat: Ara’s pane recalls your standard position on the liability cap and drafts the reply; you send it."},
    {"at": t(13, 30), "who": "narrator", "text": "The sweep finds the physio appointment inside its two-hour lead time: it climbs to time-sensitive, health is admitted in Work, and it pushes — two hours ahead, as the lead time intended."},
    {"at": t(13, 40), "who": "you", "action": {"type": "chip", "id": "prj-invoices", "label": "Send all three"}, "text": "The invoice run: Ara has three drafts ready; you send all three. Done."},
    {"at": t(14, 20), "who": "narrator", "text": "Beat asks about signing on Monday. Admitted in Work, but only active: it waits for the 17:00 digest. If you want it earlier, Held is one tap away."},
    {"at": t(14, 45), "who": "narrator", "text": "The Secretary triaged a letter from the tax office: statement due 30 September, importance 5, lead time two weeks. Twenty-seven days out it is active, so it waits for the digest too."},
    {"at": t(14, 58), "who": "you", "action": {"type": "doIt", "id": "thr-physio"}, "text": "You mark the appointment as handled and pack up."},
    {"at": t(15, 0),  "who": "you", "action": {"type": "mode", "id": "off"}, "text": "You leave for physio and switch the mode to Off by hand. The schedule is suspended until you release it."},
    {"at": t(15, 30), "who": "narrator", "text": "Your mother asks about Sunday lunch. Off: held."},
    {"at": t(16, 15), "who": "you", "action": {"type": "mode", "id": None}, "text": "Back at the desk you release the mode to the schedule. Work resumes; the change is a breakpoint, so what was held meanwhile arrives as a small digest."},
    {"at": t(16, 28), "who": "you", "action": {"type": "say", "id": "prj-vat", "text": "Can you file the VAT return for me?"}, "text": "The VAT return has sat in Next all day. You open its project and ask Ara to file it. She has the figures ready but cannot submit: the portal needs your login. By hand in ten minutes, or a Cowork session with the Ara connector, where Claude fills the form in your browser and asks her for the figures."},
    {"at": t(16, 30), "who": "you", "action": {"type": "lead", "id": "prj-vat", "lead": "4w"}, "text": "Two weeks is too short a lead for a filing like this, so you correct the lead time to four weeks in the details. The profile learns it for every tax filing, and with the deadline now inside the lead time the return climbs to time-sensitive and moves to Now."},
    {"at": t(16, 35), "who": "you", "action": {"type": "chip", "id": "prj-vat", "label": "Park until tomorrow"}, "text": "You look through the figures and park it until tomorrow morning."},
    {"at": t(17, 0),  "who": "narrator", "text": "Nothing was held, so the 17:00 breakpoint passes without a digest: an open hour before Social."},
    {"at": t(17, 10), "who": "you", "action": {"type": "reply", "id": "chat:Beat Frei", "text": "Monday 10:00 works — see you at your office."}, "text": "Monday signing: Ara’s pane shows the free slots; you propose 10:00."},
    {"at": t(18, 0),  "who": "narrator", "text": "Social mode: friends and family may break through; customers wait for 21:00."},
    {"at": t(19, 5),  "who": "narrator", "text": "Luca: fire on at the Werdinsel at 19:30. Time-sensitive and friends are admitted, so it rings. The same message at 10:00 would have waited for a digest."},
    {"at": t(19, 8),  "who": "you", "action": {"type": "reply", "id": "chat:Luca Meier", "text": "On my way 🔥"}, "text": "On your way."},
    {"at": t(19, 40), "who": "narrator", "text": "Beat needs a signed NDA before 22:00. Time-sensitive, but Social does not admit customers: held for 21:00 — unless Beat holds a permit. Try it yourself: open Held, tap the item, and let Beat interrupt in Social."},
    {"at": t(20, 30), "who": "you", "action": {"type": "permit", "sender": "Beat Frei", "mode": "social", "on": True}, "text": "You decide Beat may interrupt you in Social. A delivery correction: the permit is stored per mode, importance untouched. The NDA request moves to Now and pushes."},
    {"at": t(20, 45), "who": "you", "action": {"type": "reply", "id": "chat:Beat Frei", "text": "Signed NDA attached. Good night!"}, "text": "Ara attaches the signed NDA from your drafts; you send it."},
    {"at": t(21, 0),  "who": "narrator", "text": "The 21:00 breakpoint passes without a digest too. The card renewal is still passive: it crosses into the last third of its lead time at midnight, climbs to active, and appears in tomorrow’s morning digest — never the bell."},
    {"at": t(21, 5),  "who": "you", "action": {"type": "reply", "id": "chat:Mum", "text": "Yes to Sunday lunch — I’ll bring dessert."}, "text": "Sunday lunch: yes."},
    {"at": t(22, 0),  "who": "narrator", "text": "Off. Whatever arrives now sleeps until 08:00."},
    {"at": t(23, 15), "who": "narrator", "text": "Beat says thanks. Importance 2, no deadline: passive, listed, silent."},
    {"at": t(23, 59), "who": "narrator", "summary": True, "text": "Midnight. The day in numbers is in the panel: how often the bell rang, how much waited for a digest, and what the profile learned from your two corrections."},
]

# -- Ara's canned turns ---------------------------------------------------------------
# One dialogue per thread; a chip is a canned prompt and tapping it is the
# user's turn, exactly as on the dashboard. Effects: done (the item is
# handled), later (parked), wait_on (parked on someone else), draft (Ara
# stages a reply in the chat's composer — the send press stays the user's),
# note (what the real system would do outside the page). {time} is the clock.
THREAD_DIALOGUES = {
    "thr-quote": {
        "opening": "Quote 2026-031 for Müller AG is drafted: twelve consulting days at the agreed rate, delivery by end of October, thirty days payment. They expect it today by 17:00.",
        "chips": ["Show the draft", "Send it", "Change the terms"],
        "replies": {
            "Show the draft": {"text": "Position 1: analysis and concept, 4 days. Position 2: implementation, 8 days. Travel at cost. Validity 30 days. The PDF is attached to this thread.", "chips": ["Send it", "Change the terms"]},
            "Send it": {"text": "Sent to Müller AG at {time} with the PDF attached, and filed under the project. I will open this again if they answer.", "done": True},
            "Change the terms": {"text": "Tell me what to change and I will redraft.", "chips": ["Send it"]},
        },
        "free": {"text": "Redrafted with that. Ready when you are.", "chips": ["Show the draft", "Send it"]},
    },
    "thr-card": {
        "opening": "The card on file for the parking app expires Friday night. You used the app twice this year. Renew, or let it lapse?",
        "chips": ["Let it lapse", "Renew it", "Ask Claude to do it in the browser"],
        "replies": {
            "Let it lapse": {"text": "Noted: the subscription ends Friday. I will archive this and remind you if a parking fine turns up.", "done": True},
            "Renew it": {"text": "I cannot change payment details myself. The provider’s portal takes about two minutes with the card from your password manager — or hand it to Claude in the browser.", "chips": ["Ask Claude to do it in the browser", "Renewed"]},
            "Ask Claude to do it in the browser": {"text": "Start Claude Cowork with the Ara connector and ask it to renew the parking-app card. It asks me for the account details, drives the browser while you watch, and you approve each step; the exchange lands here as a quiet cowork thread. Tell me when it is done.", "chips": ["Renewed", "Let it lapse"]},
            "Renewed": {"text": "Recorded: card renewed at {time}. Archived.", "done": True},
        },
        "free": {"text": "I can only prepare this one: the payment change needs your session on the provider’s site, by hand or through a Cowork session.", "chips": ["Renew it", "Let it lapse"]},
    },
    "thr-physio": {
        "opening": "The practice confirmed 15:30 today. Bus 33 leaves at 15:02 from the corner; leave by 15:00.",
        "chips": ["Add to the agenda", "Remind me at 14:45", "Reschedule"],
        "replies": {
            "Add to the agenda": {"text": "In your agenda: physio 15:30–16:15, bus at 15:02. I will ping you at 14:45.", "chips": ["Reschedule"]},
            "Remind me at 14:45": {"text": "I will ping you at 14:45.", "chips": ["Add to the agenda"]},
            "Reschedule": {"text": "The practice offers Monday 10:00 or Wednesday 16:00. Which one?", "chips": ["Monday 10:00", "Wednesday 16:00", "Keep today"]},
            "Monday 10:00": {"text": "Asked the practice for Monday 10:00; I will confirm when they answer.", "wait_on": "the practice"},
            "Wednesday 16:00": {"text": "Asked the practice for Wednesday 16:00; I will confirm when they answer.", "wait_on": "the practice"},
            "Keep today": {"text": "Keeping 15:30 today.", "chips": ["Add to the agenda", "Remind me at 14:45"]},
        },
        "free": {"text": "Noted. Anything else about the appointment?", "chips": ["Add to the agenda", "Reschedule"]},
    },
    "thr-backup": {
        "opening": "The nightly backup on the home server exited with code 2: the disk is full. 1.2 GB free on the backup volume.",
        "chips": ["What is using the disk?", "Delete old snapshots", "I will look myself"],
        "replies": {
            "What is using the disk?": {"text": "Snapshots older than ninety days take 140 GB; the last three would be enough. The rest is the current data set.", "chips": ["Delete old snapshots", "I will look myself"]},
            "Delete old snapshots": {"text": "Deleted eleven snapshots, 118 GB free, backup restarted at {time}. I will report when it completes.", "done": True},
            "I will look myself": {"text": "Fine. The job is paused until you restart it; tell me when.", "chips": ["Restarted"]},
            "Restarted": {"text": "Recorded. I will report when it completes.", "done": True},
        },
        "free": {"text": "Noted. Shall I clean up the snapshots, or leave it to you?", "chips": ["Delete old snapshots", "I will look myself"]},
    },
    "thr-tax": {
        "opening": "A letter from the tax office via ePost: they want the 2025 statement by 30 September. I can draft the cover note; the statement itself comes from your accountant.",
        "chips": ["What do they need exactly?", "Ask the accountant", "Draft the cover note"],
        "replies": {
            "What do they need exactly?": {"text": "The signed 2025 income statement and balance sheet, plus the depreciation schedule. Your accountant has all three.", "chips": ["Ask the accountant", "Draft the cover note"]},
            "Ask the accountant": {"text": "Asked the accountant for the three documents; she usually answers within a day. This waits on her now — I will open it again when they arrive.", "wait_on": "the accountant"},
            "Draft the cover note": {"text": "Drafted: reference to their letter, the three enclosures, your signature line. It is in the thread; I will send it with the documents once they arrive.", "chips": ["Ask the accountant"]},
        },
        "free": {"text": "Noted. The documents are the accountant’s part; the note is mine.", "chips": ["Ask the accountant", "Draft the cover note"]},
    },
    "prj-vat": {
        "opening": "The Q3 VAT return is due on 25 September. From the invoices: turnover 48 200, input tax 2 310, net payable 1 930. I cannot submit it myself — it goes through the tax administration’s portal with your login. Either file it there, about ten minutes with the figures on screen, or start a Cowork session with the Ara connector: Claude then fills the form in your browser while you watch, and asks me for whatever figures it needs.",
        "chips": ["Show the figures", "Cowork session", "Park until tomorrow"],
        "replies": {
            "Show the figures": {"text": "Q3 2026: turnover 48 200, of which 3 100 export and exempt; VAT due 4 240; input tax on purchases 2 310; net payable 1 930. Sources: nine invoices under operations, supplier receipts filed in July and August.", "chips": ["Cowork session", "Park until tomorrow"]},
            "Cowork session": {"text": "Start Claude Cowork with the Ara connector and ask it to file the Q3 VAT return. It gets the figures from me, drives the browser, and you approve each step; the exchange lands here as a quiet cowork thread. Tell me when it is filed.", "chips": ["Filed", "Park until tomorrow"]},
            "Filed": {"text": "Recorded: Q3 VAT return filed at {time}. The cadence rests until 25 December.", "done": True},
            "Park until tomorrow": {"text": "Parked until tomorrow morning; it will be in the 08:00 digest.", "later": "tomorrow"},
        },
        "free": {"text": "I can prepare anything about the return, but the filing itself needs your login on the portal — by hand, or through a Cowork session with the Ara connector driving the browser for you.", "chips": ["Show the figures", "Cowork session"]},
    },
    "prj-invoices": {
        "opening": "Three invoices are due this month: Müller AG (twelve days, August), Frei Bau AG (retainer) and Studio Nord (final). The drafts are ready with the standard terms.",
        "chips": ["Send all three", "Show the drafts", "Hold Studio Nord"],
        "replies": {
            "Show the drafts": {"text": "Müller AG 4 800 · Frei Bau AG 2 400 · Studio Nord 2 640, total 9 840, all thirty days net.", "chips": ["Send all three", "Hold Studio Nord"]},
            "Send all three": {"text": "Sent three invoices at {time}, total 9 840, filed under operations. Next run on 3 October.", "done": True},
            "Hold Studio Nord": {"text": "Holding Studio Nord. Send the other two?", "chips": ["Send the other two"]},
            "Send the other two": {"text": "Sent Müller AG and Frei Bau AG at {time}; Studio Nord stays in drafts.", "done": True},
        },
        "free": {"text": "Noted. Send now, or hold one of them?", "chips": ["Send all three", "Hold Studio Nord"]},
    },
    "prj-insurance": {
        "opening": "Parked on the insurance office since twelve days; their usual turnaround is three weeks. I will wake this when they answer or on 24 September, whichever comes first.",
        "chips": ["Nudge them", "Fine, wait"],
        "replies": {
            "Nudge them": {"text": "I will have the Secretary draft a polite follow-up; you approve it on the sends page before it goes out.", "chips": ["Fine, wait"]},
            "Fine, wait": {"text": "Waiting.", "chips": ["Nudge them"]},
        },
        "free": {"text": "Noted; it stays parked on them.", "chips": ["Nudge them"]},
    },
    "prj-brochure": {
        "opening": "The Publisher has the brochure since two days; the translation usually takes three. Nothing for you to do yet.",
        "chips": ["Ask for a status", "Fine, wait"],
        "replies": {
            "Ask for a status": {"text": "The Publisher reports two of three sections done; the last one tomorrow morning.", "chips": ["Fine, wait"]},
            "Fine, wait": {"text": "Waiting.", "chips": ["Ask for a status"]},
        },
        "free": {"text": "Noted.", "chips": ["Ask for a status"]},
    },
}

# The companion pane of a messenger chat: the chat's own conversation with
# Ara, by the stage of the conversation (the sender's latest message).
COMPANION_DIALOGUES = {
    "Mum": {
        "base": {"opening": "Your mother wrote at {arrived}: “{last}” Want a draft, or a reminder at nine?", "chips": ["Draft a reply", "Remind me at 09:00", "Just mark it read"],
                 "replies": {"Draft a reply": {"text": "Here is a draft in the composer — send it or change it.", "draft": "Just up ☕ — I’ll call you in ten minutes."},
                             "Remind me at 09:00": {"text": "I will ping you at 09:00 to call her.", "later": "next"},
                             "Just mark it read": {"text": "Marked read.", "done": True}},
                 "free": {"text": "Noted. A draft, then?", "chips": ["Draft a reply"]}},
        "later": {"opening": "Your mother asks: “{last}” Your Sunday is free. A yes?", "chips": ["Draft a yes", "Draft a no"],
                  "replies": {"Draft a yes": {"text": "Draft is in the composer.", "draft": "Yes to Sunday lunch — I’ll bring dessert."},
                              "Draft a no": {"text": "Draft is in the composer.", "draft": "Not this Sunday, sorry — next one?"}},
                  "free": {"text": "Noted. A draft?", "chips": ["Draft a yes"]}},
    },
    "Anna Keller": {
        "base": {"opening": "Dinner Friday 19:00 at Anna’s, and she asks for the projector. Your Friday evening is free, and the projector is in the office.", "chips": ["Draft a yes", "Draft a no", "Check the agenda"],
                 "replies": {"Draft a yes": {"text": "Draft is in the composer.", "draft": "Yes to Friday, 19:00 — and I’ll bring the projector."},
                             "Draft a no": {"text": "Draft is in the composer.", "draft": "Sorry, Friday doesn’t work — another evening?"},
                             "Check the agenda": {"text": "Friday: nothing after 17:00. Saturday morning is blocked.", "chips": ["Draft a yes", "Draft a no"]}},
                 "free": {"text": "Noted. Shall I draft it?", "chips": ["Draft a yes", "Draft a no"]}},
    },
    "Beat Frei": {
        "base": {"opening": "Beat asks about clause 7, the liability cap. Your standard position is a cap at the contract value; the draft says so and points to the note in the contract folder.", "chips": ["Draft the reply", "Show clause 7", "I will write myself"],
                 "replies": {"Draft the reply": {"text": "Draft is in the composer.", "draft": "Cap at the contract value, as in our standard terms — I’ll add a short note to clause 7 today."},
                             "Show clause 7": {"text": "Clause 7: liability limited to direct damage, capped at the contract value; gross negligence excluded from the cap.", "chips": ["Draft the reply"]},
                             "I will write myself": {"text": "Fine — the composer is yours."}},
                 "free": {"text": "Noted. A draft?", "chips": ["Draft the reply"]}},
        "later": {"opening": "Beat asks: “{last}” Monday 10:00 and 14:00 are free.", "chips": ["Propose 10:00", "Propose 14:00"],
                  "replies": {"Propose 10:00": {"text": "Draft is in the composer.", "draft": "Monday 10:00 works — see you at your office."},
                              "Propose 14:00": {"text": "Draft is in the composer.", "draft": "Monday 14:00 works — see you at your office."}},
                  "free": {"text": "Noted. Which slot?", "chips": ["Propose 10:00", "Propose 14:00"]}},
        "later2": {"opening": "Beat asks: “{last}” The NDA is signed in your drafts folder since Tuesday; I can attach the PDF.", "chips": ["Draft with the PDF", "Show it first"],
                   "replies": {"Draft with the PDF": {"text": "Draft is in the composer, PDF attached.", "draft": "Signed NDA attached. Good night!"},
                               "Show it first": {"text": "NDA_FreiBau_2026.pdf, four pages, signed 1 September.", "chips": ["Draft with the PDF"]}},
                   "free": {"text": "Noted.", "chips": ["Draft with the PDF"]}},
        "later3": {"opening": "“{last}” — a thank-you, no reply needed.", "chips": ["Mark read"],
                   "replies": {"Mark read": {"text": "Marked read.", "done": True}},
                   "free": {"text": "Noted.", "chips": ["Mark read"]}},
    },
    "Luca Meier": {
        "base": {"opening": "Luca is at the Werdinsel now; nothing in your agenda tonight.", "chips": ["Draft: on my way", "Draft: not tonight"],
                 "replies": {"Draft: on my way": {"text": "Draft is in the composer.", "draft": "On my way 🔥"},
                             "Draft: not tonight": {"text": "Draft is in the composer.", "draft": "Not tonight — have one for me!"}},
                 "free": {"text": "Noted.", "chips": ["Draft: on my way"]}},
    },
    "Quartier group": {
        "base": {"opening": "Fourteen messages about the street party on 20 September, nothing addressed to you. Petra’s summary: 16:00 start, drinks, cake, one table and the kids’ corner are covered; still missing a grill and one more table. Ruth adds that the street closes at 14:00.", "chips": ["Mark read", "Offer the grill", "Offer a table", "Who said what?"],
                 "replies": {"Mark read": {"text": "Marked read.", "done": True},
                             "Offer the grill": {"text": "Draft is in the composer.", "draft": "We can bring the big grill — where should it go?"},
                             "Offer a table": {"text": "Draft is in the composer.", "draft": "One table from us as well."},
                             "Who said what?": {"text": "Petra organises and holds the permit; Marco does drinks and music; Sibylle brings a table; Ruth bakes; Dani runs the kids’ corner. The full thread is on the Chat tab.", "chips": ["Offer the grill", "Offer a table", "Mark read"]}},
                 "free": {"text": "Noted.", "chips": ["Mark read"]}},
    },
}


def chips_markup(chips: list[str]) -> str:
    """The dashboard's own chip syntax (the dashboard-composing skill)."""
    return " · ".join(f"[[chip: {c}]]" for c in chips)
