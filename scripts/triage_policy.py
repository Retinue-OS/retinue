#!/usr/bin/env python3
"""Triage delivery-gate policy: sender whitelist/blacklist + group flags as N-Triples.

This is the single source of truth for *who is worth a model turn* in the triage
delivery gate (see `docs/triage-delivery-gate.md`). It is deliberately
stdlib-only so the messenger gateways can import a byte-identical copy and read
policy straight off their mounted volume, without the ~15 s SPARQL reindex lag on
their classify hot path.

Two kinds of policy live here:

  * **E-mail whitelist** — exact addresses (auto-derived from the Sent folder) and
    hand-added `*@domain` / `*@*.domain` wildcards. Only exact addresses are ever
    auto-added; a whole domain is trusted only when someone writes the wildcard.
    That is what keeps freemail safe: sending to one `person@gmail.com` whitelists
    that address, never all of `gmail.com`.
  * **Messenger policy** — per channel, on two orthogonal axes:
      - **Sender** (a handle): whitelisted / blacklisted / unknown. A whitelisted
        handle is forwarded to triage immediately regardless of its group; a
        blacklisted handle is never forwarded live (the daily drain still picks
        it up); an unknown handle is governed by its group (below).
      - **Group** (three independent flags): ``news`` — its messages are also
        forwarded to the news feed (Herald), a rail parallel to triage;
        ``quieted`` — an *unknown* sender in it is not forwarded live but is
        drained daily; ``ignored`` — an unknown sender in it never reaches triage
        at all (accounted for, never drained). ``quieted`` and ``ignored`` bite
        only for unknown senders; ``news`` is independent of all of it. The
        legacy ``triageBlockedGroup`` predicate is read as ``ignored``.

Everything is emitted as sorted, deterministic N-Triples with the same
write-if-changed discipline as `discover-agents.py`, so an unchanged policy never
triggers a qlever-dir rebuild, and the very same file the gateway reads raw is
indexed in the life store for `who is whitelisted?` queries.

Retinue (Ara) is the sole writer; the gateways are readers. The `_generated`
paths are framework-owned, in no chamber's git repo.
"""

from __future__ import annotations

import argparse
import email.utils
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"

# Predicates. One flat subject per policy file, repeated predicates for members.
P_ADDRESS = KB + "triageWhitelistAddress"
P_WILDCARD = KB + "triageWhitelistWildcard"
P_HANDLE = KB + "triageWhitelistHandle"
P_BLOCKED_HANDLE = KB + "triageBlacklistHandle"
# Group flags — three orthogonal axes (see module docstring).
P_IGNORED_GROUP = KB + "triageIgnoredGroup"
P_QUIETED_GROUP = KB + "triageQuietedGroup"
P_NEWS_GROUP = KB + "triageNewsGroup"
# Legacy: the original single "blocked group" flag, now read as `ignored`. Still
# parsed so existing policy files keep working; render migrates it to
# P_IGNORED_GROUP on the next write.
P_BLOCKED_GROUP = KB + "triageBlockedGroup"

EMAIL_SUBJECT = "urn:retinue:triage:email-whitelist"

# The loaded messenger policy: two sender sets plus the three group-flag sets.
MessengerPolicy = namedtuple(
    "MessengerPolicy", "whitelist blacklist ignored quieted news"
)


def _channel_subject(channel: str) -> str:
    return f"urn:retinue:triage:{channel}"


CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")


def email_whitelist_path() -> Path:
    """Where the e-mail whitelist `.nt` lives (retinue side, indexed by qlever)."""
    return Path(
        os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH")
        or (CHAMBERS_DIR / "_generated" / "triage" / "email-whitelist.nt")
    )


def messenger_policy_path(channel: str) -> Path:
    """Where a channel's policy `.nt` lives on the retinue side.

    This is the retinue writer's view. Each gateway reads the *same file* through
    its own volume mount (env `INBOUND_POLICY_PATH`), so the gateway never needs
    this path.

    The file sits in a ``policy/`` subdirectory of the channel's directory, a
    sibling of the gateway-written ``messages/``. That folder split is the
    single-writer-per-file boundary: Ara owns ``policy/``, the gateway owns
    ``messages/``, and both live in the one per-gateway volume qlever indexes
    read-only. The gateway's default ``INBOUND_POLICY_PATH`` mirrors this
    (``<store>/policy/policy.nt``), so writer and reader agree with no config.
    """
    base = os.environ.get("TRIAGE_MESSENGER_DIR")
    root = Path(base) if base else (CHAMBERS_DIR / "_generated" / "messenger")
    return root / channel / "policy" / "policy.nt"


