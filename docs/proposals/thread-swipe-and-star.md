# Dashboard: swipe threads like chats, and star threads and chats

*Design proposal — status: draft for review. Touches `webapp/` and the
gateway's serving logic, so it lands as Tier 3 PRs. The layout follows the
dashboard's earlier design issues (e.g. #282): problem, proposal, acceptance
criteria, then non-binding implementation notes.*

## Problem

The chats list and the threads list are curated in two different ways.

- **Chats** (`chats.html`) are swiped, the way a messenger app works: a swipe
  right archives a chat at once, or restores it from the Archived tab. A swipe
  left opens a tray with the other actions (Archive · Mute, or Restore ·
  Mute/Unmute · Delete). See `webapp/components/chats.js`.
- **Threads** (`conversations.html`) can only be archived from *inside* a
  thread, with the Archive pill in its bar. To clear five finished threads you
  have to open each one, archive it, and go back each time. Restoring works the
  same way: Archived tab, open, Unarchive, back.

There is also no way to keep a thread at hand. The list is sorted by recency
only. A thread you keep returning to, such as a long-running topic with Ara,
sinks under every new triage proposal. Chats have the same gap: a `pinned`
flag has been "deliberately deferred" since the chat surface shipped
(`webapp/README.md`, `docs/messenger-chats.md`).

## Proposal

### 1. Threads swipe exactly like chats

The thread tiles on `conversations.html` get the chats page's gesture, with
the same parameters, colours and words:

| Tab | Swipe right | Swipe left (tray) |
|---|---|---|
| Active | **archive** at once | Star · Archive · Mute |
| Archived | **restore** at once | Star · Restore · Mute *or* Unmute |
| Edits, Cowork | archive or restore, whichever the row's own state calls for | the same tray as the row's state |

For comparison, here is the chats tray after this proposal. The only
difference is Delete, which exists only for chats:

| Tab | Swipe right | Swipe left (tray) |
|---|---|---|
| Active | archive at once | Star · Archive · Mute |
| Archived | restore at once | Star · Restore · Mute *or* Unmute · Delete |

**The gesture** is the one `chats.js` already implements, unchanged:

- The row declares `touch-action: pan-y`, so vertical scrolling stays with the
  browser.
- A drag is claimed as a swipe only after 8 px of movement, and only if it is
  more horizontal than vertical. Otherwise it stays a scroll.
- A right drag past `min(120 px, 35 % of the tile width)` commits. The tile
  slides out in 160 ms and the action is posted.
- A left drag past half the tray's width snaps the tray open. Past the tray's
  edge the tile resists (it moves at a quarter of the drag speed) rather than
  stopping dead.
- Only one tray is open at a time. A press anywhere else closes it. Tapping an
  open tile closes its tray instead of opening the thread.
- The click that ends a swipe is swallowed, so a swipe never opens the thread.
- A long press, a right-click or the context-menu key opens the tray without
  swiping. From the keyboard, focus lands on the tray's first button.
- A list refresh is not rendered while a finger is down. A refresh that
  arrives while a tray is open keeps that tray open.

**The words mean what they mean on chats:**

- **Archive** moves the thread out of the list until something new is filed
  into it. A swipe-archive is exactly the Archive pill in the thread bar:
  `archived` is set, `muted` is not, and an agent's next message brings the
  thread back.
- **Mute** moves it out and keeps it out. Mute posts
  `{archived: true, muted: true}`, which is what Ara runs today when the user
  says "archive this" (`conversation-push.py --archive --mute`). The user can
  now say that themselves, on the thread, without a turn.
- **Unmute** (Archived tab) lets the next message bring the thread back. It
  stays archived until then.
- **Restore** puts the thread back in the list and clears `muted`. This is the
  same as on chats. The thread bar's *Unarchive* pill gets the same meaning,
  so the two ways back in never disagree.

This gives threads a mute control in the dashboard. Until now that was
deliberately left to agents ("no UI for it *yet*",
`_handle_agent_conversation_flags`). With mute on the chats tray, leaving it
off the threads tray would make the same word work on one list and not the
other. On the server, thread `muted` stays independent of `archived`:
muting a thread that stays active is still a valid agent request. Only the
tray's *Mute* button sends both flags.

**What a tile shows:** the meta line already says "archived". It gains
"muted" in the same place, plus a 🔕 glyph like the chat row's mute mark, so
the Unmute/Restore choice is legible before the tray opens.

**Wide layout:** tiles reflow into several columns there, and each tile swipes
inside its own cell. A mouse drag works the same way, because these are
pointer events. The hover outline stays.

### 2. Undo, on both lists

