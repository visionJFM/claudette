#!/usr/bin/env python3
""""..recap" — a concise model summary of the conversation, ending in the snapshot.

The one command that is IN CONTEXT ONLY (free=False): it asks the model where the
conversation is at, which no hook can answer from disk, so a free ".recap" cannot
exist: the dispatcher answers that form with a zero-token pointer to "..recap"
rather than letting it reach the model.

Unlike every other command this is an instruction to the model, not an echo — so it
builds its own additionalContext rather than using dot_common.emit_context.
"""
import json

import dot_common as dc
import dot_status


def handle(data, tail, to_context):
    # Only ever called with to_context=True (the dispatcher gates the "." form),
    # but a malformed argument is still this command's to answer, on this channel.
    if tail:
        # "?" asks for this usage on purpose (dc.HELP): no escape hint for it.
        help_ = dc.is_help(tail)
        instr = ("Claudette .recap request %s. Reply with EXACTLY these lines and "
                 "nothing else:\n**..recap** — usage: `..recap`. No arguments.%s"
                 % ("for its usage" if help_ else "with an invalid argument",
                    "" if help_ else "\n" + dc.ESCAPE_HINT))
    else:
        lines = dot_status.snapshot_lines(data)
        instr = ("Claudette recap request. Reply with a very concise bullet summary "
                 "of where this conversation is at (3-6 short bullets: done / in "
                 "progress / pending or blocked; no prose paragraphs)"
                 + (", then end your reply with EXACTLY %s:\n%s"
                    % ("these lines" if len(lines) > 1 else "this line",
                       "\n".join(lines)) if lines else ".")
                 )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": instr}}))
