#!/usr/bin/env bash
# push-diff.sh [repo...] — what a push from this machine would actually contain.
# Pre-approved in settings.json (via run.sh) so it runs without a prompt.
#
# WHICH REPOS. No args: the repos claudette itself spans (the personal root repo
# and the claudette plugin), for a person at a terminal. Every caller passes its
# repo (`<repo_root>`; the `/claudette push` flow passes the personal root repo,
# CLAUDETTE_PUSH.md step 3), and a non-repo argument is a loud exit 2, not a silent
# skip. Called bare while another project is the one being pushed, it would report
# "nothing flagged" about claudette's own repos and not a line of that project.
#
# WHAT THIS DOES NOT PRINT. File names, commit subjects, flag lines and a verdict —
# never file content. What is left to the caller is the SEMANTIC half: personal data,
# someone else's hostname, a customer name — anything no fixed pattern can match. That
# half is a read of the lines the push ADDS, and a file list or a `--stat` is not one:
# a name inside an added line shows in neither (CLAUDETTE_DESIGN.md, "The push gate
# moved out of the skill").
#
# ON A HIT. A `SECRET?` line means ROTATE the credential: git history keeps it even
# if the commit is reverted, so removing the file is not a fix. Show the finding and
# let the user decide; never rewrite history to hide it.
#
# A `HOST?` / `USER?` line is this machine's own hostname / OS username inside an
# outgoing blob. Nothing to rotate — the fix is to redact the string — but the same
# history rule holds: once pushed it stays, so a pushed one is a decision to bring to
# the user, not an edit to make quietly.
#
# PER-PROJECT CLASSES. Which classes run is the `leak_scan_classes` parameter
# (secret, host, user), resolved per repo by overrides.py: registry default, then
# this machine's prefs, then the repo's committed `.claudette.json`, then its
# out-of-git `.claudette.local.json`. A repo where a class is expected content —
# hostnames in notes about your own machines — removes it there:
#     {"params": {"leak_scan_classes": {"remove": ["host"]}},
#      "notes":  {"leak_scan_classes": "why"}}
# Removal only, never a replacement list: a class added later runs everywhere. The
# shared file may not remove `secret` (one commit must not disarm every clone).
# Anything the resolver refuses is printed as a CONFIG line; if the resolver itself
# cannot run, every class runs. `LARGE` is not a class: it marks a blob that was NOT
# scanned, so silencing it would hide a coverage hole rather than a false positive.
#
# WHAT RAN IS PRINTED. Every repo ends with a `COVERAGE` line naming the classes that
# actually ran, so a `clean` verdict can never be read as more coverage than it had —
# whether a class was skipped by a project file or was never available (no usable
# machine name on this host makes `host` inert with or without an override).
#
# WHICH STATE. Two, because a preview and a gate stand at different moments:
#   - staging dry-run  — uncommitted work; the push flow previews here and commits
#                        at step 5, so this is what it is about to create
#   - outgoing commits — every blob of every commit in @{upstream}..HEAD; anything
#                        gating at the push itself runs after the commit exists,
#                        and the working tree no longer holds what is leaving
# A repo with no upstream cannot have its outgoing side determined: that is
# reported as `unknown`, which is NOT an all-clear.
#
# Warns, never blocks — a false positive that halts a push trains you to bypass
# the guard. The per-repo RESULT line carries the verdict (clean / flagged /
# unknown); the exit status is 0 for all three and non-zero only when the
# invocation itself was wrong.
set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PAT_FILE="$SELF_DIR/leak_patterns.re"
# The pattern file stays separate from this script because tests read it too, in
# Python.
SECRET_RE=$(awk '!/^#/ && NF {printf "%s%s", sep, $0; sep="|"}' "$PAT_FILE" 2>/dev/null)
if [ -z "$SECRET_RE" ]; then
    echo "push-diff.sh: cannot read patterns from $PAT_FILE" >&2
    exit 2
fi

