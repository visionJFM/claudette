#!/usr/bin/env python3
# No-op shim for a removed hook feature (CLAUDETTE_DESIGN.md "The gate went back to
# an instruction"). Claude Code reads hooks.json ONCE, at session start, so a session
# that began with the hook on keeps naming this path after an update. A missing script
# makes python exit 2, and exit 2 from a PreToolUse hook BLOCKS the tool, which would
# stop the rest of the update; restarting does not help, since hooks.json on disk is
# only rewritten by the update's apply step. Exiting 0 lets the stale hook allow, so
# the update finishes and the next start is clean.
#
# CLAUDETTE_REGISTRY.md "Removing one" — for a hook feature the shim is step 3.
import sys

sys.exit(0)
