#!/usr/bin/env bash
# Sourced by claudette's shell entry points (run.sh, pull.sh, status.sh,
# log_run.sh, the test runner) to pick the interpreter they run Python scripts
# with: the one stored for this machine in prefs.local.json (the `python`
# parameter, written by features_apply.py after validating it), else a PATH
# lookup — python3 first (CLAUDETTE_DESIGN.md "Why python3 FIRST").
#
# Every answer is verified to RUN and be >= 3.9 before it is returned. That is
# what turns a Microsoft Store alias (found by `command -v`, exits 9009 when
# run) or a python2 into a message instead of a silent failure — and the
# message tells the agent NOT to improvise another interpreter: a script
# launched by hand with a full path proves nothing about the hooks.
#
# Not on the hook path. Hooks and the status line receive the absolute path
# rendered into their command (CLAUDETTE_DESIGN.md "Absolute interpreter
# path"); this file exists for the bootstrap, before prefs exist.
#
# Usage:  . "$SC/pyfind.sh"; PY="$(claudette_python "$SC")" || exit 4
claudette_python() {
    local sc="$1" py="" stored=""
    stored="$(sed -n 's/^[[:space:]]*"python":[[:space:]]*"\([^"]*\)".*/\1/p' \
              "$sc/../prefs.local.json" 2>/dev/null | head -n 1)"
    for py in "$stored" "$(command -v python3 || true)" "$(command -v python || true)"; do
        [ -n "$py" ] || continue
        if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$py"
            return 0
        fi
    done
    {
        echo "claudette: no usable Python (>= 3.9): stored path, python3 and python all failed to run."
        echo "  Windows: install Python from python.org with \"Add python.exe to PATH\" ticked, or"
        echo "           re-run its installer > Modify; if PATH finds a Store stub, disable the python"
        echo "           App Execution Aliases (Settings > Apps > Advanced app settings)."
        echo "  macOS:   xcode-select --install"
        echo "  Then re-run the same command. Do NOT substitute another interpreter by hand:"
        echo "  claudette records the one it validated, and the hooks run only that one."
    } >&2
    return 4
}
