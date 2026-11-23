#!/usr/bin/env python3
"""Focused checks for the chat state store and the live overlay.

`scripts/chat_state.py` holds everything about a messenger chat that is not a
channel message: the read mark (forward-only, but for the guarded undo of a
*Mark as read*), the version-guarded shared draft, archive/mute, cached
display metadata — plus the in-memory overlay that bridges the life store's
indexing lag. This exercises the draft guard (stale version rejected; agent
staging never clobbers user text), read-mark and unread-classification
semantics, the restore's two guards, the arrival count that decides what
landed after a read (a mark guarded by what was seen, a reply, the undo; one
count per message, however often its event is delivered), the reach of a
read to a message stamped by a fast clock, path-safe state filenames, and
the overlay's merge/dedup/expiry contract — an entry is keyed by its chat as
well as its message id. And what a mailbox adds: an arrival that is gone is
no unread one, a merge keeps every arrival's place in the count true, and
the id of a chat that was folded into another says where it went.

Standalone, no third-party deps:

    python3 tests/test_chat_state.py
"""
import importlib.util
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "chat_state.py"


def _load():
    spec = importlib.util.spec_from_file_location("chat_state", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()


def test_split_and_filename_safety():
    # Split at the FIRST colon only — chat keys carry their own colons.
    assert cs.split_chat_id("signal:group:R2xh/cnVz=") == ("signal", "group:R2xh/cnVz=")
    assert cs.split_chat_id("whatsapp:4179@s.whatsapp.net") == ("whatsapp", "4179@s.whatsapp.net")
    assert cs.split_chat_id("telegram:-1001455667788") == ("telegram", "-1001455667788")
    assert cs.split_chat_id("nochannel") is None
    assert cs.split_chat_id("signal:") is None
    # An id that names an account splits the same way for callers that only
    # want the key — the key means the same thing either way.
    assert cs.split_chat_id("signal:~+41766029556:group:R2xh=") == (
        "signal", "group:R2xh=")
    # Filenames are digests: path-hostile key characters never touch the fs,
    # and distinct ids never collide on a file.
    a = cs.state_filename("signal:group:R2xh/../../etc=")
    b = cs.state_filename("signal:group:R2xh/other=")
    assert a != b and "/" not in a and a.endswith(".json")
    print("PASS test_split_and_filename_safety")


def test_account_is_half_the_identity():
    """A chat is (channel, account, key), and the three round-trip.

    Two accounts of one channel writing to the same peer produce the same chat
    key — that is what merged their conversations — so the account has to be
    part of the id. Ids for records that carry no account keep the exact shape
    they have always had, which is why nothing already on disk moves.
    """
    mk, ref = cs.make_chat_id, cs.split_chat_ref
    # Unattributed: byte-identical to the historical form.
    assert mk("signal", "+41790000000") == "signal:+41790000000"
    assert ref("signal:+41790000000") == ("signal", None, "+41790000000")
    assert ref("signal:group:R2xh=") == ("signal", None, "group:R2xh=")

    # Attributed: the account is a segment of its own, and a key's own colons
    # stay inside the key.
    cid = mk("signal", "group:R2xh=", "+41766029556")
    assert cid == "signal:~+41766029556:group:R2xh="
    assert ref(cid) == ("signal", "+41766029556", "group:R2xh=")

    # Same peer, two accounts: two ids, and two state documents.
    a = mk("signal", "+41790000000", "+41766029556")
    b = mk("signal", "+41790000000", "+41765761976")
    assert a != b
    assert cs.state_filename(a) != cs.state_filename(b)
    assert ref(a)[2] == ref(b)[2], "the peer is the same; the account is not"

    # The shape survives values no channel emits today. The key is the
    # channel's string, so the encoding may not rest on a claim about what
    # channels produce: a key opening with the marker, an account carrying the
    # separator, the marker or a percent sign all round-trip.
    for channel, key, account in [
        ("signal", "~looks-like-a-marker", None),
        ("signal", "~~doubled", None),
        ("signal", "+4179", "acct:with:colons"),
        ("signal", "+4179", "~leading-marker"),
        ("signal", "+4179", "100%"),
        ("telegram", "-1001455667788", "@ara_bot"),
        ("whatsapp", "4179@s.whatsapp.net", "41765761976"),
    ]:
        cid = mk(channel, key, account)
        assert ref(cid) == (channel, account, key), (cid, ref(cid))
        assert cs.split_chat_id(cid) == (channel, key)

    # Malformed ids are refused, never half-read: a bare marker, an empty
    # account or an empty key is not something make_chat_id ever composed.
    for bad in ["", "signal", "signal:", ":key", "signal:~", "signal:~acct",
                "signal:~:key", "signal:~acct:"]:
        assert ref(bad) is None, bad
    print("PASS test_account_is_half_the_identity")


def test_draft_version_guard():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790001122"
        # First user write is based on version 0 (no draft yet).
        ok, doc = store.set_draft(cid, "hoi", author="user", base_version=0)
        assert ok and doc["draft"]["text"] == "hoi"
        assert doc["draft"]["author"] == "user" and doc["draft_version"] == 1
        # A writer that still holds version 0 is stale → rejected, state intact.
        ok, doc = store.set_draft(cid, "clobber", author="user", base_version=0)
        assert not ok and doc["draft"]["text"] == "hoi"
        # The current version succeeds; empty text clears the draft AND its
        # author tag, but the counter keeps counting (a clear is guarded too).
        ok, doc = store.set_draft(cid, "", author="user", base_version=1)
        assert ok and doc["draft"] is None and doc["draft_version"] == 2
        print("PASS test_draft_version_guard")


def test_undo_clear_restores_the_draft_with_its_author():
    """A cleared draft comes back exactly, author included.

    The restore lives here rather than in the client because a client can only
    resubmit text, and the user-facing draft endpoint stamps every write
    "user" — so a draft Ara staged would come back as the user's own words.
    That marker is the one thing saying whose words are about to be sent in
    the user's name, which is precisely what a mis-tapped ✕ must not cost.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790001122"
        store.set_draft(cid, "Samstag passt.", author="agent", agent="Ara",
                        base_version=0)
        ok, doc = store.set_draft(cid, "", author="user", base_version=1)
        assert ok and doc["draft"] is None
        assert doc["cleared"]["text"] == "Samstag passt."

        restored, doc = store.undo_clear(cid)
        assert restored, doc
        assert doc["draft"]["text"] == "Samstag passt."
        assert doc["draft"]["author"] == "agent", doc["draft"]
        assert doc["draft"]["agent"] == "Ara", doc["draft"]
        # The version moves on, so a writer holding the pre-undo version is
        # stale — the restore is guarded like any other write.
        assert doc["draft_version"] == 3
        assert doc["cleared"] is None, "the stash outlived its use"

        # Nothing left to undo: a second attempt refuses rather than repeating.
        again, doc = store.undo_clear(cid)
        assert not again and doc["draft"]["text"] == "Samstag passt."
    print("PASS test_undo_clear_restores_the_draft_with_its_author")


def test_undo_clear_is_refused_once_it_is_stale_or_moot():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790001122"
        # Nothing was ever cleared.
        assert store.undo_clear(cid)[0] is False

        # Writing a draft settles what the composer holds: whatever was cleared
        # before it is no longer the thing to go back to.
        store.set_draft(cid, "erste", author="user", base_version=0)
        store.set_draft(cid, "", author="user", base_version=1)
        store.set_draft(cid, "zweite", author="user", base_version=2)
        assert store.undo_clear(cid)[0] is False
        assert store.get(cid)["draft"]["text"] == "zweite"

        # A send is not a clear to offer back — those words are gone on purpose.
        store.clear_draft(cid)
        assert store.get(cid)["draft"] is None
        assert store.undo_clear(cid)[0] is False

        # And a stash that has aged out is dropped rather than restored.
        store.set_draft(cid, "alt", author="user")
        store.set_draft(cid, "", author="user")
        assert store.get(cid)["cleared"]["text"] == "alt"
        restored, doc = store.undo_clear(cid, max_age_seconds=-1)
        assert not restored and doc["draft"] is None
        assert doc["cleared"] is None, "an aged-out stash should not linger"
    print("PASS test_undo_clear_is_refused_once_it_is_stale_or_moot")


def test_agent_staging_never_clobbers_user_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "telegram:774301992"
        # Agent stages onto an empty draft without knowing a version: accepted.
        ok, doc = store.set_draft(cid, "Draft A", author="agent", agent="Ara",
                                  require_free=True)
        assert ok and doc["draft"]["author"] == "agent"
        assert doc["draft"]["agent"] == "Ara"
        # Re-staging over its own draft is fine (the agent revises itself)…
        ok, doc = store.set_draft(cid, "Draft B", author="agent", require_free=True)
        assert ok
        # …but once the user typed, a versionless agent write is refused.
        ok, doc = store.set_draft(cid, "mine", author="user",
                                  base_version=doc["draft_version"])
        assert ok
        ok, doc = store.set_draft(cid, "agent again", author="agent",
                                  require_free=True)
        assert not ok and doc["draft"]["text"] == "mine"
        # With the current version the agent asserts an informed overwrite.
        ok, doc = store.set_draft(cid, "agent again", author="agent",
                                  base_version=doc["draft_version"])
        assert ok and doc["draft"]["text"] == "agent again"
        # Invalid author is a programming error.
        try:
            store.set_draft(cid, "x", author="assistant")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid author must raise")
        print("PASS test_agent_staging_never_clobbers_user_text")


def test_last_read_forward_only_and_unread_modes():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "whatsapp:4179@s.whatsapp.net"
        # First inbound on a read chat: no unread before → "new".
        doc, had = store.mark_unread(cid, 1000.0)
        assert not had and doc["unread_since"] == cs.iso_z(1000.0)
        # A second inbound while still unread → "reply".
        _, had = store.mark_unread(cid, 1010.0)
        assert had
        # Catching up clears the watermark…
        doc = store.advance_last_read(cid, 1010.0)
        assert doc["last_read"] == cs.iso_z(1010.0)
        assert doc["unread_since"] is None
        # …and the watermark never regresses (a stale client cannot resurrect
        # read messages).
        doc = store.advance_last_read(cid, 500.0)
        assert doc["last_read"] == cs.iso_z(1010.0)
        # The next inbound is "new" again.
        _, had = store.mark_unread(cid, 1020.0)
        assert not had
        print("PASS test_last_read_forward_only_and_unread_modes")


def test_mark_read_and_its_guarded_restore():
    """The read mark only advances — except for the undo of a *Mark as read*,
    which is guarded: only while the mark is still the one being undone, and
    never to a later instant."""
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790000090"
        t1, t2, t3 = cs.iso_z(1000.0), cs.iso_z(2000.0), cs.iso_z(3000.0)
        # mark_read is advance_last_read that also says what the mark was.
        doc, previous = store.mark_read(cid, 1000.0)
        assert previous is None and doc["last_read"] == t1 and doc["read_seq"] == 1
        doc, previous = store.mark_read(cid, 2000.0)
        assert previous == t1 and doc["last_read"] == t2
        # Forward only, still: an older mark moves nothing, and is a read.
        doc, previous = store.mark_read(cid, 500.0)
        assert previous == t2 and doc["last_read"] == t2 and doc["read_seq"] == 3

        # The undo: back to the previous mark, the read count untouched.
        restored, doc = store.restore_last_read(cid, restore=t1, marked=t2)
        assert restored and doc["last_read"] == t1 and doc["read_seq"] == 3
        assert store.get(cid)["last_read"] == t1
        # Spent — the mark is no longer the one named — so nothing changes.
        restored, doc = store.restore_last_read(cid, restore=None, marked=t2)
        assert not restored and doc["last_read"] == t1
        # Back to "never read" is a restore like any other.
        restored, doc = store.restore_last_read(cid, restore=None, marked=t1)
        assert restored and doc["last_read"] is None

        # A reply (or a later mark) in between wins over the undo.
        doc, previous = store.mark_read(cid, 2000.0)
        store.advance_last_read(cid, 3000.0)
        restored, doc = store.restore_last_read(cid, restore=previous, marked=t2)
        assert not restored and doc["last_read"] == t3
        # An "undo" to a later instant would be an advance by another name.
        try:
            store.restore_last_read(cid, restore=cs.iso_z(4000.0), marked=t3)
        except ValueError:
            pass
        else:
            raise AssertionError("a restore later than the mark it undoes must raise")
        assert store.get(cid)["last_read"] == t3
        # Nothing is stored for a chat an undo merely named.
        restored, _ = store.restore_last_read("signal:+41790000091", restore=None, marked=t1)
        assert not restored and not store.exists("signal:+41790000091")

        # What the newest mark settled is remembered beside it, and cleared.
        assert store.get(cid)["read_settled"] is None
        assert store.set_read_settled(cid, "2026-01-01T00:00:00+00:00")["read_settled"] \
            == "2026-01-01T00:00:00+00:00"
        assert store.set_read_settled(cid, None)["read_settled"] is None
        print("PASS test_mark_read_and_its_guarded_restore")


def test_arrival_order_decides_what_came_after_a_read():
    """`read_inbound` is the arrival count the newest read covered: what is
    counted after it landed after the read, whatever it is stamped."""
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "sms:+41790000095"
        since = lambda: cs.arrived_since_read(store.get(cid))  # noqa: E731
        # Never read: nothing is recorded, the timestamps alone decide.
        store.mark_unread(cid, 1000.0)
        assert store.get(cid)["read_inbound"] is None and since() == 0
        assert cs.arrived_since_read(None) == 0 and cs.arrived_since_read({}) == 0
        # A document from before the field existed reads the same way.
        assert cs.arrived_since_read({"inbound_seq": 57}) == 0

        # Read as it stands: both arrivals so far are covered.
        store.mark_unread(cid, 1010.0)
        doc, previous = store.mark_read(cid)
        assert previous is None and doc["read_inbound"] == 2 and since() == 0
        assert doc["unread_since"] is None
        mark = doc["last_read"]
        # One that lands now is unread though it is stamped long before the
        # mark, and the mark has not moved.
        doc, _ = store.mark_unread(cid, 900.0)
        assert doc["last_read"] == mark and cs.iso_z(900.0) < mark
        assert since() == 1

        # Guarded by what the caller showed: a message it never saw refuses
        # the mark, and nothing is written.
        marked, doc, previous = store.mark_read_unless_arrived(cid, None, 2)
        assert not marked and previous == mark
        assert (doc["read_seq"], doc["read_inbound"]) == (1, 2) and since() == 1
        marked, doc, previous = store.mark_read_unless_arrived(cid, None, 3)
        assert marked and previous == mark and doc["read_inbound"] == 3 and since() == 0
        # A caller ahead of the count is not refused; None does not ask.
        assert store.mark_read_unless_arrived(cid, None, 99)[0]
        assert store.mark_read_unless_arrived(cid, None, None)[0]

        # A read naming an instant behind the mark covers nothing that landed
        # since: not a stale mark, not a late echo of an old reply.
        store.mark_unread(cid, 950.0)
        store.mark_read(cid, 500.0)
        assert since() == 1
        store.advance_last_read(cid, 600.0)
        assert since() == 1
        # The dashboard's own send does, whatever instant it is stamped with …
        store.advance_last_read(cid, 600.0, everything=True)
        assert since() == 0 and store.get(cid)["unread_since"] is None
        # … and so does a reply or a mark stamped at or after the mark.
        store.mark_unread(cid, 960.0)
        store.advance_last_read(cid, store.get(cid)["last_read"])
        assert since() == 0

        # A mark is never set past this clock, whatever instant it names: a
        # read cannot have happened later than now. The instant still says
        # what the read covers.
        ahead = "sms:+41790000096"
        future = time.time() + 3600
        store.mark_unread(ahead, 1000.0)
        doc, previous = store.mark_read(ahead, future)
        assert previous is None and doc["last_read"] <= cs.iso_z()
        store.mark_unread(ahead, 1010.0)
        doc, _ = store.mark_read(ahead, future)
        assert doc["last_read"] <= cs.iso_z() and cs.arrived_since_read(doc) == 0
        # A reply keeps the instant it was written at.
        doc = store.advance_last_read(ahead, future)
        assert doc["last_read"] == cs.iso_z(future)
        print("PASS test_arrival_order_decides_what_came_after_a_read")


def test_the_undo_restores_the_arrival_count_too():
    """Undoing a mark puts `read_inbound` back with it — the messages that
    had landed unread before the mark are unread again, with any that came
    since — and never reaches behind a reply."""
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "sms:+41790000097"
        since = lambda: cs.arrived_since_read(store.get(cid))  # noqa: E731
        store.mark_unread(cid, 1000.0)
        doc, _ = store.mark_read(cid, 2000.0)
        t2 = doc["last_read"]
        store.mark_unread(cid, 1500.0)           # lands late, behind the mark
        assert since() == 1
        doc, previous = store.mark_read(cid, 3000.0)
        t3 = doc["last_read"]
        assert previous == t2 and since() == 0
        assert doc["read_undo"] == {"read_inbound": 1, "read_ahead": None}
        store.mark_unread(cid, 2500.0)           # another, after this mark
        restored, doc = store.restore_last_read(cid, restore=previous, marked=t3)
        assert restored and doc["last_read"] == t2
        assert doc["read_inbound"] == 1 and doc["read_undo"] is None and since() == 2
        # Undone to "never read": nothing is recorded again.
        store.mark_read(cid, 3000.0)
        store.restore_last_read(cid, restore=t2, marked=t3)
        restored, doc = store.restore_last_read(cid, restore=None, marked=t2)
        assert restored and doc["last_read"] is None

        # A reply in the mark's own second passes the undo's first guard (the
        # mark still reads the same); its cover of what had landed stands.
        other = "sms:+41790000098"
        store.mark_unread(other, 1000.0)
        doc, previous = store.mark_read(other, 2000.0)
        store.mark_unread(other, 1900.0)
        doc = store.advance_last_read(other, 2000.0)
        assert doc["read_inbound"] == 2 and doc["read_undo"] is None
        restored, doc = store.restore_last_read(other, restore=previous,
                                                marked=cs.iso_z(2000.0))
        assert restored and doc["last_read"] is None and doc["read_inbound"] == 2
        assert cs.arrived_since_read(doc) == 0
        # A stale read in between leaves the undo's record alone.
        third = "sms:+41790000099"
        store.mark_unread(third, 1000.0)
        store.mark_read(third, 2000.0)
        store.mark_unread(third, 1900.0)
        doc, previous = store.mark_read(third, 3000.0)
        store.advance_last_read(third, 1000.0)
        store.mark_read(third, 1000.0)
        restored, doc = store.restore_last_read(third, restore=previous,
                                                marked=cs.iso_z(3000.0))
        assert restored and cs.arrived_since_read(doc) == 1
        print("PASS test_the_undo_restores_the_arrival_count_too")


def test_a_repeated_event_is_one_arrival():
    """The rail delivers at least once; the count is per message. An event
    repeated for a message id the chat has counted is no second arrival —
    across a restart too, the record being in the document."""
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "sms:+41790000085"
        since = lambda: cs.arrived_since_read(store.get(cid))  # noqa: E731
        doc, had = store.mark_unread(cid, 1000.0, message_id="m1")
        assert not had and doc["inbound_seq"] == 1 and cs.arrival_seq(doc, "m1") == 1
        store.mark_read(cid)
        doc, had = store.mark_unread(cid, 1010.0, message_id="m2")
        assert not had and doc["inbound_seq"] == 2 and since() == 1
        before = store.get(cid)
        # The retry of that event: counted once, nothing written, and the
        # chat is said to stand as it does.
        doc, had = store.mark_unread(cid, 1010.0, message_id="m2")
        assert had and doc == before and store.get(cid) == before
        assert since() == 1 and cs.arrival_seq(doc, "m2") == 2
        # A new store over the same directory — the web gateway restarted
        # inside the retry window — answers the same.
        again = cs.ChatStateStore(tmp)
        doc, _ = again.mark_unread(cid, 1010.0, message_id="m2")
        assert doc["inbound_seq"] == 2 and since() == 1

        # A repeat of an older message keeps the place that message was
        # counted at, not the count as it stands: an arrival still on its
        # way into the attention model is not claimed by it.
        store.mark_unread(cid, 1020.0, message_id="m3")
        doc, _ = store.mark_unread(cid, 1010.0, message_id="m2")
        assert doc["inbound_seq"] == 3
        assert (cs.arrival_seq(doc, "m2"), cs.arrival_seq(doc, "m3")) == (2, 3)
        # Read, and redelivered afterwards: it stays read, and "new" is
        # still what the next message is.
        doc, _ = store.mark_read(cid)
        doc, had = store.mark_unread(cid, 1020.0, message_id="m3")
        assert not had and since() == 0 and doc["unread_since"] is None
        _, had = store.mark_unread(cid, 1030.0, message_id="m4")
        assert not had and since() == 1

        # An event that carries no id cannot be told from a new message:
        # counted, as before, and its place is the count.
        doc, _ = store.mark_unread(cid, 1040.0)
        doc, _ = store.mark_unread(cid, 1040.0)
        assert doc["inbound_seq"] == 6 and since() == 3
        assert cs.arrival_seq(doc, None) == 6 and cs.arrival_seq(doc, "never-seen") == 6
        assert cs.arrival_seq(None, "m1") == 0 and cs.arrival_seq({"inbound_seq": 4}, "m1") == 4

        # The record is bounded: only the newest ids are kept, and one that
        # has aged out counts again.
        for n in range(cs.INBOUND_RECENT_MAX):
            doc, _ = store.mark_unread(cid, 2000.0 + n, message_id=f"n{n}")
        assert len(doc["inbound_recent"]) == cs.INBOUND_RECENT_MAX
        assert doc["inbound_recent"][-1] == [f"n{cs.INBOUND_RECENT_MAX - 1}", doc["inbound_seq"]]
        seq = doc["inbound_seq"]
        assert store.mark_unread(cid, 2000.0, message_id="n0")[0]["inbound_seq"] == seq
        assert store.mark_unread(cid, 1030.0, message_id="m4")[0]["inbound_seq"] == seq + 1
        print("PASS test_a_repeated_event_is_one_arrival")


def test_a_read_reaches_a_stamp_from_a_fast_clock():
    """A message stamped a little ahead of this clock lies later than a mark
    made after it landed. A read of the chat as it stands covers it beside
    the mark (`read_ahead`); the mark itself stays this clock's, and the
    undo puts back exactly what the mark changed."""
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "sms:+41790000086"
        now = time.time()
        fast = cs.iso_z(now + 30)
        # The rail hands the arrival on as now, and its own stamp beside it.
        doc, _ = store.mark_unread(cid, now, message_id="f1", stamp=now + 30)
        assert doc["inbound_ahead"] == fast and doc["unread_since"] == cs.iso_z(now)
        assert cs.read_cut(doc) == "" and cs.read_cut(None) == ""
        doc, previous = store.mark_read(cid)
        assert previous is None and doc["last_read"] <= cs.iso_z()
        assert doc["read_ahead"] == fast and cs.read_cut(doc) == fast
        assert doc["read_undo"] == {"read_inbound": None, "read_ahead": None}
        mark = doc["last_read"]
        # What lands next is unread by its place in the count, though the
        # reach of the read lies past its stamp.
        doc, _ = store.mark_unread(cid, time.time(), message_id="f2")
        assert cs.arrived_since_read(doc) == 1 and doc["read_ahead"] == fast
        # A read up to a named instant, or an echoed reply, reaches no
        # further than its instant — and takes nothing back either.
        doc, _ = store.mark_read(cid, mark)
        assert doc["read_ahead"] == fast and cs.arrived_since_read(doc) == 0
        assert doc["read_undo"] == {"read_inbound": 1, "read_ahead": fast}
        doc = store.advance_last_read(cid, mark)
        assert doc["read_ahead"] == fast and doc["last_read"] == mark
        # Naming the stamp itself — the message the caller showed — reaches
        # it; the mark is still not lifted there. An instant short of it
        # reaches nothing.
        named = "sms:+41790000084"
        store.mark_unread(named, now, message_id="n1", stamp=now + 30)
        doc, _ = store.mark_read(named, now + 10)
        assert doc["read_ahead"] is None and doc["last_read"] <= cs.iso_z()
        doc, _ = store.mark_read(named, now + 30)
        assert doc["read_ahead"] == fast and doc["last_read"] <= cs.iso_z()
        assert doc["read_undo"] == {"read_inbound": 1, "read_ahead": None}

        # The undo: the mark, the count and the reach go back together.
        undo = "sms:+41790000087"
        store.mark_unread(undo, 1000.0, message_id="u1")
        doc, _ = store.mark_read(undo, 2000.0)
        t2 = doc["last_read"]
        store.mark_unread(undo, time.time(), message_id="u2", stamp=time.time() + 45)
        doc, previous = store.mark_read(undo)
        ahead = doc["read_ahead"]
        assert previous == t2 and ahead == doc["inbound_ahead"] > doc["last_read"]
        restored, doc = store.restore_last_read(undo, restore=previous, marked=doc["last_read"])
        assert restored and (doc["last_read"], doc["read_inbound"], doc["read_ahead"]) == \
            (t2, 1, None) and doc["read_undo"] is None
        assert cs.read_cut(doc) == t2 and cs.arrived_since_read(doc) == 1
        # Marked again, and marked once more: the second mark's undo goes
        # back to the first one's reach, not to none.
        doc, _ = store.mark_read(undo)
        first = doc["last_read"]
        doc, previous = store.mark_read(undo)
        assert doc["read_undo"] == {"read_inbound": 2, "read_ahead": ahead}
        restored, doc = store.restore_last_read(undo, restore=previous, marked=doc["last_read"])
        assert restored and (doc["last_read"], doc["read_ahead"]) == (first, ahead)
        # A record of an undo written before the field existed names none,
        # and changes none.
        doc = store._read(undo)
        doc["read_undo"] = {"read_inbound": 1}
        store._write(doc)
        restored, doc = store.restore_last_read(undo, restore=t2, marked=first)
        assert restored and (doc["read_inbound"], doc["read_ahead"]) == (1, ahead)

        # The dashboard's own send answers everything that has landed, so it
        # reaches as far; a mark that has passed the stamp needs no reach.
        sent = "sms:+41790000088"
        store.mark_unread(sent, time.time(), message_id="s1", stamp=time.time() + 20)
        doc = store.advance_last_read(sent, time.time(), everything=True)
        assert doc["read_ahead"] == doc["inbound_ahead"] and doc["read_undo"] is None
        doc = store._read(sent)
        doc["inbound_ahead"] = doc["read_ahead"] = cs.iso_z(time.time() - 5)
        store._write(doc)
        doc, _ = store.mark_read(sent)
        assert doc["read_ahead"] is None and cs.read_cut(doc) == doc["last_read"]

        # Only a clock's error is taken for one. A stamp further ahead than
        # READ_SKEW_SECONDS is kept nowhere, and no read reaches it; nor is
        # one that is not ahead at all.
        far = "sms:+41790000089"
        doc, _ = store.mark_unread(far, time.time(), message_id="x1",
                                   stamp=time.time() + cs.READ_SKEW_SECONDS + 60)
        assert doc["inbound_ahead"] is None
        doc, _ = store.mark_unread(far, time.time() - 50, message_id="x2")
        assert doc["inbound_ahead"] is None
        doc, _ = store.mark_read(far)
        assert doc["read_ahead"] is None and cs.read_cut(doc) == doc["last_read"]
        # The later of two fast stamps is the one kept.
        store.mark_unread(far, time.time(), message_id="x3", stamp=time.time() + 60)
        doc, _ = store.mark_unread(far, time.time(), message_id="x4", stamp=time.time() + 20)
        assert doc["inbound_ahead"] >= cs.iso_z(time.time() + 55)
        print("PASS test_a_read_reaches_a_stamp_from_a_fast_clock")


def test_archive_unless_arrived():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790000080"
        # A chat the attention model has never seen archives as before.
        doc, archived = store.archive_unless_arrived(cid)
        assert archived and doc["archived"]
        store.set_flags(cid, archived=False)
        # An arrival counted but not yet taken in holds the archive off…
        doc, _ = store.mark_unread(cid, 1000.0)
        assert doc["inbound_seq"] == 1 and doc["attended_seq"] == 0
        doc, archived = store.archive_unless_arrived(cid)
        assert not archived and not doc["archived"]
        # …until a block decided from it is stored; it only moves forward.
        store.set_attention(cid, {"state": "open"}, attended=1)
        doc = store.set_attention(cid, {"state": "open"}, attended=0)
        assert doc["attended_seq"] == 1
        doc, archived = store.archive_unless_arrived(cid)
        assert archived and doc["archived"]
        print("PASS test_archive_unless_arrived")


def test_flags_and_note_message():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41794456312"
        doc = store.set_flags(cid, archived=True, muted=True)
        assert doc["archived"] and doc["muted"]
        doc = store.set_flags(cid, archived=False)
        assert not doc["archived"] and doc["muted"], "muted must survive un-archive"
        assert doc["starred"] is False, "no star by default"
        doc = store.set_flags(cid, starred=True)
        assert doc["starred"] and not doc["archived"], "a star moves nothing"
        doc = store.set_flags(cid, muted=True)
        assert doc["starred"] and doc["archived"], "muting keeps the star"
        doc = store.set_flags(cid, archived=False, muted=False)
        assert doc["starred"], "restoring keeps the star"
        doc = store.set_flags(cid, starred=False)
        assert not doc["starred"]
        doc = store.set_flags(cid, archived=True, muted=True)
        doc = store.set_flags(cid, archived=False)
        doc = store.note_message(cid, name="Mara Meier", group=False,
                                 gateway="signal-gateway-personal",
                                 sender="+41794456312", sender_name="Mara Meier")
        assert doc["name"] == "Mara Meier"
        assert doc["gateway"] == "signal-gateway-personal"
        assert doc["roster"]["+41794456312"] == "Mara Meier"
        assert doc["roster_refreshed"] is not None
        # all() round-trips by id.
        assert store.all()[cid]["name"] == "Mara Meier"
        print("PASS test_flags_and_note_message")


def test_companion_link():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "whatsapp:123456@g.us"
        assert store.get(cid)["companion"] is None, "no companion by default"
        doc = store.set_companion(cid, "deadbeef")
        assert doc["companion"] == "deadbeef"
        # Survives an unrelated write, and round-trips through all().
        store.set_flags(cid, archived=True)
        assert store.get(cid)["companion"] == "deadbeef"
        assert store.all()[cid]["companion"] == "deadbeef"
        assert store.set_companion(cid, None)["companion"] is None
        print("PASS test_companion_link")


def test_overlay_merge_dedup_expiry():
    ov = cs.ChatOverlay(ttl=60.0)
    cid = "signal:+41790001122"
    ov.insert({"chat_id": cid, "direction": "in", "text": "a",
               "ts": cs.iso_z(100.0), "message_id": "m1"})
    # Re-delivery of the same message id replaces, never duplicates.
    ov.insert({"chat_id": cid, "direction": "in", "text": "a (edited)",
               "ts": cs.iso_z(100.0), "message_id": "m1"})
    # No id → the (chat, ts, text) fallback key dedups.
    ov.insert({"chat_id": cid, "direction": "out", "author": "user",
               "text": "b", "ts": cs.iso_z(200.0)})
    ov.insert({"chat_id": cid, "direction": "out", "author": "user",
               "text": "b", "ts": cs.iso_z(200.0)})
    ov.insert({"chat_id": "telegram:1", "direction": "in", "text": "other",
               "ts": cs.iso_z(150.0), "message_id": "m2"})
    got = ov.entries(cid)
    assert [e["text"] for e in got] == ["a (edited)", "b"], got
    assert all("_inserted" not in e for e in got)
    assert len(ov.entries()) == 3
    # Expiry: a stale entry drops out (backdated directly, as the RecentSends
    # test does — the clock is the contract, not a sleep).
    with ov._lock:
        for key in list(ov._entries):
            if ov._entries[key].get("message_id") == "m1":
                ov._entries[key]["_inserted"] = time.time() - 120.0
    assert [e["text"] for e in ov.entries(cid)] == ["b"]
    print("PASS test_overlay_merge_dedup_expiry")


def test_iso_z_normalizes():
    assert cs.iso_z(0) == "1970-01-01T00:00:00Z"
    assert cs.iso_z("2026-08-27T09:12:00+02:00") == "2026-08-27T07:12:00Z"
    assert cs.iso_z("2026-08-27T07:12:00Z") == "2026-08-27T07:12:00Z"
    # ISO strings compare as instants once normalized.
    assert cs.iso_z(10.0) < cs.iso_z(20.0)
    print("PASS test_iso_z_normalizes")


def test_delete_forgets_the_chat():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790000001"
        assert store.delete(cid) is None, "nothing stored, nothing to delete"
        store.set_flags(cid, muted=True)
        store.set_companion(cid, "f" * 32)
        gone = store.delete(cid)
        assert gone["muted"] is True and gone["companion"] == "f" * 32
        assert cid not in store.all(), "a deleted chat is not listed"
        fresh = store.get(cid)
        assert fresh["muted"] is False and fresh["companion"] is None
    ov = cs.ChatOverlay(ttl=60)
    ov.insert({"chat_id": "a", "message_id": "1", "ts": "t"})
    ov.insert({"chat_id": "a", "message_id": "2", "ts": "t"})
    ov.insert({"chat_id": "b", "message_id": "3", "ts": "t"})
    assert ov.forget("a") == 2
    assert [e["chat_id"] for e in ov.entries()] == ["b"]
    print("PASS test_delete_forgets_the_chat")


MAIL_A = "email:~me@example.org:root-a@x.org"
MAIL_B = "email:~me@example.org:root-b@x.org"


def test_a_mail_chat_keeps_its_subject_and_its_people():
    """A mail thread is named by its subject: the people in it are kept
    beside the name, and a contact card never writes over it."""
    assert cs.named_by_subject("email") and not cs.named_by_subject("signal")
    # A Message-ID is a key like any other: it round-trips, colons, slashes
    # and all, and never touches the file system.
    key = "a/b:c+d=@mail.example.org"
    cid = cs.make_chat_id("email", key, "me@example.org")
    assert cs.split_chat_ref(cid) == ("email", "me@example.org", key)
    assert "/" not in cs.state_filename(cid)
    # Participants are text from outside: an address must read as one, a
    # name is one bounded line, each address is listed once.
    assert cs.clean_party({"address": " Ann@X.org ", "name": "Ann\r\n Example\x00"}) == {
        "address": "ann@x.org", "name": "Ann Example"}
    assert cs.clean_party({"address": "ann@x.org"}) == {"address": "ann@x.org", "name": None}
    for bad in (None, "ann@x.org", {}, {"address": "no-at-sign"}, {"address": "a b@x.org"},
                {"address": "@x.org"}, {"address": "a@"}, {"address": 7},
                {"address": "a@x.org\nBcc: y@z.org"}, {"address": "a" * 400 + "@x.org"}):
        assert cs.clean_party(bad) is None, bad
    assert len(cs.clean_party({"address": "a@x.org", "name": "n" * 999})["name"]) == 200
    parties = cs.clean_parties([{"address": "A@x.org", "name": "A"}, "junk",
                                {"address": "a@x.org", "name": "again"},
                                {"address": "b@y.org"}])
    assert parties == [{"address": "a@x.org", "name": "A"}, {"address": "b@y.org", "name": None}]
    assert cs.clean_parties("not a list") == [] and cs.clean_parties(None) == []
    assert len(cs.clean_parties([{"address": f"p{i}@x.org"} for i in range(500)])) \
        == cs.PARTICIPANTS_MAX
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        assert store.get(MAIL_A)["participants"] is None and store.get(MAIL_A)["peer"] is None
        doc = store.note_message(MAIL_A, name="Quarterly numbers", group=False,
                                 participants=[{"address": "Ann@x.org", "name": "Ann"}],
                                 peer={"address": "ann@x.org", "name": "Ann"})
        assert doc["participants"] == [{"address": "ann@x.org", "name": "Ann"}]
        assert doc["peer"] == {"address": "ann@x.org", "name": "Ann"}
        # None leaves them; a list replaces; a peer that is none is not stored.
        doc = store.note_message(MAIL_A, sender="ann@x.org", sender_name="Ann")
        assert doc["participants"] and doc["peer"]
        doc = store.note_message(MAIL_A, participants=[], peer={"address": "nonsense"})
        assert doc["participants"] == [] and doc["peer"] == {"address": "ann@x.org", "name": "Ann"}
        # The contact card: filed beside the subject, and removed without
        # taking the subject with it.
        doc = store.set_contact(MAIL_A, name="Ann Meier", sphere="Customers", person="ann-1")
        assert doc["name"] == "Quarterly numbers" and doc["contact"]["name"] == "Ann Meier"
        assert doc["contact"]["sphere"] == "customers" and doc["contact"]["person"] == "ann-1"
        doc = store.set_contact(MAIL_A, name="")
        assert doc["name"] == "Quarterly numbers" and doc["contact"] is None
        # A messenger chat is still called by its card, and loses the name with it.
        sig = "signal:+41794456312"
        assert store.set_contact(sig, name="Mara Meier")["name"] == "Mara Meier"
        assert store.set_contact(sig, name="")["name"] is None
    print("PASS test_a_mail_chat_keeps_its_subject_and_its_people")


def test_merge_folds_one_chat_into_another():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        # Nothing to fold: nothing written, either way round.
        doc, gone = store.merge(MAIL_B, MAIL_A)
        assert gone is None and not store.exists(MAIL_A) and not store.exists(MAIL_B)
        store.note_message(MAIL_A, name="Offer", participants=[{"address": "a@x.org"}],
                           peer={"address": "a@x.org"}, group=False)
        assert store.merge(MAIL_A, MAIL_A) == (store.get(MAIL_A), None)

        # The survivor: one arrival, read past it, then one more — unread.
        store.mark_unread(MAIL_A, cs.iso_z(100.0))
        store.advance_last_read(MAIL_A, cs.iso_z(150.0))
        store.mark_unread(MAIL_A, cs.iso_z(140.0))     # landed late, stamped early
        store.set_draft(MAIL_A, "mine", author="user")
        store.set_companion(MAIL_A, "a" * 32)
        store.set_attention(MAIL_A, {"state": "done", "done_how": "replied"}, attended=1)
        # The absorbed: read later, two arrivals of which one came after.
        store.note_message(MAIL_B, name="Re: Offer", group=False,
                           participants=[{"address": "b@y.org", "name": "B"}],
                           peer={"address": "b@y.org", "name": "B"},
                           sender="b@y.org", sender_name="B", unknown_sender=True)
        store.mark_unread(MAIL_B, cs.iso_z(200.0))
        store.advance_last_read(MAIL_B, cs.iso_z(300.0))
        store.mark_unread(MAIL_B, cs.iso_z(400.0))
        store.set_flags(MAIL_B, starred=True, archived=True)
        store.set_draft(MAIL_B, "theirs", author="agent", agent="Ara")
        store.set_draft(MAIL_B, "theirs, again", author="agent", agent="Ara")
        store.set_companion(MAIL_B, "b" * 32)
        store.set_attention(MAIL_B, {"state": "open", "importance": 4.0}, attended=1)
        store.set_contact(MAIL_B, name="B Person", person="b-1")
        before = store.get(MAIL_B)

        doc, gone = store.merge(MAIL_B, MAIL_A)
        assert gone == before and not store.exists(MAIL_B)
        assert MAIL_B not in store.all() and store.get(MAIL_A) == doc
        # The later read mark; the arrivals that landed after each chat's own
        # read are still unread, wherever the other's mark falls.
        assert doc["last_read"] == cs.iso_z(300.0)
        assert doc["inbound_seq"] == 4 and doc["read_inbound"] == 2
        assert cs.arrived_since_read(doc) == 2
        assert doc["read_seq"] == 2 and doc["attended_seq"] == 2
        assert doc["read_undo"] is None, "no undo reaches behind a merge that moved the mark"
        # Flags: the star follows; archived only if both were.
        assert doc["starred"] is True and doc["archived"] is False and doc["muted"] is False
        # The survivor's draft and companion stand; its settled attention
        # block gives way to the one still open.
        assert doc["draft"]["text"] == "mine" and doc["companion"] == "a" * 32
        assert doc["draft_version"] >= 2, "never behind a version the absorbed chat handed out"
        assert doc["attention"] == {"state": "open", "importance": 4.0}
        # Its own name and peer; the absorbed chat's where it had none.
        assert doc["name"] == "Offer" and doc["peer"] == {"address": "a@x.org", "name": None}
        assert doc["contact"]["name"] == "B Person" and doc["unknown_sender"] is True
        assert doc["participants"] == [{"address": "a@x.org", "name": None},
                                       {"address": "b@y.org", "name": "B"}]
        assert doc["group"] is True, "two participants make a group"
        assert doc["roster"] == {"b@y.org": "B"}
        # Idempotent: the absorbed chat is gone, so a repeat changes nothing.
        again, gone = store.merge(MAIL_B, MAIL_A)
        assert gone is None and again == doc

        # A survivor with nothing of its own takes the draft (under a version
        # later than any either handed out), the companion and the gateway
        # stamp with its provenance; muted on the absorbed side mutes — and
        # so archives — the whole.
        c, d = "email:~me@example.org:c@x.org", "email:~me@example.org:d@x.org"
        store.set_draft(c, "x", author="user")
        store.set_draft(c, "", author="user")          # cleared: version 2, no draft
        store.set_draft(d, "staged", author="agent", agent="Ara")
        store.set_companion(d, "d" * 32)
        store.set_flags(d, muted=True)
        store.note_message(d, gateway="mail-gateway", gateway_source=cs.GATEWAY_SOURCE_ACCOUNT)
        doc, gone = store.merge(d, c)
        assert doc["draft"]["text"] == "staged" and doc["draft"]["agent"] == "Ara"
        assert doc["draft"]["version"] == doc["draft_version"] == 3
        ok, _doc = store.set_draft(c, "stale", author="user", base_version=2)
        assert not ok, "a writer holding the old version is refused"
        assert doc["companion"] == "d" * 32 and doc["cleared"] is None
        assert doc["muted"] is True and doc["archived"] is True
        assert doc["gateway"] == "mail-gateway"
        assert doc["gateway_source"] == cs.GATEWAY_SOURCE_ACCOUNT
        # Two chats never read have no mark between them: the timestamps
        # go on deciding, and nothing is counted as landed after a read.
        assert doc["last_read"] is None and doc["read_inbound"] is None
        assert cs.arrived_since_read(doc) == 0
        # A chat never read keeps its arrivals unread behind the mark it
        # inherits from the other.
        e, f = "email:~me@example.org:e@x.org", "email:~me@example.org:f@x.org"
        store.mark_unread(e, cs.iso_z(10.0))
        store.mark_unread(e, cs.iso_z(20.0))
        store.advance_last_read(f, cs.iso_z(500.0))
        doc, _gone = store.merge(e, f)
        assert doc["last_read"] == cs.iso_z(500.0) and cs.arrived_since_read(doc) == 2
        assert doc["unread_since"] is None or doc["unread_since"] > doc["last_read"]
    print("PASS test_merge_folds_one_chat_into_another")


def test_overlay_follows_a_merge_and_a_removal():
    ov = cs.ChatOverlay(ttl=60.0)
    ov.insert({"chat_id": "a", "message_id": "1", "ts": cs.iso_z(1.0), "text": "x"})
    ov.insert({"chat_id": "a", "ts": cs.iso_z(2.0), "text": "no id"})
    ov.insert({"chat_id": "b", "message_id": "3", "ts": cs.iso_z(3.0), "text": "y"})
    ov.insert({"chat_id": "b", "ts": cs.iso_z(2.0), "text": "no id"})
    assert ov.rekey("a", "b") == 2 and not ov.entries("a")
    # The id-less entry that was already there under the same key keeps its
    # place; nothing is doubled.
    assert [(e.get("message_id"), e["text"]) for e in ov.entries("b")] == [
        ("1", "x"), (None, "no id"), ("3", "y")]
    assert ov.rekey("a", "b") == 0, "a repeat moves nothing"
    # A re-delivery after the move replaces the moved entry, never doubles it.
    ov.insert({"chat_id": "b", "message_id": "1", "ts": cs.iso_z(1.0), "text": "x again"})
    assert len(ov.entries("b")) == 3
    assert ov.forget_messages("b", ["1", "nope", None]) == 1
    assert ov.forget_messages("c", ["3"]) == 0, "only that chat's"
    assert [e.get("message_id") for e in ov.entries("b")] == [None, "3"]
    assert ov.forget_messages("b", []) == 0 and ov.forget_messages("b", None) == 0
    print("PASS test_overlay_follows_a_merge_and_a_removal")


def test_overlay_keys_an_entry_by_its_chat():
    """A message id is unique within a conversation, not across them: one
    mail to two of the user's addresses is one Message-ID in two chats."""
    ov = cs.ChatOverlay(ttl=60.0)
    mine = {"chat_id": "email:~me@x.org:root@y.org", "message_id": "dup@y.org",
            "ts": cs.iso_z(5.0), "text": "to both"}
    theirs = dict(mine, chat_id="email:~other@x.org:root@y.org")
    ov.insert(mine)
    ov.insert(theirs)
    assert len(ov.entries(mine["chat_id"])) == 1 and len(ov.entries(theirs["chat_id"])) == 1
    assert cs.ChatOverlay.entry_key(mine) != cs.ChatOverlay.entry_key(theirs)
    # Within one chat a re-delivery still replaces.
    ov.insert(dict(mine, text="again"))
    assert [e["text"] for e in ov.entries(mine["chat_id"])] == ["again"]
    assert len(ov.entries()) == 2
    # A merge of the two (not that these two ever merge) keeps one.
    assert ov.rekey(theirs["chat_id"], mine["chat_id"]) == 1
    assert [e["text"] for e in ov.entries(mine["chat_id"])] == ["again"]
    print("PASS test_overlay_keys_an_entry_by_its_chat")


def test_an_arrival_that_is_gone_is_not_unread():
    with tempfile.TemporaryDirectory() as tmp:
        st = cs.ChatStateStore(tmp)
        cid = "email:~me@x.org:root@y.org"
        st.advance_last_read(cid, 100.0)
        # count_arrival says whether the event counted; mark_unread is its
        # two-value form.
        doc, had, counted = st.count_arrival(cid, 200.0, message_id="a@y.org")
        assert (had, counted) == (False, True) and doc["inbound_seq"] == 1
        doc, had, counted = st.count_arrival(cid, 200.0, message_id="a@y.org")
        assert (had, counted) == (True, False) and doc["inbound_seq"] == 1
        assert st.mark_unread(cid, 210.0, message_id="b@y.org")[1] is True
        st.mark_unread(cid, 220.0, message_id="c@y.org")
        assert cs.arrived_since_read(st.get(cid)) == 3

        # The middle one is deleted in a client: not unread, still counted —
        # so a client's `seen` and every remembered place stay true.
        doc = st.forget_arrivals(cid, ["b@y.org", "never-seen@y.org", None])
        assert doc["inbound_gone"] == [2] and doc["inbound_seq"] == 3
        assert cs.arrived_since_read(doc) == 2 and doc["unread_since"]
        assert cs.arrival_seq(doc, "c@y.org") == 3
        # Safe to repeat: nothing moves, nothing is written.
        before = st.get(cid)
        assert st.forget_arrivals(cid, ["b@y.org"]) == before and st.get(cid) == before
        # The rest go too: nothing unread is left, and the watermark of the
        # first unread message goes with them.
        doc = st.forget_arrivals(cid, ["a@y.org", "c@y.org"])
        assert cs.arrived_since_read(doc) == 0 and doc["unread_since"] is None
        # Announced again after it was gone, a message has come back (the
        # mail gateway re-announces a mail only then): it counts anew, at a
        # new place, and the old one stays gone.
        doc, _had, counted = st.count_arrival(cid, 230.0, message_id="b@y.org")
        assert counted and doc["inbound_seq"] == 4 and doc["inbound_gone"] == [1, 2, 3]
        assert cs.arrived_since_read(doc) == 1 and cs.arrival_seq(doc, "b@y.org") == 4
        assert [mid for mid, _seq in doc["inbound_recent"]].count("b@y.org") == 1
        # …and its own repeat is a repeat again.
        assert st.count_arrival(cid, 230.0, message_id="b@y.org")[2] is False

        # A read covers what it covered; a gone arrival behind it changes
        # nothing, and the undo brings back only what is still there.
        st.mark_unread(cid, 240.0, message_id="d@y.org")
        marked, doc, previous = st.mark_read_unless_arrived(cid, None, 5)
        assert marked and cs.arrived_since_read(doc) == 0
        st.forget_arrivals(cid, ["d@y.org"])
        restored, doc = st.restore_last_read(cid, restore=previous, marked=doc["last_read"])
        assert restored and cs.arrived_since_read(doc) == 1, "b came back; d is gone"

        # No document is made for a chat that has none, and ids a chat never
        # counted write nothing.
        st.forget_arrivals("email:~me@x.org:nobody@y.org", ["x@y.org"])
        assert not st.exists("email:~me@x.org:nobody@y.org")
        # A chat never read counts nothing by arrival, gone or not.
        st.mark_unread("email:~me@x.org:new@y.org", 10.0, message_id="n@y.org")
        assert cs.arrived_since_read(st.forget_arrivals("email:~me@x.org:new@y.org",
                                                         ["n@y.org"])) == 0
        # What is stored is read defensively.
        assert cs.gone_arrivals({"inbound_gone": [3, "x", True, 1, 3, -2, None]}) == [1, 3]
        assert cs.gone_arrivals(None) == [] and cs.gone_arrivals({"inbound_gone": "no"}) == []
    print("PASS test_an_arrival_that_is_gone_is_not_unread")


def test_merge_keeps_the_arrivals_places_true():
    """After a merge a place in the count still says read or unread, so a
    repeated event and a removal find the absorbed chat's messages too."""
    with tempfile.TemporaryDirectory() as tmp:
        st = cs.ChatStateStore(tmp)
        a, b = "email:~me@x.org:a@y.org", "email:~me@x.org:b@y.org"
        # The survivor: two read, then one unread. The absorbed chat: one
        # read, two unread — one of which is gone already.
        for n, ts in ((1, 10.0), (2, 20.0)):
            st.mark_unread(a, ts, message_id=f"a{n}")
        st.mark_read(a)
        st.mark_unread(a, 30.0, message_id="a3")
        st.mark_unread(b, 11.0, message_id="b1")
        st.mark_read(b)
        st.mark_unread(b, 21.0, message_id="b2")
        st.mark_unread(b, 31.0, message_id="b3")
        st.forget_arrivals(b, ["b2"])
        doc, gone = st.merge(b, a)
        assert gone is not None and doc["inbound_seq"] == 6 and doc["read_inbound"] == 3
        places = dict(doc["inbound_recent"])
        assert places == {"a1": 1, "a2": 2, "b1": 3, "a3": 4, "b2": 5, "b3": 6}, places
        assert [mid for mid, _seq in doc["inbound_recent"]] == ["a1", "a2", "b1", "a3", "b2", "b3"]
        assert doc["inbound_gone"] == [5]
        assert cs.arrived_since_read(doc) == 2, "a3 and b3; b2 was gone before the merge"
        # A repeated event for a message of either chat is a repeat.
        for mid in ("a3", "b1", "b3"):
            assert st.count_arrival(a, 40.0, message_id=mid)[2] is False, mid
        # A removal finds the absorbed chat's message, and a read one's
        # removal changes nothing.
        assert cs.arrived_since_read(st.forget_arrivals(a, ["b3"])) == 1
        assert cs.arrived_since_read(st.forget_arrivals(a, ["b1", "a1"])) == 1
        assert cs.arrived_since_read(st.forget_arrivals(a, ["a3"])) == 0

        # Neither chat ever read: no mark between them, the places simply
        # follow on.
        c, d = "email:~me@x.org:c@y.org", "email:~me@x.org:d@y.org"
        st.mark_unread(c, 10.0, message_id="c1")
        st.mark_unread(d, 11.0, message_id="d1")
        doc, _gone = st.merge(d, c)
        assert doc["read_inbound"] is None and dict(doc["inbound_recent"]) == {"c1": 1, "d1": 2}

        # A settled block of the absorbed chat is not taken over a survivor
        # that has none; an open one is, and the survivor's open one stays.
        e, f = "email:~me@x.org:e@y.org", "email:~me@x.org:f@y.org"
        st.mark_unread(e, 10.0, message_id="e1")
        st.set_attention(f, {"state": "done", "done_how": "marked"})
        assert st.merge(f, e)[0]["attention"] is None
        st.set_attention(f, {"state": "open", "importance": 2.0})
        st.set_attention(e, {"state": "done"})
        assert st.merge(f, e)[0]["attention"] == {"state": "open", "importance": 2.0}
        st.set_attention(f, {"state": "open", "importance": 5.0})
        assert st.merge(f, e)[0]["attention"]["importance"] == 2.0
    print("PASS test_merge_keeps_the_arrivals_places_true")


def test_a_merged_chats_id_says_where_it_went():
    with tempfile.TemporaryDirectory() as tmp:
        st = cs.ChatStateStore(tmp)
        a, b, c, d = (f"email:~me@x.org:{k}@y.org" for k in "abcd")
        assert st.merged_into(a) is None and st.merged_away() == {}
        st.note_message(a, name="A")
        st.note_message(b, name="B")
        st.merge(b, a)
        st.note_merged(b, a)
        assert st.merged_into(b) == a and st.merged_into(a) is None
        assert st.merged_away() == {b: a}
        # Not a chat document: all() lists chats, and b is none.
        assert set(st.all()) == {a}
        # Idempotent, and nothing is noted about a chat and itself.
        index = (Path(tmp) / "merged" / "into.json").read_text()
        st.note_merged(b, a)
        st.note_merged(a, a)
        st.note_merged("", a)
        assert (Path(tmp) / "merged" / "into.json").read_text() == index
        # A chat that never had a document can be noted all the same (its
        # messages were in the ledger).
        st.note_merged(d, a)
        assert st.merged_into(d) == a
        # The survivor is absorbed in turn: everything folded into it follows.
        st.note_message(c, name="C")
        st.merge(a, c)
        st.note_merged(a, c)
        assert st.merged_into(b) == c and st.merged_into(d) == c and st.merged_into(a) == c
        assert st.merged_away() == {a: c, b: c, d: c}
        # An id that has a document again is a chat again, whatever is noted
        # — and what was folded into it leads there, not past it.
        st.note_message(a, name="A again")
        assert st.merged_into(a) is None and st.merged_into(b) == a
        assert st.merged_away() == {b: a, d: a}
        st.delete(a)
        assert st.merged_away() == {}, "a delete forgets both sides of a merge"
        # A chat absorbed by the one it once absorbed: no loop, and the
        # survivor's own note is dropped.
        st.note_message(a, name="A")
        st.note_merged(b, a)
        st.note_message(b, name="B")
        st.merge(a, b)
        st.note_merged(a, b)
        assert st.merged_into(a) == b and st.merged_into(b) is None
        # Deleting the chat at the end of a chain forgets the whole chain.
        st.note_merged(d, a)
        assert st.merged_into(d) == b
        st.delete(b)
        assert st.merged_into(d) is None and st.merged_into(a) is None
        assert not (Path(tmp) / "merged" / "into.json").exists()
        # A damaged index is no index, and a loop in one ends.
        path = Path(tmp) / "merged" / "into.json"
        path.write_text('{"x": {"into": "y"}, "y": {"into": "x"}, "z": "junk", "w": {"into": 5}}')
        assert st.merged_into("x") == "y" and st.merged_into("z") is None
        assert st.merged_into("w") is None
        path.write_text("not json")
        assert st.merged_into("x") is None and st.merged_away() == {}
        # Bounded: the newest notes are kept.
        was, cs.MERGED_MAX = cs.MERGED_MAX, 3
        try:
            for n in range(5):
                st.note_merged(f"email:~me@x.org:old{n}@y.org", c)
            assert len(st.merged_away()) == 3
        finally:
            cs.MERGED_MAX = was
    print("PASS test_a_merged_chats_id_says_where_it_went")


if __name__ == "__main__":
    test_split_and_filename_safety()
    test_account_is_half_the_identity()
    test_draft_version_guard()
    test_undo_clear_restores_the_draft_with_its_author()
    test_undo_clear_is_refused_once_it_is_stale_or_moot()
    test_agent_staging_never_clobbers_user_text()
    test_last_read_forward_only_and_unread_modes()
    test_mark_read_and_its_guarded_restore()
    test_arrival_order_decides_what_came_after_a_read()
    test_the_undo_restores_the_arrival_count_too()
    test_a_repeated_event_is_one_arrival()
    test_a_read_reaches_a_stamp_from_a_fast_clock()
    test_archive_unless_arrived()
    test_flags_and_note_message()
    test_companion_link()
    test_overlay_merge_dedup_expiry()
    test_iso_z_normalizes()
    test_delete_forgets_the_chat()
    test_a_mail_chat_keeps_its_subject_and_its_people()
    test_merge_folds_one_chat_into_another()
    test_overlay_follows_a_merge_and_a_removal()
    test_overlay_keys_an_entry_by_its_chat()
    test_an_arrival_that_is_gone_is_not_unread()
    test_merge_keeps_the_arrivals_places_true()
    test_a_merged_chats_id_says_where_it_went()
    print("all chat-state tests passed")
