#!/usr/bin/env python3
"""Triage delivery-gate policy: whitelist / blacklist / group-block as N-Triples.

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
  * **Messenger policy** — per channel: whitelisted handles, blacklisted handles
    (an unknown sender the user declined, never asked about again), and blocked
    groups (never trigger an unknown-sender prompt).

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
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"

# Predicates. One flat subject per policy file, repeated predicates for members.
P_ADDRESS = KB + "triageWhitelistAddress"
P_WILDCARD = KB + "triageWhitelistWildcard"
P_HANDLE = KB + "triageWhitelistHandle"
P_BLOCKED_HANDLE = KB + "triageBlacklistHandle"
P_BLOCKED_GROUP = KB + "triageBlockedGroup"

EMAIL_SUBJECT = "urn:retinue:triage:email-whitelist"


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
) -> tuple[set[str], set[str], set[str]]:
    """Return (whitelisted handles, blacklisted handles, blocked groups)."""
    path = path or messenger_policy_path(channel)
    whitelist: set[str] = set()
    blacklist: set[str] = set()
    groups: set[str] = set()
    for _subj, pred, lit in _parse(path):
        val = lit.strip()
        if not val:
            continue
        if pred == P_HANDLE:
            whitelist.add(_norm_handle(val))
        elif pred == P_BLOCKED_HANDLE:
            blacklist.add(_norm_handle(val))
        elif pred == P_BLOCKED_GROUP:
            groups.add(val.strip())
    return whitelist, blacklist, groups


def render_messenger_policy(
    channel: str, whitelist: set[str], blacklist: set[str], groups: set[str]
) -> str:
    subj = _channel_subject(channel)
    lines = [_triple(subj, P_HANDLE, h) for h in whitelist]
    lines += [_triple(subj, P_BLOCKED_HANDLE, h) for h in blacklist]
    lines += [_triple(subj, P_BLOCKED_GROUP, g) for g in groups]
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


def group_blocked(group: str, blocked_groups: set[str]) -> bool:
    return bool(group) and group.strip() in blocked_groups


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

    - ``forward`` — spend a model turn on this message now.
    - ``flagged_unknown`` — annotate that turn as an unknown sender (ask the user
      whether to whitelist/blacklist the handle).
    - ``delivered_if_held`` — the ``delivered`` flag to persist when NOT
      forwarding: ``True`` means "accounted for, never drained" (group-blocked),
      ``False`` means "held, the daily drain picks it up" (blacklisted).
    - ``reason`` — a short label for the gateway log.

    | class          | forward | flagged | held-flag | drained daily |
    |----------------|---------|---------|-----------|---------------|
    | whitelisted    | yes     | no      | —         | —             |
    | unknown        | yes     | yes     | —         | —             |
    | blacklisted    | no      | no      | false     | yes           |
    | group-blocked  | no      | no      | true      | no            |

    ``enabled=False`` forwards everything (the gate turned off). May raise if the
    policy file is present but unreadable; the caller decides fail-open.
    """
    if not enabled:
        return {"forward": True, "flagged_unknown": False,
                "delivered_if_held": True, "reason": "gate-disabled"}
    whitelist, blacklist, groups = load_messenger_policy(channel, path=path)
    if group_id and group_blocked(group_id, groups):
        return {"forward": False, "flagged_unknown": False,
                "delivered_if_held": True, "reason": "group-blocked"}
    status = handle_status(sender, whitelist, blacklist)
    if status == "blacklisted":
        return {"forward": False, "flagged_unknown": False,
                "delivered_if_held": False, "reason": "blacklisted"}
    if status == "whitelisted":
        return {"forward": True, "flagged_unknown": False,
                "delivered_if_held": True, "reason": "whitelisted"}
    return {"forward": True, "flagged_unknown": True,
            "delivered_if_held": True, "reason": "unknown"}


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
    whitelist, blacklist, groups = load_messenger_policy(channel)
    added = sorted(norm - whitelist - blacklist)
    if not added:
        return []
    whitelist |= set(added)
    write_if_changed(
        render_messenger_policy(channel, whitelist, blacklist, groups),
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
                      grp_add=(), grp_del=()) -> None:
    whitelist, blacklist, groups = load_messenger_policy(channel)
    whitelist |= {_norm_handle(h) for h in wl_add if h.strip()}
    whitelist -= {_norm_handle(h) for h in wl_del}
    blacklist |= {_norm_handle(h) for h in bl_add if h.strip()}
    blacklist -= {_norm_handle(h) for h in bl_del}
    groups |= {g.strip() for g in grp_add if g.strip()}
    groups -= {g.strip() for g in grp_del}
    write_if_changed(
        render_messenger_policy(channel, whitelist, blacklist, groups),
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

    for name in ("show", "whitelist-add", "whitelist-remove", "blacklist-add",
                 "blacklist-remove", "groupblock-add", "groupblock-remove"):
        p = sub.add_parser(name)
        p.add_argument("--channel", required=True)
        if name.endswith(("-add", "-remove")) and "group" not in name:
            p.add_argument("--handle", action="append", default=[])
        if "group" in name:
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
        wl, bl, grp = load_messenger_policy(args.channel)
        for h in sorted(wl):
            print(f"whitelist\t{h}")
        for h in sorted(bl):
            print(f"blacklist\t{h}")
        for g in sorted(grp):
            print(f"groupblock\t{g}")
    elif args.cmd == "whitelist-add":
        _mutate_messenger(args.channel, wl_add=args.handle)
    elif args.cmd == "whitelist-remove":
        _mutate_messenger(args.channel, wl_del=args.handle)
    elif args.cmd == "blacklist-add":
        _mutate_messenger(args.channel, bl_add=args.handle)
    elif args.cmd == "blacklist-remove":
        _mutate_messenger(args.channel, bl_del=args.handle)
    elif args.cmd == "groupblock-add":
        _mutate_messenger(args.channel, grp_add=args.group)
    elif args.cmd == "groupblock-remove":
        _mutate_messenger(args.channel, grp_del=args.group)
    elif args.cmd == "check-handle":
        wl, bl, _grp = load_messenger_policy(args.channel)
        status = handle_status(args.handle, wl, bl)
        print(status)
        return 0 if status == "whitelisted" else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