# --- this machine's own names ----------------------------------------------
# The leak class no shipped pattern file can hold: the hostname and the OS username
# of the machine doing the pushing. They cannot go in leak_patterns.re — that file
# travels inside this repo, so writing the name there would publish the exact string
# it exists to keep in. Built here instead, at scan time, from the running machine,
# and stored nowhere. The case it covers: a sample document pasted into a comment or a
# test fixture carries the machine's name, and no credential pattern can match a name.
#
# Case-folding and word boundaries are written INTO the expression instead of coming
# from flags, because one grep serves both classes and the flags are wrong for the
# other one: `-w` truncates a credential match, `-i` loosens it, and `\b` is not
# portable to BSD grep. So a machine called `ORB` becomes
# `(^|[^A-Za-z0-9_])[Oo][Rr][Bb]([^A-Za-z0-9_]|$)` — which is also what keeps a short
# name that happens to sit inside an ordinary word (`ORB` in `ABSORB`) from flagging
# every file that word appears in.
#
# Dropped: names under 3 characters, and names too generic to carry information. A
# guard that fires on every file is a guard you learn to skip — the same reasoning
# leak_patterns.re gives for rejecting *secret*-style patterns.
IDENT_GENERIC='admin|administrator|build|builder|desktop|guest|home|host|localhost|mac|macbook|owner|pc|root|runner|ubuntu|user|users|win|windows'

ident_alt() {   # $1 a name -> a case-insensitive ERE branch, or nothing if rejected
    local n="$1" lo up out="" i=0 a b
    case "$n" in ""|*[!A-Za-z0-9_-]*) return 0 ;; esac
    [ "${#n}" -ge 3 ] || return 0
    lo=$(printf '%s' "$n" | LC_ALL=C tr 'A-Z' 'a-z')
    case "|$IDENT_GENERIC|" in *"|$lo|"*) return 0 ;; esac
    up=$(printf '%s' "$n" | LC_ALL=C tr 'a-z' 'A-Z')
    while [ "$i" -lt "${#n}" ]; do
        a="${lo:$i:1}"; b="${up:$i:1}"
        if [ "$a" = "$b" ]; then out="$out$a"; else out="$out[$b$a]"; fi
        i=$((i + 1))
    done
    printf '%s' "$out"
}

names_re() {  # names... -> one word-bounded ERE over the usable ones, or nothing
    local _n _a alts=""
    for _n in "$@"; do
        _a="$(ident_alt "$_n")"
        [ -n "$_a" ] || continue
        case "|$alts|" in *"|$_a|"*) continue ;; esac
        alts="${alts:+$alts|}$_a"
    done
    [ -n "$alts" ] && printf '%s' '(^|[^A-Za-z0-9_])('"$alts"')([^A-Za-z0-9_]|$)'
    return 0
}
# Two classes, not one: a repo about your own machines expects the hostname but
# still must not carry the OS username (it sits in every home-directory path).
HOST_RE="$(names_re "$(hostname 2>/dev/null | sed 's/\..*//')")"
USER_RE="$(names_re "${USERNAME:-}" "${USER:-}" "${LOGNAME:-}")"

# --- which classes run in this repo -----------------------------------------
# Resolved per repo by overrides.py (header: PER-PROJECT CLASSES). The rules —
# removal only, a note for every override, `secret` not removable from the shared
# file, a tracked `.claudette.local.json` not applied — live there, once, for every
# script feature; this side only turns the answer into expressions.
KNOWN_CLASSES="secret host user"
PY=""
if . "$SELF_DIR/pyfind.sh" 2>/dev/null; then
    PY="$(claudette_python "$SELF_DIR" 2>/dev/null)" || PY=""
fi

ACTIVE_SECRET_RE=""     # recomputed per repo, never carried from the previous one
ACTIVE_HOST_RE=""
ACTIVE_USER_RE=""
SCAN_RE=""
SKIP_NOTES=""
COVERAGE=""

