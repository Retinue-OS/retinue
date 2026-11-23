# Mail threads as chats — step 1 of merging the surfaces

**Status: design note, nothing built.** The first of a short series of steps
that bring the dashboard's conversations together incrementally. This step
gives e-mail the surface the messengers have (`docs/messenger-chats.md`): a
mail thread is a chat, with the companion pane beside it, and the gate's
e-mail path becomes the messengers' — a model turn for VIP and instructed
senders, nothing for the rest. It also changes the Chats page's two groups to
three, for every channel. Step 2 (a thread with Ara is a chat whose counterpart
is Ara) and anything after it are not here. Every phase is Tier 3.

## Why

- **E-mail has no surface.** A mail is visible only once triage has turned it
  into a thread or an omnibus line; "I just sent you an e-mail" has no answer
  but asking Ara. Messenger messages have had a mirror since the chats work.
- **Triage is a parallel world.** It keeps its own handled-state, proposes
  dispositions in dashboard threads, and reasons about a mailbox the user
  never sees. The messengers showed that a mirror plus a turn for the senders
  who matter is cheaper and clearer; e-mail can take the same shape.
- **Archive answers no question.** A chat is archived "until the next
  message", which is nobody's workflow. What the user wants to know is: have I
  dealt with the latest thing that came in?

## Design in one paragraph

A **mail gateway** mirrors the mailbox into the messengers' ledger: every
inbound and outbound mail, attachments kept as files behind its media route.
A **thread is a chat**: the `Message-ID`/`References` chain under one root, on
one account, named by its subject, its participants listed, an `@` channel
mark. Bodies are shown **trimmed** like messages — the new text, no footers or
quoted history — with the original one tap away. The composer replies in the
thread; a new mail is a new chat. **E-mail triage and the whitelist retire**:
a mail lands in its chat and the attention model lists or rings it; a turn runs
only for a VIP or an **instructed** sender, in the chat's companion, and every
rule triage carried — the whitelist, the receipt and filing rules — becomes an
instruction on a contact. The Chats page shows **Unread · Read · Archived** for
every chat: unread until the latest inbound message is replied to or marked
read with a button under it. The chat's read state flows to the mailbox, never
back.

## 1 · A thread is a chat

**Identity.** The chat key is the thread root's `Message-ID`, found by chasing
`In-Reply-To` and `References` to the oldest message known, on one mail
account: `email:<account>:<root message-id>` — the `<channel>:<account>:<chat>`
shape `inbound_store.thread_key()` builds for the messengers, so reply tokens,
`kb:chat` and the chat page need no new case. A mail whose parent is unknown
starts a new chat; a later mail that turns out to be its parent (arriving out
of order, or found in backfill) **merges** the two chats under the older root —
the store rewrites `kb:chat` on the younger one, the chat state carries over,
and the dashboard shows one chat. Subject-only threading (a reply that lost
its headers) is not done: a stray `Re:` with no `References` is a new chat,
which the user can tolerate and the heuristic cannot be trusted to.

