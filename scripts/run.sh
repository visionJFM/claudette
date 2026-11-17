#!/usr/bin/env bash
# claudette dispatcher — the single entry point for every claudette script that
# Claude or a user invokes directly. One literal prefix (bash ".../run.sh") is
# all a permission rule needs to cover, and no documented command needs
# $(command -v ...), pipes, or redirects — shapes the permission layer fails
# closed on. Hooks and the statusline are not permission-gated and never pass
# through here: this file stays off their hot path.
#
# SECURITY: the claude_allow_claudette_scripts rule accepts ANY argument
# suffix, so the discipline below IS the boundary. The target is a bare
# filename resolved strictly inside this directory; args pass as argv —
# never eval'd, never sourced, never relayed to a shell.
# Why: CLAUDETTE_DESIGN.md "Permission / allow-rule principles".
#
# Interpreter: pyfind.sh — the machine's stored Python, else a validated PATH
# lookup; a failure is a message and exit 4, never a silent no-op. Hooks and
# the status line do not come through here: they carry the absolute path.
# CLAUDETTE_VIA lets features_apply.py tell "dispatched" from "launched by
# hand" (its doctor row), which is the difference between an interpreter the
# hooks share and one that proves nothing about them.
set -euo pipefail
SC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDETTE_VIA=run.sh

if [ "$#" -lt 1 ]; then
  echo "usage: run.sh <script>.py|<script>.sh [args...]   (bare filename in scripts/)" >&2
  exit 2
fi
name="$1"; shift

if [[ ! "$name" =~ ^[a-z0-9_-]+\.(py|sh)$ ]]; then
  echo "run.sh: target must be a bare [a-z0-9_-] filename ending in .py or .sh: $name" >&2
  exit 2
fi
target="$SC/$name"
if [ ! -f "$target" ]; then
  echo "run.sh: no such claudette script: $name" >&2
  exit 3
fi

case "$name" in
  *.sh) exec bash "$target" "$@" ;;
  *.py)
    . "$SC/pyfind.sh"
    PY="$(claudette_python "$SC")" || exit 4
    exec "$PY" "$target" "$@" ;;
esac