repo_classes() {  # $1 repo -> ACTIVE_*_RE, SCAN_RE, SKIP_NOTES, COVERAGE
    local repo="$1" out="" line classes="" c ran="" skipped="" notes="" member layer note
    # Reset first: this runs once per repo in a multi-repo invocation, and an override
    # leaking from the repo it was set in to the next one would silently reduce coverage.
    ACTIVE_SECRET_RE="$SECRET_RE"; ACTIVE_HOST_RE="$HOST_RE"; ACTIVE_USER_RE="$USER_RE"
    SKIP_NOTES=""
    if [ -n "$PY" ]; then
        out="$("$PY" "$SELF_DIR/overrides.py" resolve leak_scan_classes --repo "$repo" 2>/dev/null)" || out=""
    fi
    line="$(printf '%s\n' "$out" | sed -n 's/^VALUE //p' | head -n 1)"
    if [ -z "$line" ]; then
        # Fail closed: no answer means every class, and a line saying why.
        classes="$KNOWN_CLASSES"
        notes="  CONFIG  project overrides not read (resolver unavailable) — every class runs"
    else
        classes="$(printf '%s' "$line" | LC_ALL=C tr -d '[]" ' | tr ',' ' ')"
        case " $classes " in *" secret "*) ;; *) ACTIVE_SECRET_RE="" ;; esac
        case " $classes " in *" host "*)   ;; *) ACTIVE_HOST_RE="" ;; esac
        case " $classes " in *" user "*)   ;; *) ACTIVE_USER_RE="" ;; esac
        while IFS=$'\t' read -r member layer note; do
            [ -n "$member" ] || continue
            case " $classes " in *" $member "*) continue ;; esac
            skipped="${skipped:+$skipped, }$member ($layer: \"$note\")"
        done < <(printf '%s\n' "$out" | sed -n 's/^SKIP //p')
        while IFS= read -r line; do
            [ -n "$line" ] && notes="${notes:+$notes$'\n'}  CONFIG  $line"
        done < <(printf '%s\n' "$out" | sed -n 's/^MSG //p')
    fi
    SKIP_NOTES="$notes"

    SCAN_RE=""
    for c in "$ACTIVE_SECRET_RE" "$ACTIVE_HOST_RE" "$ACTIVE_USER_RE"; do
        [ -n "$c" ] && SCAN_RE="${SCAN_RE:+$SCAN_RE|}$c"
    done

    [ -n "$ACTIVE_SECRET_RE" ] && ran="secret"
    [ -n "$ACTIVE_HOST_RE" ] && ran="${ran:+$ran,}host"
    [ -n "$ACTIVE_USER_RE" ] && ran="${ran:+$ran,}user"
    ran="${ran:+$ran,}large"
    COVERAGE="ran $ran"
    [ -n "$skipped" ] && COVERAGE="$COVERAGE; skipped $skipped"
    # Inert is not the same as skipped: saying "ran host" on a machine whose name was
    # rejected as too short or too generic is the overstatement COVERAGE exists to
    # prevent.
    [ -z "$HOST_RE" ] && case " $classes " in *" host "*)
        COVERAGE="$COVERAGE; host unavailable (no usable machine name on this host)" ;; esac
    [ -z "$USER_RE" ] && case " $classes " in *" user "*)
        COVERAGE="$COVERAGE; user unavailable (no usable OS username on this host)" ;; esac
    return 0
}

scan_labels() {  # content on stdin -> SECRET? / HOST? / USER?, one per line, or nothing
    # ONE pass over the blob, then the matches it produced are classified — so more
    # leak classes do not multiply what a push costs. `-I` drops binaries; the
    # matched text lands in a variable and is never printed (the file-content rule).
    local m
    # Every class removed: consume the blob and say nothing. Never reach grep with an
    # empty expression — that matches every line of everything and reports the whole push.
    if [ -z "$SCAN_RE" ]; then cat >/dev/null 2>&1; return 0; fi
    m=$(LC_ALL=C grep -oIE -- "$SCAN_RE" 2>/dev/null | LC_ALL=C sort -u)
    [ -n "$m" ] || return 0
    if [ -n "$ACTIVE_SECRET_RE" ]; then
        printf '%s\n' "$m" | LC_ALL=C grep -qE -- "$ACTIVE_SECRET_RE" && echo "SECRET?"
    fi
    if [ -n "$ACTIVE_HOST_RE" ]; then
        printf '%s\n' "$m" | LC_ALL=C grep -qE -- "$ACTIVE_HOST_RE" && echo "HOST?"
    fi
    if [ -n "$ACTIVE_USER_RE" ]; then
        printf '%s\n' "$m" | LC_ALL=C grep -qE -- "$ACTIVE_USER_RE" && echo "USER?"
    fi
    return 0
}

LARGE_BYTES=1048576
BLOB_CAP=2000
WT_CAP=2000
OUTGOING_ONLY=0