**Name and participants.** The name is the first mail's subject with
`Re:`/`Fwd:`/`AW:`/`WG:` prefixes stripped; the subject line of a later mail
that differs is shown on its bubble, not in the name. Under the name, the
participants: every address in `From`, `To` and `Cc` across the thread other
than the user's own, resolved through the contacts (`contacts.py find
--email`) to names where the book has them. A thread with one correspondent is
a 1:1 chat; one with several is a **group chat** — sender labels on the
bubbles, as the messengers show them, and a reply addressed to all of them by
default (reply-all), with a *reply to sender only* option in the composer.
The `@` channel mark sits on the avatar as the messengers' marks do.

**Several accounts.** Each account is its own `<account>`, so the same thread
reaching two of the user's addresses is two chats; the Chats page shows the
account on the row where names repeat within the channel, as it does for two
messenger accounts today (`acct`). Merging across accounts is not done.

**What is not a chat.** The group axis of the gate stays (`docs/triage-delivery-gate.md`,
"E-mail groups"): a sender or `List-Id` flagged `news` goes to the news rail as
today and **never becomes a chat row**; `ignored` keeps it out of the list;
`quieted` keeps it silent but listed. A newsletter is read in the feed, not as
a chat.

## 2 · The mail gateway

A sibling of `sms-gateway.py`, in the container, the single writer of the mail
ledger:

- **Inbound**: it polls the INBOX (and, for the mirror's sake, the folders a
  deployment names) over IMAP and persists each mail as `kb:InboundMessage`
  with `kb:chat` (the thread key), `kb:messageId`, `kb:sender` (the address),
  `kb:account`, the subject, the parsed body, and attachment references. The
  never-drop rule holds: persisted before anything else, `kb:delivered` set by
  the chat rail's acceptance, as for the messengers.
- **Outbound**: every mail sent through `email_client.py` is a
  `kb:OutboundMessage` with `kb:author agent|user`; mail the user sends from a
  client is picked up from the **Sent** folder and persisted with `kb:author
  device` — the mail analogue of the own-device echo, so the chat equals what
  the mail client shows.
- **Attachments, as the messenger gateways keep them.** Each part is stored as
  a file with the content type the mail declared, under the gateway's
  token-gated `GET /media/<id>`; the web-gateway proxies it as
  `/chats/media/<slug>/<media-id>` behind the dashboard's auth, and the proxy's
  rules (`docs/dashboard.md`, "Attachments") decide what renders in place and
  what downloads. **Inline images** (`cid:` parts referenced from the HTML)
  are media in the bubble like a photo on WhatsApp; `audio/*` and `video/*`
  play; a PDF opens in the viewer; everything else is a download row with its
  stated type. Nothing is reduced to text. The HTML body itself is kept as a
  part too, for the original view (section 3).
- **First run.** Backfill the INBOX and the Sent folder for the last **90
  days** (`MAIL_BACKFILL_DAYS`), oldest first, so threads merge as they are
  met; older mail enters the mirror when a new message in its thread arrives
  and the gateway fetches the thread's history (`email_client.py thread`).
  Trimming (section 3) is **lazy** — on first view, not on ingest — so the
  backfill costs no model turns.
- **The mailbox stays the source** of what exists; the mirror is a copy that
  the gateway keeps current. A mail deleted in a client disappears from the
  mirror on the next poll (its bubble becomes a tombstone, the chat stays).

## 3 · Bubbles, trimmed — the original one tap away

A mail is shown the way a message is: the **new text only**. The trim removes
quoted history (`>` lines, `On … wrote:` blocks, Outlook's `From:/Sent:/To:`
headers), signatures (`-- ` and what follows), footers and disclaimers, and
tracking pixels; it keeps inline images in place. Two passes:

1. **Deterministic**, at render: the quote and signature markers above, and a
   small set of footer patterns. Cheap, safe, enough for most personal mail.
2. **A model turn on the router tier** (`RETINUE_ROUTER_MODEL`, the cheapest
   in use) when the deterministic pass leaves more than ~20 lines or the mail
   is HTML-only: *return the message's own content as plain text, nothing
   else*. Run once per message, on first view, cached in the message's record
   (`kb:trimmedText`); never on ingest or backfill. The model sees the body
   only; its output is text, rendered escaped.

Every mail bubble carries an **original** affordance (a small `✉` or *show
original*); a tap opens the full mail in place — headers, the HTML under the
dashboard's CSP (images through the media proxy, scripts and remote loads
blocked, as the chat page already does for messenger media), every
attachment. The trim is a view; the record is never changed.

## 4 · Writing

**Replying** in a chat uses `email_client.py reply` semantics: `In-Reply-To`
and `References` from the message replied to, `Re:` on the subject, the
account the thread lives on; in a group thread, all participants by default.
The send goes through the sending identity's policy (`allow`/`trust`/`verify`)
exactly as a Signal reply does; a queued send shows as pending in the chat with
its approval link.

**A new mail is a new chat**: ＋ on the Chats page → *E-mail* → recipients
(from the contacts, or typed), subject, text. The chat exists from the first
send; its key is the sent mail's `Message-ID`.

**The companion pane** is unchanged: it reads the thread (the engage prompt
carries the trimmed bodies and the attachment list, as it does for a messenger
chat), stages a draft into the composer, sends under policy, forks a topic to a
dashboard conversation. The quick-pattern chips apply.

## 5 · Unread · Read · Archived — for every chat

The Chats page's *Active/Archived* filter becomes three groups, on every
channel:

- **Unread**: the chat's latest inbound message has neither been replied to
  nor marked read. This is the "what still wants me" list.
- **Read**: it has — by a reply from the dashboard, from the phone (own-device
  echo), or from a mail client (Sent folder); or by the **Mark as read**
  button, prominent, under the latest inbound message in the chat.
- **Archived**: as today — out of the list until the next message; muted stays
  archived.

**Opening a chat does not mark it read.** Reading is not dealing with; the
button is one tap and says what it means. The chat state gets
`last_handled` (an ISO timestamp, the newest inbound message dealt with)
beside `last_read` (the scroll watermark the unread badge uses); a chat is
*unread* when its newest inbound `ts` is later than `last_handled`. A new
inbound message moves a read chat back to unread. The row keeps its unread
count badge; the group is the coarser, deliberate state.

The starred flag still leads each group; swipe gestures: right = mark as read
(the gesture *done* used to be), left = archive; the ⋮ tray keeps mute and
delete.

## 6 · The gate: triage and the whitelist retire

The e-mail path of the delivery gate becomes the messengers' path
(`docs/messenger-chats.md`, "Inbound flow"):

1. **Persist** (the mail gateway).
2. **Gate**: the group axis (`news`/`quieted`/`ignored`) as today; the sender
   axis is now the messengers' — **VIP**, **instructed**, everyone else. The
   e-mail **whitelist retires**; `triage_policy.py`'s whitelist commands go.
3. **Notify deterministically**: `POST /internal/chats/inbound`; the chat has
   the mail, the attention model decides from the sender's prior and sphere
   whether it rings, waits for a digest or is listed. **A mail pushes nothing
   by default** — the one per-channel difference from the messengers, where a
   message pushes: a mailbox is read when one looks, a chat rings. A contact
   can say otherwise.
4. **A turn only for a VIP or an instructed sender**, in the chat's companion,
   with the instruction as its brief and the thread (trimmed) in the prompt;
   it ends with `chat-verdict.py` (importance, deadline, actions, flags), and
   the attention model delivers by the verdict. The same timeout and
   "failure rings" guarantee as on the messengers.

**Everything triage knew becomes an instruction.** The migration writes, for
each address on today's whitelist, a contact (or adds the address to the
existing person) with the **generic instruction**:

> See whether this belongs to a project and link it; extract the facts worth
> keeping; determine importance and urgency.

A sender with a more specific rule carries it instead — and **the receipt and
filing rules move into instructions too**: a sender whose mails are receipts,
payslips, statements or invoices (the bank, the payroll service, a shop)
carries *file the document with the Archivist, book it, then archive the
mail*; the Archivist does the filing as it does today when handed a document,
the turn archives the mail (section 7). The rules that live in `sender:`
memories today are read once and written as instruction text on those
contacts; the memories are superseded, not deleted. **No filing rule lives
outside a contact.**

**What retires**: the omnibus digest and the per-mail triage threads, the
triage status store (`TRIAGE_STATE_DIR`) and the "already answered" check
(the mirror knows what was answered), the frequent and daily triage runs.
**What stays**: the news rail on e-mail (unchanged plumbing, now in the mail
gateway); the group flags; `email_client.py` for sends and for the Archivist's
document fetches.

**No daily sweep in this step.** A mail from a sender who is neither VIP nor
instructed is listed in *Unread* and judged by nobody; the user sees it there,
as they see a messenger message from a stranger. A batch that judges what
nobody looked at can be added later and does not depend on anything here.

## 7 · The mailbox follows the chat, never the reverse

Today's rule — `unread ≠ unhandled`, `\Seen` is never state — exists because a
mail client used beside triage would otherwise fight it. With the chat as the
surface the direction is settled: **the chat's state flows to the mailbox, one
way**, so a client shows what the dashboard knows, and nothing a client does
changes the chat.

- *Mark as read* or a reply → `\Seen` on every message of the thread
  (`email_client.py flag --read`).
- *Archive* on the chat → the thread's messages move out of INBOX to the
  archive folder (`TRIAGE_NEWS_FOLDER`'s sibling, `MAIL_ARCHIVE_FOLDER`,
  default `Archive`); *unarchive* does not move them back.
- An instructed turn that files a document archives the mail the same way;
  one that decides a mail is to be deleted moves it to the trash folder.
- **Deleting the chat deletes the mails.** The chat-page delete — today an
  erasure of the mirror, reached from the Archived tab's tray, two taps, no
  undo — moves every message of the thread to the **trash folder**
  (`MAIL_TRASH_FOLDER`, the account's own Trash) before erasing the mirror.
  The trash is the safety net: a mail deleted by mistake is in the client's
  trash for as long as the provider keeps it, which is what a user expects of
  "delete" in any mail app. Nothing is expunged. The chat's own guards stay —
  the two taps, the `CHAT_ERASE_TOKEN` capability that agent sessions do not
  hold, the same-origin check — so no agent and no page from another site can
  trigger it. An instruction's "delete" is the same move.
- `\Seen` set or cleared in a client → ignored. A mail **moved or deleted** in
  a client → gone from the mirror on the next poll; its bubble is a tombstone,
  the chat stays (see open question 2).

## What this replaces, what it keeps

**Replaced**: e-mail triage (omnibus, per-mail threads, status store, the
frequent/daily runs); the e-mail whitelist; the Chats page's Active/Archived
pair (by Unread/Read/Archived).

**Kept**: the chat entity, its state and page, the companion; the messenger
gateways untouched; the delivery ledger and its never-drop rule; the group
flags and the news rail; send policies and `/sends`; `email_client.py`;
contacts and the instruction mechanism; the attention model.

## Phases

1. **The mail gateway, read-only.** Inbound and Sent mirrored into the ledger,
   attachments behind `/media`, thread keys, merging; `/chats` lists mail
   chats; the chat page renders them with the deterministic trim and the
   original view. Triage keeps running as today. Value: *seeing mail threads
   as conversations at last*.
2. **Writing.** Reply in the thread under policy; ＋ → E-mail; the Sent-folder
   echo; the companion pane's draft into the mail composer.
3. **Unread · Read · Archived**, `last_handled`, the Mark-as-read button and
   the swipe, on every channel. Independent of 1–2; can go first.
4. **The gate.** The e-mail sender axis becomes VIP/instructed; the whitelist
   and `sender:` filing memories migrate to instructions; the mail gateway
   notifies the chat rail; triage's runs and store retire. The mailbox follows
   the chat (section 7).
5. **The model trim** on the router tier, lazily, cached.

## Open questions

1. **Threading across accounts.** Two chats for one thread on two of the
   user's addresses — accept, or merge on the root `Message-ID` with the
   accounts listed?
2. **Deleted in a client.** A tombstone bubble and the chat stays, or the chat
   is archived when its last message is gone?
3. **Reply-all default** in a group thread, with *sender only* as the option —
   or the reverse?
4. **The trim's model fallback**: router tier, or a local model (`docs/ollama.md`)
   where one is deployed, since the body is the only input and the output is
   text?
5. **`last_read` vs `last_handled`**: does the unread *count* (messages below
   the scroll watermark) still earn its place beside the coarser group, or does
   the group replace it?
6. **The generic instruction's wording**, and whether "link it to a project"
   should be in it before step 2 gives projects a place in the chat list.