# --------------------------------------------------------------------------- #
# N-Triples I/O                                                               #
# --------------------------------------------------------------------------- #

def _nt_string(value: str) -> str:
    """Escape a Python string as an N-Triples literal (RDF 1.1 §7.2)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def _triple(subject: str, predicate: str, literal: str) -> str:
    return f"<{subject}> <{predicate}> {_nt_string(literal)} ."


_TRIPLE_RE = re.compile(r'^<([^>]+)>\s+<([^>]+)>\s+"(.*)"\s*\.\s*$')


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append(
                {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt)
            )
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse(path: Path) -> list[tuple[str, str, str]]:
    """Parse our own deterministic N-Triples into (subject, predicate, literal)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    triples: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        m = _TRIPLE_RE.match(line.strip())
        if m:
            triples.append((m.group(1), m.group(2), _unescape(m.group(3))))
    return triples


def write_if_changed(content: str, path: Path) -> bool:
    """Atomically write only when bytes differ. Returns True if it wrote."""
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


# --------------------------------------------------------------------------- #
# E-mail whitelist                                                            #
# --------------------------------------------------------------------------- #

def load_email_whitelist(path: Path | None = None) -> tuple[set[str], set[str]]:
    """Return (exact addresses, wildcards), both lower-cased."""
    path = path or email_whitelist_path()
    addresses: set[str] = set()
    wildcards: set[str] = set()
    for _subj, pred, lit in _parse(path):
        low = lit.strip().lower()
        if not low:
            continue
        if pred == P_ADDRESS:
            addresses.add(low)
        elif pred == P_WILDCARD:
            wildcards.add(low)
    return addresses, wildcards


def render_email_whitelist(addresses: set[str], wildcards: set[str]) -> str:
    lines = [_triple(EMAIL_SUBJECT, P_ADDRESS, a) for a in addresses]
    lines += [_triple(EMAIL_SUBJECT, P_WILDCARD, w) for w in wildcards]
    lines.sort()
    return "".join(line + "\n" for line in lines)


def _domain_matches_wildcard(domain: str, wildcard: str) -> bool:
    """Match a domain against a `*@domain` or `*@*.domain` wildcard."""
    try:
        pattern = wildcard.split("@", 1)[1]
    except IndexError:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        # A subdomain wildcard also covers the apex, so `*@*.epfl.ch` trusts both
        # alice@epfl.ch and bob@cs.epfl.ch — the friendlier reading for a
        # whitelist.
        return domain == base or domain.endswith("." + base)
    return domain == pattern


def email_whitelisted(addr: str, addresses: set[str], wildcards: set[str]) -> bool:
    """True if `addr` is an exact whitelist entry or falls under a wildcard."""
    low = (addr or "").strip().lower()
    if not low or "@" not in low:
        return False
    if low in addresses:
        return True
    domain = low.split("@", 1)[1]
    return any(_domain_matches_wildcard(domain, w) for w in wildcards)


def recipients_from_sent(messages: list[dict]) -> set[str]:
    """Extract every recipient address from a Sent-folder listing.

    `messages` is the `messages` array from `email_client.py list`, each carrying
    `to`/`cc`/`bcc` as raw address-list headers.
    """
    out: set[str] = set()
    for msg in messages:
        for field in ("to", "cc", "bcc"):
            raw = msg.get(field)
            if not raw:
                continue
            for _name, addr in email.utils.getaddresses([raw]):
                addr = (addr or "").strip().lower()
                if addr and "@" in addr:
                    out.add(addr)
    return out


# --------------------------------------------------------------------------- #
# Messenger policy                                                            #
# --------------------------------------------------------------------------- #