toplevel() { git -C "$1" rev-parse --show-toplevel 2>/dev/null; }

# --outgoing-only: skip the working tree entirely. For a caller standing at the
# moment of `git push` the worktree is not what is leaving, and enumerating an
# unignored node_modules to grep it would be pure cost.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --outgoing-only) OUTGOING_ONLY=1; shift ;;
        --) shift; break ;;
        -*) echo "push-diff.sh: unknown option: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done

# --- repo list -------------------------------------------------------------
# N_REPOS is kept alongside the array deliberately: ${#arr[@]} on an empty array
# under `set -u` is the same bash-3.2 grey area as "${arr[@]}", and this branch IS
# reachable — a consumer install where neither ~/.claude nor claudette itself is a
# git repo. A scalar counter has no version-sensitive behaviour anywhere.
REPOS=()
N_REPOS=0
if [ "$#" -gt 0 ]; then
    for arg in "$@"; do
        t="$(toplevel "$arg")"
        if [ -z "$t" ]; then
            # Loud, unlike the default list: an explicit argument means the caller
            # believes this is the repo being pushed. Skipping it silently is the
            # exact false all-clear this argument exists to prevent.
            echo "push-diff.sh: not inside a git work tree: $arg" >&2
            exit 2
        fi
        REPOS+=("$t")
        N_REPOS=$((N_REPOS + 1))
    done
else
    HERE="$(cd "$(dirname "$0")" && pwd)"
    ROOT="$HERE"
    [ "$(basename "$HERE")" = "scripts" ] && ROOT="$(dirname "$HERE")"
    for d in "$HOME/.claude" "$ROOT"; do
        # Test .git before resolving: without it a non-repo ROOT nested under a
        # repo ~/.claude would resolve to ~/.claude and be scanned twice.
        [ -e "$d/.git" ] || continue
        t="$(toplevel "$d")"
        [ -n "$t" ] || continue
        REPOS+=("$t")
        N_REPOS=$((N_REPOS + 1))
    done
fi

if [ "$N_REPOS" -eq 0 ]; then
    echo "push-diff.sh: no git repository to scan"
    exit 0
fi

# --- guards ----------------------------------------------------------------
# Tripwire for the three things a path-based allowlist cannot see: an oversized blob
# that should have gone to local/, secret-shaped CONTENT in a normally-named file,
# and this machine's own names in any of it. A SECRET? hit means rotate the
# credential — git history keeps it even if the commit is reverted.

guard_worktree() {  # $1 repo — returns 1 if the file cap was hit
    local repo="$1" f p sz lab n=0
    # Process substitution, not a pipe: the loop body must run in this shell for
    # $n to survive it. A repo that does not ignore its build output (node_modules,
    # dist) would otherwise have every file in it opened and grepped.
    while IFS= read -r f; do
        p="$repo/$f"
        [ -f "$p" ] || continue
        n=$((n + 1))
        if [ "$n" -gt "$WT_CAP" ]; then
            echo "  CAP     stopped after $WT_CAP worktree files — check .gitignore"
            return 1
        fi
        sz=$(wc -c < "$p" 2>/dev/null || echo 0)
        if [ "$sz" -gt "$LARGE_BYTES" ]; then
            echo "  LARGE   $((sz / 1024)) KB  worktree:$f"
            continue
        fi
        while IFS= read -r lab; do
            printf '  %-7s worktree:%s\n' "$lab" "$f"
        done < <(scan_labels < "$p")
    done < <(git -C "$repo" add --dry-run -A . 2>/dev/null | sed -n "s/^add '\(.*\)'\$/\1/p")
    return 0
}

