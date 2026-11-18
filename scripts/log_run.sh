#!/usr/bin/env bash
# log_run.sh <section> -- <command...>
#
# Run <command...> with its output mirrored into this run's config_runs log
# under a `== <section> ==` header (CLAUDETTE_LOGS.md). For the steps the
# /claudette skill drives command by command, above all the push.
#
# Reuses $CLAUDETTE_RUN_LOG when a parent already opened one (a chained
# update-then-push stays ONE file) and exports it for children; opens a new log
# otherwise, so wrapping a read-only command creates a run file.
#
# Exit status is the command's, not tee's: a failed push still fails.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# No interpreter is not fatal: logging never stops a command from running
# (below); the empty PYTHON falls through to exec.
. "$HERE/pyfind.sh"
PYTHON="$(claudette_python "$HERE" 2>/dev/null || true)"

SECTION="${1:-run}"
shift || true
[ "${1:-}" = "--" ] && shift

if [ "$#" -eq 0 ]; then
    echo "usage: log_run.sh <section> -- <command...>" >&2
    exit 2
fi

if [ -z "${CLAUDETTE_RUN_LOG:-}" ]; then
    CLAUDETTE_RUN_LOG="$(cd "$HERE" && "$PYTHON" -c "import paths; print(paths.new_run_log())" 2>/dev/null)"
    export CLAUDETTE_RUN_LOG
fi

# No log available (unwritable logs/, no python) — run the command anyway.
# Logging is never allowed to be the reason a command did not happen.
if [ -z "${CLAUDETTE_RUN_LOG:-}" ]; then
    exec "$@"
fi

# The log is written masked (redact.py): a push typed with its credential on the
# command line (`-c http.extraHeader=Authorization: Bearer …`) must not leave the
# credential in logs/ (CLAUDETTE_DESIGN.md "Run logs are masked, and pull failures
# quote git"). A masker that
# does not work would kill the command through its pipe, so it is tried first;
# failing that, the command runs and the log records only that its output is
# missing, never the raw text.
if [ "$("$PYTHON" "$HERE/redact.py" --selftest 2>/dev/null)" != "ok" ]; then
    echo "== $SECTION == $(date +%Y-%m-%dT%H:%M:%S)  (output not logged: redact.py did not run)" \
        >> "$CLAUDETTE_RUN_LOG" 2>/dev/null
    exec "$@"
fi

{ "$PYTHON" "$HERE/redact.py" --header "$SECTION" "$@"; "$@"; } 2>&1 \
    | "$PYTHON" "$HERE/redact.py" --tee "$CLAUDETTE_RUN_LOG"
status=${PIPESTATUS[0]}
echo "Run log: $CLAUDETTE_RUN_LOG"
exit "$status"