def _norm_handle(handle: str) -> str:
    # Phone numbers/JIDs are case-insensitive digits; usernames case-insensitive.
    return (handle or "").strip().lower()


def load_messenger_policy(
    channel: str, path: Path | None = None
) -> MessengerPolicy:
    """Return a :class:`MessengerPolicy` for one channel.

    The legacy ``triageBlockedGroup`` predicate is folded into ``ignored`` so a
    policy file written before the news/quieted/ignored split keeps its old
    behaviour (blocked == never reaches triage).
    """
    path = path or messenger_policy_path(channel)
    whitelist: set[str] = set()
    blacklist: set[str] = set()
    ignored: set[str] = set()
    quieted: set[str] = set()
    news: set[str] = set()
    for _subj, pred, lit in _parse(path):
        val = lit.strip()
        if not val:
            continue
        if pred == P_HANDLE:
            whitelist.add(_norm_handle(val))
        elif pred == P_BLOCKED_HANDLE:
            blacklist.add(_norm_handle(val))
        elif pred in (P_IGNORED_GROUP, P_BLOCKED_GROUP):
            ignored.add(val)
        elif pred == P_QUIETED_GROUP:
            quieted.add(val)
        elif pred == P_NEWS_GROUP:
            news.add(val)
    return MessengerPolicy(whitelist, blacklist, ignored, quieted, news)


def render_messenger_policy(channel: str, pol: MessengerPolicy) -> str:
    subj = _channel_subject(channel)
    lines = [_triple(subj, P_HANDLE, h) for h in pol.whitelist]
    lines += [_triple(subj, P_BLOCKED_HANDLE, h) for h in pol.blacklist]
    lines += [_triple(subj, P_IGNORED_GROUP, g) for g in pol.ignored]
    lines += [_triple(subj, P_QUIETED_GROUP, g) for g in pol.quieted]
    lines += [_triple(subj, P_NEWS_GROUP, g) for g in pol.news]
    lines.sort()
    return "".join(line + "\n" for line in lines)


def handle_status(handle: str, whitelist: set[str], blacklist: set[str]) -> str:
    """'blacklisted' | 'whitelisted' | 'unknown'. Blacklist wins over whitelist."""
    norm = _norm_handle(handle)
    if norm in blacklist:
        return "blacklisted"
    if norm in whitelist:
        return "whitelisted"
    return "unknown"


def gate_decision(
    channel: str,
    sender: str,
    group_id: str | None = None,
    *,
    path: Path | None = None,
    enabled: bool = True,
) -> dict:
    """Route one inbound messenger message against the policy.

    This is the single source of truth for the delivery-gate routing table (see
    docs/triage-delivery-gate.md); all three gateways call it so they can never
    drift. Returns a dict with:

    - ``forward`` — spend a model turn on this message now (the triage rail).
    - ``flagged_unknown`` — annotate that turn as an unknown sender (ask the user
      whether to whitelist/blacklist the handle).
    - ``delivered_if_held`` — the ``delivered`` flag to persist when NOT
      forwarding: ``True`` means "accounted for, never drained" (ignored group),
      ``False`` means "held, the daily drain picks it up" (blacklisted handle or
      quieted group).
    - ``news`` — the message's group is a news source: forward it to the news
      feed (Herald) too. This rail is *independent* of the triage decision above
      (a message can be both, either, or neither).
    - ``reason`` — a short label for the gateway log.

    Triage rail (news rail is orthogonal, driven only by group ∈ news):

    | class                    | forward | flagged | held-flag | drained daily |
    |--------------------------|---------|---------|-----------|---------------|
    | whitelisted handle       | yes     | no      | —         | —             |
    | blacklisted handle       | no      | no      | false     | yes           |
    | unknown, normal group    | yes     | yes     | —         | —             |
    | unknown, quieted group   | no      | no      | false     | yes           |
    | unknown, ignored group   | no      | no      | true      | no            |

    Whitelist/blacklist are sender-level and win over the group's quieted/ignored
    flag; quieted/ignored bite only for unknown senders — matching the user's
    model ("new senders in quieted or ignored groups").

    ``enabled=False`` forwards everything (the gate turned off). May raise if the
    policy file is present but unreadable; the caller decides fail-open.
    """
    if not enabled:
        return {"forward": True, "flagged_unknown": False,
                "delivered_if_held": True, "news": False, "reason": "gate-disabled"}
    pol = load_messenger_policy(channel, path=path)
    grp = group_id.strip() if group_id else None
    news = bool(grp and grp in pol.news)
    status = handle_status(sender, pol.whitelist, pol.blacklist)

    if status == "whitelisted":
        dec = {"forward": True, "flagged_unknown": False,
               "delivered_if_held": True, "reason": "whitelisted"}
    elif status == "blacklisted":
        dec = {"forward": False, "flagged_unknown": False,
               "delivered_if_held": False, "reason": "blacklisted"}
    elif grp and grp in pol.ignored:
        dec = {"forward": False, "flagged_unknown": False,
               "delivered_if_held": True, "reason": "group-ignored"}
    elif grp and grp in pol.quieted:
        dec = {"forward": False, "flagged_unknown": False,
               "delivered_if_held": False, "reason": "group-quieted"}
    else:
        dec = {"forward": True, "flagged_unknown": True,
               "delivered_if_held": True, "reason": "unknown"}

    dec["news"] = news
    return dec