guard_outgoing() {  # $1 repo, $2 range — returns 1 if the blob cap was hit
    local repo="$1" range="$2" n=0 c line blob path sz lab
    # Every commit in the range, not just the diff of its endpoints: a secret added
    # in one commit and removed in the next is still in the history being pushed.
    # Process substitution (not a pipe) so $n survives the loop body.
    while IFS= read -r c; do
        while IFS= read -r line; do
            blob=$(printf '%s' "$line" | awk '{print $4}')
            case "${blob:-x}" in *[!0-9a-f]*) continue ;; esac
            path=${line#*$'\t'}
            n=$((n + 1))
            if [ "$n" -gt "$BLOB_CAP" ]; then
                echo "  CAP     stopped after $BLOB_CAP blobs — range too large to scan here"
                return 1
            fi
            sz=$(git -C "$repo" cat-file -s "$blob" 2>/dev/null || echo 0)
            if [ "$sz" -gt "$LARGE_BYTES" ]; then
                echo "  LARGE   $((sz / 1024)) KB  ${c:0:7}:$path"
                continue
            fi
            while IFS= read -r lab; do
                printf '  %-7s %s:%s\n' "$lab" "${c:0:7}" "$path"
            done < <(git -C "$repo" cat-file -p "$blob" 2>/dev/null | scan_labels)
        done < <(git -C "$repo" diff-tree -r --root --no-commit-id --no-renames \
                     --diff-filter=d "$c" 2>/dev/null)
    done < <(git -C "$repo" rev-list "$range" 2>/dev/null)
    return 0
}

# --- report ----------------------------------------------------------------
# ${REPOS[@]+"${REPOS[@]}"} rather than "${REPOS[@]}": in bash before 4.4 — macOS
# /bin/bash is 3.2 — expanding an EMPTY array under `set -u` aborts with "unbound
# variable". The empty check above already exits first, so this only matters if
# that check is ever moved or removed; one token buys immunity to that edit.
for repo in ${REPOS[@]+"${REPOS[@]}"}; do
    echo "=== $repo ==="
    verdict=clean
    repo_classes "$repo"

    if [ "$OUTGOING_ONLY" -eq 0 ]; then
        echo "--- Staging dry-run (uncommitted) ---"
        git -C "$repo" add --dry-run -A . 2>&1
    fi

    upstream=$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)
    if [ -n "$upstream" ]; then
        range="$upstream..HEAD"
        ahead=$(git -C "$repo" rev-list --count "$range" 2>/dev/null || echo 0)
        echo "--- Outgoing commits ($range): $ahead ---"
        [ "$ahead" -gt 0 ] && git -C "$repo" log --oneline --no-decorate "$range" 2>/dev/null | head -40
    else
        range=""
        echo "--- Outgoing commits: NO UPSTREAM — cannot determine what a push would send ---"
        verdict=unknown
    fi

    echo "--- Guard: additions the ignore rules did not catch ---"
    # Printed here rather than folded into $GUARD: these are statements about the scan,
    # not findings, so they must not suppress the "nothing flagged" line nor be read as
    # a hit by the verdict match below.
    [ -n "$SKIP_NOTES" ] && printf '%s\n' "$SKIP_NOTES"
    GUARD=""
    if [ "$OUTGOING_ONLY" -eq 0 ]; then
        GUARD=$(guard_worktree "$repo") || verdict=unknown
    fi
    if [ -n "$range" ]; then
        OUT=$(guard_outgoing "$repo" "$range") || verdict=unknown
        [ -n "$OUT" ] && GUARD="${GUARD:+$GUARD$'\n'}$OUT"
    fi
    if [ -n "$GUARD" ]; then
        echo "$GUARD"
        case "$GUARD" in *"SECRET?"* | *"HOST?"* | *"USER?"* | *"LARGE"*) verdict=flagged ;; esac
        # Said at the moment the false positive happens, because that is the only moment
        # anyone goes looking for the switch. Never for SECRET: a machine name can be
        # expected content in a repo about your own machines, a credential never is.
        for c in HOST USER; do
            case "$GUARD" in *"$c?"*)
                lc=$(printf '%s' "$c" | LC_ALL=C tr 'A-Z' 'a-z')
                echo "  HINT    $c? expected in this repo? remove that class there, in $repo/.claudette.json:"
                echo "          {\"params\": {\"leak_scan_classes\": {\"remove\": [\"$lc\"]}},"
                echo "           \"notes\":  {\"leak_scan_classes\": \"<why it is expected here>\"}}" ;;
            esac
        done
    else
        echo "  nothing flagged"
    fi

    # Before RESULT, so RESULT stays the last line of the block and keeps its exact
    # `RESULT <repo>: <verdict>` shape — callers split that line on its last colon.
    echo "COVERAGE $repo: $COVERAGE"
    echo "RESULT $repo: $verdict"
    echo ""
done
