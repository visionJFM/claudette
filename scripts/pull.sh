#!/usr/bin/env bash
# Pull every repo claudette spans, then apply the features to this machine.
# Claudette lives in two repos (the personal root repo and the claudette
# plugin); a folder with no .git is simply skipped.
#
# The personal root repo is pulled ONLY with the `my_root_repo_sync` feature on:
# a user's ~/.claude may be their own repo that has nothing to do with
# claudette, and mutating it without consent is exactly what the feature
# registry exists to prevent.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE"
[ "$(basename "$HERE")" = "scripts" ] && ROOT="$(dirname "$HERE")"

# Exit 5 on no interpreter (4 is taken: a refused credential, see the end).
. "$HERE/pyfind.sh"
PYTHON="$(claudette_python "$HERE")" || exit 5
ROOT_SYNC="$(cd "$HERE" && "$PYTHON" -c "import prefs; print(1 if prefs.enabled('my_root_repo_sync') else 0)" 2>/dev/null || echo 0)"

# One run = one log (CLAUDETTE_LOGS.md). Opened HERE, before any git output,
# and handed to features_apply.py through the environment so the pull and the
# apply it caused land in the same file. The git section is the part a caller
# that keeps only the tail loses first.
RUN_LOG="$(cd "$HERE" && "$PYTHON" -c "import paths; print(paths.new_run_log())" 2>/dev/null)"
[ -n "$RUN_LOG" ] && export CLAUDETTE_RUN_LOG="$RUN_LOG"

# Never let a pull sit on a credential prompt (git_upstreams.py runs the same
# way): an expired token would raise a Username prompt, or a Windows Credential
# Manager dialog, and hang the skill's tool call. Fail fast and loud instead.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes}"

# stderr of a REFUSED remote, not an unreachable one — same list as
# git_upstreams.py AUTH_RE (a private GitHub repo with a dead fine-grained PAT
# answers "Repository not found", never "401").
AUTH_GREP='authentication failed|invalid username or password|could not read (username|password)|terminal prompts disabled|bad credentials|permission denied|access denied|repository not found|(^|[^0-9])(401|403)([^0-9]|$)'

# pull_repos runs inside a `| tee` pipeline, so it cannot export anything to
# this shell — it records the verdict in a file the parent reads afterwards.
FAIL_MARK="${TMPDIR:-/tmp}/claudette-pull-fail.$$"
: > "$FAIL_MARK"

pull_repos() {
    for repo in "$HOME/.claude" "$ROOT"; do
        [ -d "$repo/.git" ] || continue
        if [ "$repo" = "$HOME/.claude" ] && [ "$repo" != "$ROOT" ] && [ "$ROOT_SYNC" != "1" ]; then
            echo "=== skipping $repo (my_root_repo_sync is off — claudette does not touch it) ==="
            continue
        fi
        # CLAUDE.md is tracked but its claudette block is DERIVED: every machine
        # rewrites it from the registry on every apply, so a registry change
        # dirties this file and git refuses to merge over it ("local changes
        # would be overwritten"). Restoring the block from HEAD before the pull
        # costs nothing: the apply that follows regenerates it. Only when the
        # dirt is confined to the block: `outside` is authored content, and that
        # pull must still fail loudly.
        if [ "$repo" = "$HOME/.claude" ]; then
            scope="$("$PYTHON" "$HERE/features_apply.py" --md-dirt 2>/dev/null)"
            if [ "$scope" = "managed" ]; then
                echo "=== $repo/CLAUDE.md: dirt confined to the claudette block — restoring from HEAD (the apply below regenerates it) ==="
                git -C "$repo" checkout -- CLAUDE.md
            fi
        fi
        # On an install, the claudette folder is a copy of the published repo, not
        # a place where work is merged: only a fast-forward updates it. Anything
        # else (the repo moved, its history was rewritten, an edited file in the
        # way) fails here, and the repair is a reinstall, which keeps the folder's
        # data (README, Install). Without --ff-only, a user's own pull.rebase=false
        # can leave conflict markers in the scripts. The user's root repo and a
        # development clone (CLAUDETTE_DEV.md) keep a plain pull. The notes are
        # looked for in docs/ and at the root, where an older clone holds them.
        ff=""
        if [ "$repo" = "$ROOT" ] && [ "$repo" != "$HOME/.claude" ] \
           && [ ! -f "$ROOT/docs/CLAUDETTE_DEV.md" ] && [ ! -f "$ROOT/CLAUDETTE_DEV.md" ]; then
            ff="--ff-only"
        fi
        if [ -n "$ff" ]; then
            # An install fetches, screens what arrived with the INSTALLED screen, and
            # fast-forwards to that exact commit only when nothing is flagged. Exit 5 is
            # a hold: nothing changed on disk, and the update plan names the findings.
            echo "=== git fetch, screen, $ff $repo ==="
            out="$("$PYTHON" "$HERE/update_screen.py" pull "$repo" 2>&1)"
            rc=$?
        else
            echo "=== git pull $repo ==="
            # color off explicitly: the log must stay greppable, and git would drop
            # color anyway once its stdout is the tee pipe — being explicit keeps
            # the logged and unlogged runs identical.
            out="$(git -c color.ui=false -C "$repo" pull 2>&1)"
            rc=$?
        fi
        printf '%s\n' "$out"
        if [ "$rc" -eq 5 ]; then
            echo "=== claudette: update held for an audit, nothing changed on disk (see update_held in the plan) ==="
            continue
        fi
        if [ "$rc" -eq 0 ]; then
            # This pull changed what the staleness cache memorizes: evict, so
            # ⚠auth/↓N clear on the next render.
            "$PYTHON" "$HERE/git_upstreams.py" "$repo" --clear >/dev/null 2>&1
            continue
        fi
        if [ -n "$ff" ]; then
            # The published repo asks for no credential: a refusal means it moved or
            # is no longer public, which a reinstall from its current address repairs.
            if printf '%s' "$out" | grep -qiE "$AUTH_GREP"; then
                echo "auth:$repo" >> "$FAIL_MARK"
            else
                echo "other:$repo" >> "$FAIL_MARK"
            fi
            echo "!!! claudette: could not update claudette itself (rc=$rc) — see the output above."
            echo "!!! This machine keeps running the version it has. To repair it, reinstall:"
            echo "!!! paste the prompt of the Install section of $ROOT/README.md into Claude Code."
            echo "!!! The reinstall keeps your answers and logs, and backs up what it replaces."
            edited="$(git -C "$repo" status --porcelain --untracked-files=no 2>/dev/null)"
            if [ -n "$edited" ]; then
                echo "!!! Edited files the reinstall replaces (the backup keeps them, as a patch too):"
                printf '%s\n' "$edited" | sed 's/^/!!!   /'
            fi
        elif printf '%s' "$out" | grep -qiE "$AUTH_GREP"; then
            echo "!!! claudette: the remote REFUSED your credential for $repo (rc=$rc)."
            echo "!!! Nothing was pulled — this machine keeps running its LOCAL checkout,"
            echo "!!! and can no longer tell how far behind it is. Most likely cause: the"
            echo "!!! access token has expired. Re-authenticate, then re-run /claudette update:"
            echo "!!!   git -C \"$repo\" ls-remote origin      # prompts for the new credential"
            echo "auth:$repo" >> "$FAIL_MARK"
        else
            echo "!!! claudette: git pull failed for $repo (rc=$rc) — see the output above."
            echo "!!! Continuing with the local checkout; this machine may be stale."
            echo "other:$repo" >> "$FAIL_MARK"
        fi
    done
}