def auto_whitelist_on_send(channel: str, handles) -> list[str]:
    """Whitelist recipient handle(s) after an outbound 1:1 messenger send.

    This is the messenger analogue of the e-mail Sent-folder auto-whitelist
    (see ``load_email_whitelist``): sending to someone is standing proof they
    are a wanted correspondent, so their reply must count as a *known* sender
    rather than resurface as an "unknown sender" prompt. Each gateway calls this
    from its send choke point once a send has actually gone out.

    Idempotent and write-if-changed (an already-known handle is a no-op, so no
    qlever rebuild churn). A handle currently on the *blacklist* is never
    re-whitelisted — an explicit block must survive an outbound send. Returns the
    handles newly added (empty when all were already known or blocked).
    """
    norm = {_norm_handle(h) for h in handles if h and str(h).strip()}
    if not norm:
        return []
    pol = load_messenger_policy(channel)
    added = sorted(norm - pol.whitelist - pol.blacklist)
    if not added:
        return []
    write_if_changed(
        render_messenger_policy(channel, pol._replace(whitelist=pol.whitelist | set(added))),
        messenger_policy_path(channel),
    )
    return added


# --------------------------------------------------------------------------- #
# CLI — the deterministic editor Ara and the gate use                         #
# --------------------------------------------------------------------------- #

def _mutate_email(add_addresses=(), add_wildcards=(), remove=()) -> None:
    addresses, wildcards = load_email_whitelist()
    addresses |= {a.strip().lower() for a in add_addresses if a.strip()}
    wildcards |= {w.strip().lower() for w in add_wildcards if w.strip()}
    for entry in remove:
        e = entry.strip().lower()
        addresses.discard(e)
        wildcards.discard(e)
    write_if_changed(render_email_whitelist(addresses, wildcards), email_whitelist_path())


