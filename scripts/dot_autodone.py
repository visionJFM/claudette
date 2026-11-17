#!/usr/bin/env python3
"""".autodone [on|off] [all]" — the auto-done switch (both halves: the idle watchdog and
the compaction gate), without the model.

The marker file is written by the hook itself and confirmed for zero tokens. The
switch logic lives in watchdog_switch.py — the same files, the same single writer,
whichever door is used (this command, the CLI, or the skill).

The tail is matched against the exact grammar only — anything else is a usage
answer, never a state change: a toggle is a side effect, and a side effect fired
by a half-understood argument is worse than no feature.
"""
import os

import dot_common as dc
import watchdog_switch as ws


def _feature_line(data):
    try:
        import prefs
        if not prefs.effective("auto_done"):
            return "the auto_done feature is off on this machine"
        import auto_done
        return auto_done.describe(data.get("transcript_path"))
    except Exception as e:
        return "auto_done state unreadable (%s)" % str(e)[:60]


def handle(data, tail, to_context):
    sid = str(data.get("session_id") or "")
    words = tail.lower().split()
    verb = words[0] if words else ""
    glob = words[1:] == ["all"]
    valid = (not words) or (verb in ("on", "off") and (len(words) == 1 or glob))
    hints = []
    feature = ""

    if not valid:
        text = "usage: .autodone [on|off] [all]   (all = every session, this machine)"
        shown = ".autodone"
    elif not words:
        # State report only — reading the switches is free of side effects. Both
        # markers can be set at once, and ws.state() masks the session one behind
        # the global one, so read them separately here.
        # Whether it arms at all depends on the mode and this session's cache,
        # which the line under this one says (auto_done.describe).
        g = os.path.isfile(ws.GLOBAL_OFF)
        s = bool(sid) and os.path.isfile(ws.session_off(sid))
        # Hints list only the transitions that apply FROM this state — offering
        # ".autodone off" when it is already off is noise, not discoverability.
        if g and s:
            text = "auto-done is disabled globally and for this session"
            hints = [("on all", "re-enable every session, this machine"),
                     ("on", "clear this session's own off switch")]
        elif g:
            text = "auto-done is disabled globally"
            hints = [("on all", "re-enable every session, this machine")]
        elif s:
            text = "auto-done is disabled for this session"
            hints = [("on", "re-enable this session"),
                     ("off all", "disable every session, this machine")]
        else:
            text = "auto-done is enabled"
            hints = [("off", "disable this session only"),
                     ("off all", "disable every session, this machine")]
        # What the switches cannot show, on a line of its own: whether the feature
        # is on at all, its mode, and what this session's cache decides.
        feature = _feature_line(data)
        shown = ".autodone"
    else:
        text = ws.set_switch(verb == "off", glob, sid)
        shown = ".autodone " + " ".join(words)

    lines = ["%s%s%s %s— %s%s" % (dc.ACT, shown, dc.R, dc.TIME, text, dc.R)]
    if feature:
        lines.append("   %s%s%s" % (dc.TIME, feature, dc.R))
    lines += ["   %s.autodone %s%s %s— %s%s" % (dc.ACT, v, dc.R, dc.TIME, d, dc.R)
              for v, d in hints]
    if to_context:
        dc.emit_context_block(lines, ".autodone", noisy=not valid)
    else:
        dc.emit_block(lines, noisy=not valid)