# Snapshots for the update plan, taken BEFORE the pull: the apply below may
# create prefs.local.json on a virgin machine (align_unanswered answers
# already-aligned features), so a post-hoc first-contact test would lie.
PREFS_BEFORE=0
[ -f "$ROOT/prefs.local.json" ] && PREFS_BEFORE=1
REG_SHA_BEFORE="$(git -C "$ROOT" hash-object "$ROOT/features_def.json" 2>/dev/null || true)"
VERSION_BEFORE="$(cd "$HERE" && "$PYTHON" -c "import paths; print(paths.version() or '')" 2>/dev/null || true)"

if [ -n "${CLAUDETTE_RUN_LOG:-}" ]; then
    { echo "== pull =="; pull_repos; } 2>&1 | tee -a "$CLAUDETTE_RUN_LOG"
else
    pull_repos
fi

# One verdict for the whole pull, auth outranking any other failure, handed to
# --update-plan below so the plan (the block that survives truncation) carries
# it. The apply still runs: the local checkout is complete and installing from
# it keeps the machine coherent — what must not happen is doing that SILENTLY.
PULL_FAILED=""
if grep -q '^auth:' "$FAIL_MARK" 2>/dev/null; then
    PULL_FAILED="auth:$(sed -n 's/^auth://p' "$FAIL_MARK" | tr '\n' ' ')"
elif [ -s "$FAIL_MARK" ]; then
    PULL_FAILED="other:$(sed -n 's/^other://p' "$FAIL_MARK" | tr '\n' ' ')"
fi
rm -f "$FAIL_MARK"
[ -n "$PULL_FAILED" ] && export CLAUDETTE_PULL_FAILED="$PULL_FAILED"

"$PYTHON" "$HERE/features_apply.py" -y "$@"
rc=$?

# The update plan, appended LAST so one invocation hands the /claudette skill
# pull + apply + "does anything need asking" — and so the plan (which carries
# the run-log pointer) is what survives a caller that keeps only the tail.
# Read-only: it opens no run log of its own; tee appends it to this run's.
export CLAUDETTE_PRE_PULL_PREFS="$PREFS_BEFORE"
export CLAUDETTE_VERSION_BEFORE="$VERSION_BEFORE"
REG_SHA_AFTER="$(git -C "$ROOT" hash-object "$ROOT/features_def.json" 2>/dev/null || true)"
if [ -n "$REG_SHA_BEFORE" ] && [ -n "$REG_SHA_AFTER" ]; then
    if [ "$REG_SHA_BEFORE" = "$REG_SHA_AFTER" ]; then
        export CLAUDETTE_REGISTRY_CHANGED=0
    else
        export CLAUDETTE_REGISTRY_CHANGED=1
    fi
fi
if [ -n "${CLAUDETTE_RUN_LOG:-}" ]; then
    "$PYTHON" "$HERE/features_apply.py" --update-plan 2>&1 | tee -a "$CLAUDETTE_RUN_LOG"
else
    "$PYTHON" "$HERE/features_apply.py" --update-plan
fi

# A refused credential exits 4 even when the apply succeeded: the caller must
# not read exit 0 as "synced". 4, not 1, so it is distinguishable from an apply
# failure — and the plan's `pull_failed` says which repos.
case "$PULL_FAILED" in
    auth:*) [ "$rc" -eq 0 ] && rc=4 ;;
esac

exit $rc
