#!/usr/bin/env python3
"""SessionStart hook (feature claude_md_turn_style): the turn style is off in a
headless run.

The style is imported into every session through CLAUDE.md, `claude -p` and the
Agent SDK included, and a program reading their output gets `[T1] 4` where it
expected `4`; `--output-format json` does not help, its `result` is the same
text. Claude Code sets CLAUDE_CODE_ENTRYPOINT to `sdk-cli` for `-p` (even when
launched from an interactive shell), `sdk-ts` / `sdk-py` for the SDKs, `cli`
interactive, `claude-vscode` in the extension — undocumented values. Only the `sdk-` prefix switches the style
off: an unknown value leaves it on, so a renamed interactive entrypoint keeps
its layout rather than losing it.
"""
import json
import os
import sys

OFF = ("Claudette turn style (the PREVIEW / RESPONSE / NEXT layout): OFF for this session. "
       "It is a headless run (claude -p or the Agent SDK), whose output a program may read: "
       "answer in plain form, with no turn labels, sections or Go handles.")


def main():
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-"):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": OFF}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
