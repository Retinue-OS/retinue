#!/usr/bin/env bash
# Keeps the letterbox intake watcher (scripts/letterbox-intake.py --watch)
# running. The watcher is what quarantines reserved names and keeps accepted
# files from the inbox sweep, so it must not stay dead — but a watcher that
# dies at once, every time (a configuration error), must not be restarted
# every ten seconds forever either, with nobody told.
#
# Its own process, deliberately not a subshell inside the entrypoint: that
# runs under `set -e`, which a `( … ) &` inherits, and a failing command in
# the body of a while loop is not exempt from errexit — the first crash of
# the watcher would end the loop, and nothing would restart anything.
#
# Policy: a watcher that ran for at least FAST_SECONDS is restarted after
# BASE_SLEEP; one that exited sooner counts as a fast exit, and the wait
# doubles with each consecutive fast exit (10, 20, 40, …). After MAX_FAST
# consecutive fast exits the loop gives up and opens a dashboard thread.
# Everything is overridable through the environment, which is how
# tests/test_letterbox_supervise.py drives it with a stand-in command.
set +e
set -uo pipefail

CMD="${LETTERBOX_INTAKE_CMD:-python3 /workspace/scripts/letterbox-intake.py --watch}"
ALERT_CMD="${LETTERBOX_SUPERVISE_ALERT_CMD:-python3 /workspace/scripts/conversation-push.py}"
BASE_SLEEP="${LETTERBOX_SUPERVISE_BASE_SLEEP:-10}"
FAST_SECONDS="${LETTERBOX_SUPERVISE_FAST_SECONDS:-60}"
MAX_FAST="${LETTERBOX_SUPERVISE_MAX_FAST:-5}"

fast=0
while true; do
  started=$(date +%s)
  # shellcheck disable=SC2086 — the command is a line, split on purpose
  $CMD
  rc=$?
  ran=$(( $(date +%s) - started ))
  if (( ran < FAST_SECONDS )); then
    fast=$(( fast + 1 ))
  else
    fast=0
  fi
  if (( fast >= MAX_FAST )); then
    echo "[letterbox][error] intake watcher exited ${fast} times in a row within ${FAST_SECONDS}s (last exit ${rc}); not restarting it again" >&2
    # shellcheck disable=SC2086
    $ALERT_CMD --title "Letterbox intake watcher stopped" --importance 3 \
      "The letterbox intake watcher (scripts/letterbox-intake.py) keeps exiting at once (exit ${rc}, ${fast} times in a row), so it was not restarted again. Until it runs, files with reserved names are only quarantined by the hourly inbox sweep and typed intake (accepts) does not happen. Check the container log for the error, then restart the container." \
      || echo "[letterbox][error] could not open a dashboard thread about it" >&2
    exit 1
  fi
  wait=$(( BASE_SLEEP * (1 << (fast > 0 ? fast - 1 : 0)) ))
  echo "[letterbox][warn] intake watcher exited (${rc}) after ${ran}s; restarting in ${wait}s" >&2
  sleep "$wait"
done
