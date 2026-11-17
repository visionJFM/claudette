#!/usr/bin/env bash
# Where does this machine stand — for every repo claudette spans, plus the
# semantic settings/CLAUDE.md diff. Read-only.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE"
[ "$(basename "$HERE")" = "scripts" ] && ROOT="$(dirname "$HERE")"

. "$HERE/pyfind.sh"
PYTHON="$(claudette_python "$HERE")" || exit 4
ROOT_SYNC="$(cd "$HERE" && "$PYTHON" -c "import prefs; print(1 if prefs.enabled('my_root_repo_sync') else 0)" 2>/dev/null || echo 0)"

for repo in "$HOME/.claude" "$ROOT"; do
    [ -d "$repo/.git" ] || continue
    if [ "$repo" = "$HOME/.claude" ] && [ "$repo" != "$ROOT" ] && [ "$ROOT_SYNC" != "1" ]; then
        echo "=== $repo — out of scope (my_root_repo_sync is off) ==="
        echo ""
        continue
    fi
    echo "=== $repo ==="
    git -C "$repo" fetch
    git -C "$repo" status
    echo ""
done

"$PYTHON" "$HERE/features_apply.py" --status
