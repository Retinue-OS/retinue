#!/usr/bin/env python3
"""Checks for the intake watcher's supervisor (scripts/letterbox-supervise.sh).

The watcher must not stay dead (it quarantines reserved names and keeps
accepted files from the sweep), and a watcher that dies at once every time
must not be restarted forever with nobody told. The script is driven here
with stand-in commands through its environment, from a parent that runs
under `set -euo pipefail` like the entrypoint does — which is exactly what
broke the earlier in-entrypoint `( while …; done ) &`:

- a crashed watcher is restarted; one that then ran long resets the count;
- the wait doubles with consecutive fast exits;
- after MAX_FAST immediate exits the loop stops and opens one thread.

    python3 tests/test_letterbox_supervise.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "letterbox-supervise.sh"

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def run(tmp: Path, child: str, *, max_fast=3, fast_seconds=1, base_sleep=0, timeout=20):
    (tmp / "child.sh").write_text("#!/usr/bin/env bash\n" + child + "\n")
    (tmp / "alert.sh").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$ALERTS"\n')
    # The parent is the entrypoint's situation: errexit on, the supervisor
    # backgrounded, then waited for.
    (tmp / "parent.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"bash {SCRIPT} 2>>\"$LOG\" &\nwait\necho parent-done\n")
    env = {**os.environ,
           "LETTERBOX_INTAKE_CMD": f"bash {tmp / 'child.sh'}",
           "LETTERBOX_SUPERVISE_ALERT_CMD": f"bash {tmp / 'alert.sh'}",
           "LETTERBOX_SUPERVISE_BASE_SLEEP": str(base_sleep),
           "LETTERBOX_SUPERVISE_FAST_SECONDS": str(fast_seconds),
           "LETTERBOX_SUPERVISE_MAX_FAST": str(max_fast),
           "RUNS": str(tmp / "runs"), "ALERTS": str(tmp / "alerts"), "LOG": str(tmp / "log")}
    try:
        res = subprocess.run(["bash", str(tmp / "parent.sh")], env=env, capture_output=True,
                             text=True, timeout=timeout)
        out = res.stdout
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    runs = (tmp / "runs").read_text().count("\n") if (tmp / "runs").exists() else 0
    alerts = (tmp / "alerts").read_text().splitlines() if (tmp / "alerts").exists() else []
    log = (tmp / "log").read_text() if (tmp / "log").exists() else ""
    return out, runs, alerts, log


def main():
    print("a crashing watcher is restarted under an errexit parent")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Crashes twice at once, then runs "long" (past FAST_SECONDS), then
        # crashes again twice — the long run must have reset the count, so
        # the supervisor is still going after 5 runs; the 6th run stops it.
        out, runs, alerts, log = run(tmp, '''
echo run >> "$RUNS"
n=$(wc -l < "$RUNS")
if (( n == 3 )); then sleep 1.3; exit 0; fi
if (( n >= 6 )); then sleep 30; fi
exit 1''', max_fast=3, fast_seconds=1, timeout=12)
        check("restarted after each crash (not stopped by the parent's set -e)", runs >= 6, True)
        check("a long run reset the fast-exit count: no give-up before run 6", alerts, [])
        check("the parent's wait was still running (supervisor alive)", "parent-done" in out, False)

    print("repeated immediate exits: backoff, then give up and tell the user once")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        out, runs, alerts, log = run(tmp, 'echo run >> "$RUNS"; exit 7', max_fast=3,
                                     fast_seconds=5, base_sleep=1, timeout=30)
        check("ran MAX_FAST times, then stopped", runs, 3)
        check("the supervisor exited, so the parent finished", "parent-done" in out, True)
        check("one dashboard thread", len(alerts), 1)
        check("…saying what and which exit code",
              "Letterbox intake watcher stopped" in alerts[0] and "exit 7" in alerts[0], True)
        waits = [line.split("restarting in ")[1].split("s")[0]
                 for line in log.splitlines() if "restarting in" in line]
        check("the wait doubled between fast exits", waits, ["1", "2"])
        check("the give-up is logged as an error", "[letterbox][error]" in log, True)

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
