#!/usr/bin/env python3
# Claudette UserPromptSubmit hook — the dot-command dispatcher.
#
#   .name [args]   free channel: 0 tokens, blocked prompt, terminal only
#   ..name [args]  in-context channel: costs a turn, reaches the phone app
#   .              free index        ..   in-context index
#
# Commands are declared in dot_registry.py (one row + one dot_<module>.py each);
# this file only routes. A prompt whose word after the dots is not a declared name
# passes through untouched — ".claude/x", "../scripts/y", ".histogram of z" are
# prompts, not commands. Design: CLAUDETTE_DESIGN.md "Dot-commands: the framework".
import json
import os
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

if data.get("hook_event_name") != "UserPromptSubmit":
    sys.exit(0)
# Not stripped: a leading space is the escape hatch for a prompt that would
# otherwise be captured.
prompt = data.get("prompt") or ""


def _index(to_context):
    """"." — the discovery command, rendered FROM the registry so it can never
    drift from dispatch. Two sections mirroring the namespace: the token-free
    commands, then the in-context-only ones behind a separator whose wording also
    teaches that ".." works on every command above it."""
    import dot_common as dc
    import dot_registry as dr
    free = sorted((e for e in dr.REGISTRY if e[4].get("free", True)),
                  key=lambda e: e[0][0])
    ctx = sorted((e for e in dr.REGISTRY if not e[4].get("free", True)),
                 key=lambda e: e[0][0])
    title = ("claudette dot-commands (%d) · These token-free commands skip context:"
             % len(dr.REGISTRY))
    sep = "── in context only, costs a turn (the .. prefix also works on any command above) ──"

    def help_of(e):
        names, _mod, _usage, hlp, _flags = e
        if len(names) > 1:
            hlp += "  (also: %s)" % ", ".join("." + a for a in names[1:])
        return hlp

    w = max(len(e[2]) for e in dr.REGISTRY)
    out = ["%s.%s %s— %s%s" % (dc.ACT, dc.R, dc.TIME, title, dc.R)]
    out += ["   %s%-*s%s %s— %s%s" % (dc.ACT, w, e[2], dc.R, dc.TIME, help_of(e), dc.R)
            for e in free]
    if ctx:
        out.append("   %s%s%s" % (dc.TIME, sep, dc.R))
        out += ["   %s%-*s%s %s— %s%s" % (dc.ACT, w, e[2], dc.R, dc.TIME, help_of(e), dc.R)
                for e in ctx]
    # The one argument every command takes (dc.HELP), said once for all of them.
    out.append("%sAdd %s%s %sto any command for its usage — %s.sess ?%s"
               % (dc.TIME, dc.R + dc.ACT, dc.HELP, dc.R + dc.TIME, dc.R + dc.ACT, dc.R))
    if to_context:
        dc.emit_context_block(out, ".")
    else:
        dc.emit_block(out)


if prompt[:1] in (".", "…"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dot_registry
    m = dot_registry.match(prompt)
    if m is not None:
        import dot_common as dc
        if m.entry is None:
            _index(m.to_context)
        elif not m.entry[4].get("free", True) and not m.to_context:
            # A context-only command typed with one dot gets a zero-token pointer
            # to the ".." form instead of silently spending a turn.
            name = m.entry[0][0]
            dc.emit_block(["%s.%s cannot run token-free — it needs the model. "
                           "Type %s..%s%s %sto run it in context (costs a turn).%s"
                           % (dc.TIME, name, dc.R + dc.ACT, name, dc.R, dc.TIME, dc.R)])
        else:
            name = m.entry[0][0]
            dc.TAIL = m.tail          # "?" = help: the emitters drop ESCAPE_HINT
            try:
                mod = __import__(m.entry[1])
                mod.handle(data, m.tail, m.to_context)
            except Exception as e:
                # Fail loudly on the channel that was asked for: an error on the
                # wrong channel is invisible (a blocked error never reaches the
                # phone), and a fall-through costs a turn.
                err = "❌ .%s failed: %s: %s" % (name, type(e).__name__, e)
                if m.to_context:
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": ("Claudette dot-command failure. Reply "
                                              "with EXACTLY this line and nothing "
                                              "else:\n") + err}}))
                else:
                    print(json.dumps({"decision": "block", "reason": err}))
        sys.exit(0)
    # dot-leading but not a declared command: a real prompt, untouched
# A typed /claudette:chart publishes the usage chart itself and needs the chart's address
# in its FIRST message: the live page's read rides beside `prepare` there, and without the
# address it waits for it, one more round trip for one URL (skills/chart/DESIGN.md, "The
# store is published files"). O(1): one small read, only on that prompt, silent on any
# failure.
import re                                                     # noqa: E402

CHART_CALL = re.compile(r"^\s*/claudette:chart\b", re.I)
_m = CHART_CALL.match(prompt)
if _m:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import usage_chart
        _url = (usage_chart._read_json(usage_chart.ADDR) or {}).get("url")
        if _url:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": usage_chart.hint(_url)}}))
    except Exception:
        pass
