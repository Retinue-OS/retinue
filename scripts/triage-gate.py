#!/usr/bin/env python3
"""Retired: the e-mail triage gate. This file is a stub that does nothing.

It used to be the scheduler `command` job chambers declared for e-mail triage.
Mail is now mirrored into chats and gated on arrival by the mail gateway
(docs/mail-chats.md, "The gate"), and what this gate decided lives on contacts:
a person's VIP flag, their instruction, their e-mail domains.

The file stays because the jobs that call it live in chambers'
`.schedule.json`, outside this repository: until an operator removes them the
scheduler keeps running this command. So **any** invocation — whatever the
mode or arguments — prints one line saying so and exits 0: a job left behind
is a notice in its log, never a failing job, and never a second hand on a
mailbox the mail gateway owns. It reads no mailbox, writes no file and starts
no session.
"""

from __future__ import annotations

import sys

NOTICE = (
    "[triage-gate] e-mail triage was retired: mail is mirrored and gated by "
    "the mail gateway (docs/mail-chats.md). Nothing was done; remove this job "
    "from the chamber's .schedule.json."
)


def main(argv: list[str] | None = None) -> int:
    # The arguments are deliberately not parsed: an old job's mode, a flag
    # this stub never knew, `--help` — all get the same answer and the same
    # exit code, so no job left in a chamber can fail on it.
    print(NOTICE, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