A swipe that commits takes the row out of the list you are looking at. Today
the only way back is to switch tabs, find the row and swipe again. The home
attention list already answers this with a toast: "✓ Done · **Undo**",
6 s (`attention.js`, `UNDO_MS`). This proposal brings the same toast to the
chats and threads lists, for the swipe commit and for the tray's Archive,
Mute and Restore:

> Archived · **Undo** &nbsp;/&nbsp; Muted · **Undo** &nbsp;/&nbsp; Restored · **Undo**

*Undo* writes back the flags the row had before, so undoing a Restore of a
muted chat archives and mutes it again. Star and Unstar get no toast,
because nothing leaves the list. Delete gets none either; its two-tap confirm
stays as it is.

### 3. Star: a user's mark, shared by threads and chats

**Meaning.** ★ means *"keep this one at hand"*. It is a property of the list,
not of attention.

- A **starred** row sorts above the rest of the list it is in. Starred rows are
  in recency order among themselves, and so are the others. There is no
  section heading and no separator, just the glyph, the way messenger pins
  look.
- This applies in every tab: Active, Archived, Edits and Cowork on threads,
  Active and Archived on chats, and the project page's thread list. A starred
  thread you archived for reference is therefore the first thing in the
  Archived tab.
- The star is **independent** of everything else. Archive, Restore, Mute and
  Unmute leave it as it is. A thread that comes back on its own keeps it.
  Deleting a chat erases the chat, star included.
- The star is **not an attention signal**. It changes no importance, no
  delivery decision, no push and nothing on the home list. The home keeps its
  own swipe right, which means *Done*. A thread's importance is the attention
  model's business. A star only sorts a list.
- There is **no cap**. If everything is starred, the list is just in recency
  order again, and the user will notice.

**The mark:** a small gold ★ (`#e3b341`) in front of the title: in the thread
tile's title line, before the dot and the edit tag, and in the chat row before
the name. Its accessible name is "Starred". The glyph is decorative and is not
a tap target. A tap on the row still opens it, and starring is done with the
controls below.

**Where you star:**

| Surface | Control |
|---|---|
| Threads and chats lists | **Star** / **Unstar**, first in the swipe-left tray. It does not remove the row, so it sits away from the removing actions at the right edge. Neutral slate background (`#8b93a3`) so it cannot be mistaken for Mute's amber. |
| Open thread | a ☆/★ icon button in the bar's thread group, before the Archive pill (`aria-pressed`). Not on a chat's companion pane (`bar="actions"`), since a companion follows its chat. |
| Chat page | a switch in the ⋮ menu, above Archive and Mute: "Star" / "**Starred** · Unstar". The chat header shows ★ before the name. The ⋮ dot stays reserved for archived and muted, because those are the states that hide the chat. |

**Who stars.** The star is the user's mark. Agents can read it, and set or
clear it only when the user asks them to ("star this thread"). This is the
same convention Ara follows for `--mute` today. On the user's word, the flag
is set with `conversation-push.py --thread <id> --star` (or `--unstar`) for
threads, and through `/chats/<id>/flags` for chats.

**Agents leave starred threads where they are.** An archive an agent does on
its own initiative skips a starred thread. Today that is triage's "subject
resolved → archive the thread" (`.claude/skills/triage/SKILL.md`). An explicit
instruction from the user still archives it.

## Behavioural invariants (acceptance criteria)

1. On `conversations.html`, a horizontal drag to the right that passes
   `min(120 px, 35 %)` of the tile archives an active thread and restores an
   archived one. A shorter drag snaps back, and a mostly vertical drag
   scrolls the page.
2. A swipe-archive has exactly the Archive pill's effect: `archived` is set,
   `muted` is untouched, and an agent's next message un-archives the thread.
3. The tray's *Mute* sets `archived` and `muted`. *Restore*, from the tray or
   the thread bar, clears both. *Unmute* clears only `muted`.
4. A left drag past half the tray's width opens the tray. Only one tray is
   open at a time. A press outside it closes it, and so does a tap on the
   open tile, which does not open the thread.
5. A long press, a right-click and the context-menu key each open the tray.
   From the keyboard, focus lands on its first button.
6. No swipe gesture ever opens the thread or chat it was performed on.
7. A list refresh never re-renders under a finger that is down. An open
   tray survives a refresh.
8. A failed write leaves the flags as they were, and a tile that was swiped
   out slides back.
9. After a commit that removes a row, a toast offers *Undo* for 6 s. Undo
   restores the row's previous flags exactly.
10. Starring and unstarring work from the tray, the thread bar and the chat
    page menu. The flag is stored on the server, so every device shows it.
11. In every tab of both lists, and on the project page's thread list,
    starred rows come first. Within each group, the order is by recency.
