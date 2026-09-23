#!/usr/bin/env python3
"""What code is this container actually running?

A merge is not a deployment. The framework ships as five images built from one
checkout, and `self-update.py` rebuilds them together — but until it runs, the
running containers carry whatever was merged *last* time. Twice now that gap
has looked exactly like a logic bug: a behaviour the code no longer contains
kept happening, and the diagnosis cost a session each time. This module makes
it a five-second check instead.

Two identifiers, and they answer different questions:

* **``sha``** — the commit the image was built from, when the builder says so
  (``RETINUE_BUILD_SHA``, set as a build ARG; see the Dockerfiles). Readable,
  and it links straight to a diff. It is ``None`` when nobody passed it, and
  that is reported honestly rather than guessed at: an image cannot know its
  own provenance unless the build tells it.

* **``framework``** — a digest of the shared framework modules *as they were
  actually baked in*, computed at runtime from the files on disk. It needs no
  cooperation from the build pipeline at all, so it is always there, and it is
  the one that catches the failure above: the retinue container and the three
  messenger gateways copy the **same** modules, so if their digests
  differ, one of those images is older than the other. The web-gateway compares
  them on the ``/gateways`` page.

The digest is keyed on each module's **basename**, not its path, precisely so
the two sides compare: a gateway has them at ``/app/triage_policy.py`` and the
retinue container at ``/workspace/scripts/triage_policy.py``, and it is the
same file either way.

A missing or unreadable module makes the digest ``None`` rather than a hash of
whatever was found. A partial set would hash to *something*, and that something
would read as "different code" — a false alarm every time a container legitimately
carries a subset. ``None`` means "cannot say", which is what it is: a container that
carries none of these modules — the CalDAV gateway, say — simply has no
framework digest. (That one never reaches the ``/gateways`` page, which lists
the messenger channels; the page's own "cannot say" case is a gateway built
before this field existed.)
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# The modules every messenger gateway's Dockerfile copies, and which the
# retinue container carries under /workspace/scripts. Adding one here without
# adding it to all three gateway Dockerfiles turns every gateway's digest into
# None — tests/test_build_stamp.py fails the build for exactly that, so the
# list and the COPY lines cannot drift apart silently.
FRAMEWORK_MODULES: tuple[str, ...] = (
    # This module is one of them: it is copied into every gateway like the
    # rest, so a build in which *it* changed is a different build, and a digest
    # that left itself out would call those two images identical.
    "build_stamp.py",
    "chat_ingest.py",
    "inbound_store.py",
    "job_delivery.py",
    "news_ingest.py",
    "reply_tokens.py",
    "requester_identity.py",
    "triage_policy.py",
)

# How much of the sha256 is kept. Twelve hex digits is what git shows and what
# a person can compare at a glance on a phone; this identifies a build, it does
# not authenticate one.
DIGEST_CHARS = 12

_cache: dict[str, object] = {}


def digest(paths: list[Path] | tuple[Path, ...]) -> str | None:
    """Digest a set of files by ``(basename, contents)``, or None if any is missing.

    Order-independent (the names are sorted first) and length-framed, so two
    files cannot be concatenated into a third's contents.
    """
    entries = []
    for path in sorted(paths, key=lambda p: p.name):
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        entries.append((path.name, blob))
    if not entries:
        return None
    h = hashlib.sha256()
    for name, blob in entries:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(blob)).encode("ascii"))
        h.update(b"\0")
        h.update(blob)
    return h.hexdigest()[:DIGEST_CHARS]


def framework_stamp(directory: str | Path | None = None) -> str | None:
    """The framework digest for the modules in `directory`.

    Defaults to this module's own directory, which is where its siblings are in
    every image that carries it: ``/app`` in a gateway, ``/workspace/scripts``
    in the retinue container.
    """
    base = Path(directory) if directory is not None else Path(__file__).resolve().parent
    return digest([base / name for name in FRAMEWORK_MODULES])


def build_sha() -> str | None:
    """The commit this image was built from, if the build said so."""
    return os.environ.get("RETINUE_BUILD_SHA", "").strip() or None


def build_info(directory: str | Path | None = None) -> dict:
    """The ``build`` block for a ``/health`` payload.

    Cached: the files cannot change under a running container, and /health is
    polled by the monitor, the dashboard and every gateway card on the page.
    """
    key = str(directory)
    cached = _cache.get(key)
    if cached is None:
        cached = {"sha": build_sha(), "framework": framework_stamp(directory)}
        _cache[key] = cached
    return dict(cached)  # a copy: callers put this straight into a JSON body


def _main(argv: list[str]) -> int:
    """`python3 scripts/build_stamp.py [directory]` — print what a checkout bakes.

    Run against a checkout it prints the digest that an image built from that
    checkout will report, so a rebuild can be confirmed without shelling into
    anything: build, then compare this against each gateway's /health.
    """
    directory = argv[1] if len(argv) > 1 else None
    info = build_info(directory)
    print(f"framework\t{info['framework'] or '(incomplete — modules missing)'}")
    print(f"sha\t\t{info['sha'] or '(not stamped)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    import sys
    raise SystemExit(_main(sys.argv))