def _mutate_messenger(channel, *, wl_add=(), wl_del=(), bl_add=(), bl_del=(),
                      ig_add=(), ig_del=(), q_add=(), q_del=(),
                      news_add=(), news_del=()) -> None:
    pol = load_messenger_policy(channel)
    whitelist = set(pol.whitelist)
    blacklist = set(pol.blacklist)
    ignored = set(pol.ignored)
    quieted = set(pol.quieted)
    news = set(pol.news)
    whitelist |= {_norm_handle(h) for h in wl_add if h.strip()}
    whitelist -= {_norm_handle(h) for h in wl_del}
    blacklist |= {_norm_handle(h) for h in bl_add if h.strip()}
    blacklist -= {_norm_handle(h) for h in bl_del}
    ignored |= {g.strip() for g in ig_add if g.strip()}
    ignored -= {g.strip() for g in ig_del}
    quieted |= {g.strip() for g in q_add if g.strip()}
    quieted -= {g.strip() for g in q_del}
    news |= {g.strip() for g in news_add if g.strip()}
    news -= {g.strip() for g in news_del}
    # A group carries at most one of ignored/quieted at a time: adding one clears
    # the other, so `quiet-add` on an ignored group moves it rather than doubling.
    ignored -= {g.strip() for g in q_add if g.strip()}
    quieted -= {g.strip() for g in ig_add if g.strip()}
    write_if_changed(
        render_messenger_policy(
            channel, MessengerPolicy(whitelist, blacklist, ignored, quieted, news)
        ),
        messenger_policy_path(channel),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triage delivery-gate policy editor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show-email")
    p = sub.add_parser("email-add-address"); p.add_argument("address", nargs="+")
    p = sub.add_parser("email-add-wildcard"); p.add_argument("wildcard", nargs="+")
    p = sub.add_parser("email-remove"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("check-email"); p.add_argument("address")

    # Handle commands take --handle; group commands take --group. `groupblock-*`
    # is kept as a legacy alias of `ignore-*`.
    handle_cmds = ("whitelist-add", "whitelist-remove",
                   "blacklist-add", "blacklist-remove")
    group_cmds = ("ignore-add", "ignore-remove", "quiet-add", "quiet-remove",
                  "news-add", "news-remove", "groupblock-add", "groupblock-remove")
    for name in ("show",) + handle_cmds + group_cmds:
        p = sub.add_parser(name)
        p.add_argument("--channel", required=True)
        if name in handle_cmds:
            p.add_argument("--handle", action="append", default=[])
        if name in group_cmds:
            p.add_argument("--group", action="append", default=[])
    p = sub.add_parser("check-handle")
    p.add_argument("--channel", required=True)
    p.add_argument("handle")

    args = parser.parse_args(argv)

    if args.cmd == "show-email":
        addresses, wildcards = load_email_whitelist()
        for a in sorted(addresses):
            print(a)
        for w in sorted(wildcards):
            print(w)
    elif args.cmd == "email-add-address":
        _mutate_email(add_addresses=args.address)
    elif args.cmd == "email-add-wildcard":
        _mutate_email(add_wildcards=args.wildcard)
    elif args.cmd == "email-remove":
        _mutate_email(remove=args.entry)
    elif args.cmd == "check-email":
        addresses, wildcards = load_email_whitelist()
        ok = email_whitelisted(args.address, addresses, wildcards)
        print("whitelisted" if ok else "not-whitelisted")
        return 0 if ok else 3
    elif args.cmd == "show":
        pol = load_messenger_policy(args.channel)
        for h in sorted(pol.whitelist):
            print(f"whitelist\t{h}")
        for h in sorted(pol.blacklist):
            print(f"blacklist\t{h}")
        for g in sorted(pol.ignored):
            print(f"ignore\t{g}")
        for g in sorted(pol.quieted):
            print(f"quiet\t{g}")
        for g in sorted(pol.news):
            print(f"news\t{g}")
    elif args.cmd == "whitelist-add":
        _mutate_messenger(args.channel, wl_add=args.handle)
    elif args.cmd == "whitelist-remove":
        _mutate_messenger(args.channel, wl_del=args.handle)
    elif args.cmd == "blacklist-add":
        _mutate_messenger(args.channel, bl_add=args.handle)
    elif args.cmd == "blacklist-remove":
        _mutate_messenger(args.channel, bl_del=args.handle)
    elif args.cmd in ("ignore-add", "groupblock-add"):
        _mutate_messenger(args.channel, ig_add=args.group)
    elif args.cmd in ("ignore-remove", "groupblock-remove"):
        _mutate_messenger(args.channel, ig_del=args.group)
    elif args.cmd == "quiet-add":
        _mutate_messenger(args.channel, q_add=args.group)
    elif args.cmd == "quiet-remove":
        _mutate_messenger(args.channel, q_del=args.group)
    elif args.cmd == "news-add":
        _mutate_messenger(args.channel, news_add=args.group)
    elif args.cmd == "news-remove":
        _mutate_messenger(args.channel, news_del=args.group)
    elif args.cmd == "check-handle":
        pol = load_messenger_policy(args.channel)
        status = handle_status(args.handle, pol.whitelist, pol.blacklist)
        print(status)
        return 0 if status == "whitelisted" else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
