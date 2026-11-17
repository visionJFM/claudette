#!/usr/bin/env python3
"""".status" (also: ".s") — the status-line snapshot, without the model.

Answers from the dot-command channel, so it costs no model turn. ".s" is the short
form ("..s" reaches the remote app with minimal typing). Both lines are read from
snapshots hook_statusline.py already writes; each carries its own staleness suffix,
because a stale line presented as current is the one wrong answer this command has.
"""
import json
import os
import time

import dot_common as dc
import paths


def snapshot_lines(data):
    """The "─── " lines: THIS session's last render + the latest global rate
    limits from any session. Shared with "..recap", which ends its summary with them."""
    def _load(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _stale(snap):
        age_min = (int(time.time()) - int(snap.get("ts", 0))) // 60
        return "  (%dm ago)" % age_min if age_min > 10 else ""

    sess = _load(os.path.join(paths.LOGS, "sessions",
                              str(data.get("session_id", "")) + ".json"))
    glob = _load(paths.USAGE_LAST)
    lines = []
    if sess and sess.get("line_session"):
        lines.append("─── " + sess["line_session"] + _stale(sess))
    if glob:
        if glob.get("line_global"):
            gline = glob["line_global"]
        else:
            # Fallback for snapshots without line_global (written by an older hook_statusline.py)
            rl = glob.get("rate_limits", {})
            p5 = rl.get("five_hour", {}).get("used_percentage")
            p7 = rl.get("seven_day", {}).get("used_percentage")
            caps = glob.get("caps") or {}
            h_rem = caps.get("h_remaining")
            d_rem = caps.get("d_remaining")
            parts = []
            if p5 is not None:
                parts.append("5h:%d%%" % int(p5)
                             + ("/%.0fh" % h_rem if h_rem is not None else ""))
            if p7 is not None:
                parts.append("7d:%d%%" % int(p7)
                             + ("/%.1fd" % d_rem if d_rem is not None else ""))
            gline = "  ".join(parts) + "  " + time.strftime("%H:%M") if parts else ""
        if gline:
            lines.append("─── " + gline + _stale(glob))
    return lines


def handle(data, tail, to_context):
    lines = snapshot_lines(data)
    out = []
    if tail:
        out.append("%s.status%s %s— usage: .status   (..status/..s = in context). No arguments.%s"
                   % (dc.ACT, dc.R, dc.TIME, dc.R))
    elif lines:
        # A command answers every input itself, so the empty case is said, not sent to the model.
        out.extend("%s%s%s" % (dc.TEXT, ln, dc.R) for ln in lines)
    else:
        out.append("%s.status%s %s— no status snapshot yet (statusline has not rendered)%s"
                   % (dc.ACT, dc.R, dc.TIME, dc.R))
    if to_context:
        dc.emit_context_block(out, ".status", noisy=bool(tail))
    else:
        dc.emit_block(out, noisy=bool(tail))
