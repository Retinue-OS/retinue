# The notice board

*Design and rules for `scripts/notices.py`. The digest is in `CLAUDE.md`,
"Notice board"; the ring guard it relies on is `scripts/ring_guard.py`.*

## Why

A lot of mail announces rather than says. "A new document is in your
portal." "Three hours were logged for Monday." "Your statement is ready." One
such mail is noise in the chats. Twenty of them are a to-do list the user works
through now and then ("read what the insurer uploaded", "check this month's
hours"). Neither a thread nor a project fits:

- A **thread** only grows. It cannot fold ten notices into one line, and there
  is no way to mark a batch as dealt with.
- A **project** sits in the attention list and keeps asking for attention,
  which is the opposite of what a notice that can wait wants.

The board holds the notices, groups them by **source and account**, shows each
group as one line ("Example Insurance (12345): 2 waiting since 24 Nov"), and
lets the user close a group in one step. The mail itself can then go.

## Storage

One N-Triples file per notice, and one per handling, in
`chambers/_generated/notices/` (override `RETINUE_NOTICES_DIR`). That is the
framework's own pseudo-chamber, next to memory and threads. The life store
indexes it like any chamber file, and it is never pushed to a repository, so
what the user's portals announce stays on the server. The record shapes are in
the `notices.py` docstring.

A **group** is `source-slug/account-slug`, or just the source slug when there
is no account. A group is **open** while it holds a notice that arrived after
the group's latest handling and was not handled on its own. Handling never
deletes anything: it writes a `kb:NoticeHandling` record ("this group, up to
this moment, by this actor"). A notice arriving later opens the group again.
"An upload settles every earlier notice of this account" is therefore not a
special rule. It is simply what handling a group means.

## Working a notice

A sender whose mails are notices gets a contact instruction (docs/contacts.md)
along these lines:

> This is a portal notice. File it on the notice board (`notices.py add
> --source … --account … --text "<what it announces, one line>" --ref <this
> chat id>`), then give the mail the triage word *delete* with the core fact as
> the note, and a verdict of importance 1.

The arrival turn does exactly that. The mail shows on the Chats page's Triage
list under *delete*, with the one line beside it. The board has the fact. The
user clears both whenever they like. `--ref` makes `add` idempotent, so a
turn that runs twice files the notice once.

What a source's notices mean (which ones settle which, what an account is for
this source) belongs in that sender's instruction, in the user's own data, not
in the framework.

## Urgent notices

Most notices can wait. Some cannot: "confirm within 24 hours or the account
is locked". The board must not swallow those, and it must not let doubt ring
either. Every unneeded ring makes the next one worth less.

1. **The message has to say so.** A notice is urgent only when the session can
   quote from the message itself both a **deadline** (`--due`) and a
   **consequence** (`--consequence`). Missing either, the notice goes on the
   board and nothing rings. A deadline alone is just a date on the board's
   line. Most portal notices settle here without any judgement call.
2. **With both, it gets its own thread too.** `add` opens a dashboard thread
   of importance 4.5 with that deadline, `kind` "deadline notice", made
   critical when the deadline is less than 24 hours away. The thread's context
   names the notice, so the turn in which the user deals with it closes it
   (`notices.py handle --id <id>`).
3. **Only the frontier tier may ring.** The thread is opened through
   `conversation-push.py`, whose ring guard refuses a session below the
   frontier tier (below). `add` passes the refusal on as exit 3, and the turn
   escalates. The notice is already filed, and the frontier re-run finds it
   (same `--ref`) and judges afresh: the re-run's own `--due`,
   `--consequence` and `--unsure` replace what the first attempt stored, and
   only they decide whether the thread opens. Leaving them out files the
   notice as one that can wait, and nothing rings.
4. **If even the frontier tier cannot tell, nothing rings.** `--unsure` keeps
   the deadline but opens the thread at importance 3: it waits for the next
   digest, and the board marks the group "deadline?".
5. **Corrections teach.** When the user says something rang that did not need
   to, the lesson goes into that sender's contact instruction ("their 'act
   now' mails are marketing; never urgent"), so the next one does not ring.

## The ring guard

The rule behind point 3 holds for every ring, not just notices: **before
anything interrupts the user, the best available model decides.**
`scripts/ring_guard.py` enforces it in the three CLIs that declare attention:
`conversation-push.py`, `attention-set.py` and `chat-verdict.py`.

- It acts on declarations that can ring: `--critical`, or an importance of
  3.5 or more. That is the importance at which a near deadline reaches
  time-sensitive, the level every shipped mode admits
  (docs/attention-model.md). Below that an item is at most active and waits
  for the digest, which stays open to every tier.
- It acts only in model sessions (`CLAUDECODE` set) below the frontier tier,
  as `memory.session_tier` decides it: an untiered deployment is all frontier.
  The framework's own monitors, Python processes with fixed rules, pass
  unchanged.
- A session that can escalate (`RETINUE_ESCALATE_FILE` set) is refused with
  exit 3 and told to escalate. One that cannot (a scheduled job on a lower
  tier) has its declaration lowered (no `--critical`, importance at most 3)
  and is told so. The item still arrives and waits for the digest. A job whose
  findings must be able to ring pins the frontier model.

Like the memory guards, this catches a prompt that forgot the rule, not a
session working around it on purpose. It is not a security boundary: the tier
is read from the session's environment, which a command can override, and the
gateway does not enforce it.

## Handling

- `notices.py list` shows the open groups, nearest deadline first, then the
  oldest waiting. `--group`, `--detail` and `--json` narrow it down.
- `notices.py handle <group>` closes a group up to now. `handle --id <id>`
  closes one notice (the urgent one whose thread was settled).
- From an outside Claude session, the Ask-Ara connector offers
  `list_notices` and `handle_notices(group)` (docs/ask-ara.md). The connector
  runs the write itself, so the answering session stays without Write.

A dashboard page for the board can come later. The CLI and the connector are
the whole interface for now.

## Durable records stay elsewhere

The board is a to-do list and is meant to be cleared. A notice that also
carries a fact worth keeping (hours worked against a budget, an amount owed)
is recorded where such facts live as well: in the chamber that keeps them,
by that chamber's own rules. Clearing a group must never be the only copy of
anything.