12. Archive, Restore, Mute, Unmute and an agent's wake never change
    `starred`. Deleting a chat removes it with its star.
13. `starred` has no effect on the attention list, the attention model or
    push.
14. Agents set or clear `starred` only on the user's instruction. An archive
    an agent does on its own initiative skips starred threads.
15. Chats and threads use one gesture implementation, not two copies, so
    the two lists cannot drift apart.

## Mockups

[Interactive prototype](https://claude.ai/artifact/MTRSKZypNS7fL4ZfJ9w7Bh)
— swipe the rows on a phone, or drag them with a mouse. It covers the
Threads and Chats tabs, Active/Archived, Star, the thread bar's ☆, the chat
page's ⋮ switches, Undo, and a *Simulate new messages* button that shows
Archive and Mute behaving differently. The prototype is private for now;
ask the owner for access.

## Out of scope

- **Swipe on the home and the project page.** The home's swipe right is
  *Done* and stays that way. The project page's thread list gets the ★ mark
  and the star-first order, but no gesture.
- **Deleting threads.** Chats have Delete because a chat's messages are other
  people's data, mirrored here. A thread is the record of work with Ara.
  Deleting threads would be a separate decision.
- **An unread-only or starred-only filter.** If stars become many, a filter
  could follow. Star-first ordering should come first.
- **Multi-select and bulk actions.**

## Implementation notes (non-binding)

**Phasing — three PRs, each one shippable:**

1. **One gesture module.** Extract `chats.js`'s swipe into
   `webapp/components/swipe-rows.js`: the drag state machine
   (`_dragStart`/`Move`/`End`/`Cancel`), `_slide`, open and close, click
   suppression, `contextmenu`, `onPressOutside`, and keeping a tray open
   across a re-render. It ships with the CSS string the rows need. Chats
   migrate with no change in behaviour. Call the button layer **tray** in the
   new module: "shelf" already names the minimized-pages bar (#282,
   `shelf.js`). Add a Node-harness test of the gesture, in the style of
   `tests/test_webapp_conversation_list.py`, with scripted pointer events.
   It covers axis lock, the commit threshold, the tray snap, click
   suppression and the drag-time render hold. Nothing tests these today.
2. **Threads swipe and Undo.** `conversations.js` wraps each tile:
   `<div class="swipe" data-id data-quick>` holds the quick underlay, the
   tray and the tile, so the tray's buttons sit beside the tile's `<button>`
   and never inside it. The tile's `touch-action` changes from `manipulation`
   to `pan-y`. A new user endpoint, `POST /conversations/<id>/flags`, takes
   `{archived?, muted?}`, validated like `/chats/<id>/flags` (booleans only,
   unknown keys → 400). It settles or reopens the attention item exactly as
   `/archive` does, and answers `_conv_summary`. `/archive` and `/unarchive`
   stay. `/unarchive` also clears `muted`, so it matches *Restore*. The Undo
   toast goes into the gesture module, so chats get it in the same PR.
3. **Star.**
   - Threads: a `starred` field in the conversation JSON, `starred` in
     `_conv_summary`, and accepted by both `/conversations/<id>/flags` and
     `/internal/conversations/<id>/flags`. `conversation-push.py` gets
     `--star`/`--unstar`, which are flags-only like `--archive`.
   - Chats: `starred: False` in `ChatStateStore`'s default document, a
     `set_flags(..., starred=)` that neither mute nor restore touches,
     `starred` in `_chats_payload`, and accepted by `/chats/<id>/flags`.
   - Ordering is done on the client: a small `starredFirst(list)` helper in
     `base.js`, used by `conversations.js`, `chats.js` and `project-page.js`
     before any card cap. The APIs keep their documented recency order.

**Docs to update with the code:**

- `docs/dashboard.md`: the Threads page, plus "Archived + muted" becomes
  "Archived, muted, starred".
- `webapp/README.md`: the `starred` field in both summaries, and the
  "pinned deferred" note.
- `docs/messenger-chats.md`: the roadmap's "optional `pinned`" item.
- The triage skill: skip starred threads on its own archives.
- `CLAUDE.md`'s "Archive-click ≠ archive this" rule gains "or a swipe".
  Muting from the tray is now the user's own "archive for good", so `muted`
  remains the decidable signal.

**Tests on the server side:** extend `tests/test_chat_state.py` and
`tests/test_chat_api.py` (`test_archive_and_mute`) to show the star survives
mute and restore. Add a thread-flags test covering:

- the new user endpoint's validation;
- the star surviving archive, wake and restore;
- `/unarchive` clearing `muted`;
- the internal endpoint and `--star`.
