#!/usr/bin/env python3
"""".close" — end this session and its console now, without the agent.

The free twin of /claudette:close: that one costs two model calls, an auto-mode
classifier verdict and a wait for the agent's last line; this one is the hook
alone. Same planner, same proofs (close.py); only the wait differs: no reply to
wait for, but the client writes the blocked prompt (the trace .sess reads as
"closed on purpose") after the hook returns, and a .close typed mid-turn is
blocked while the turn runs. So the detached killer waits for the session to go
idle and for that trace.

It acts WITHOUT "!", the one exception to the registry's bang rule, with the
warning in its index line instead. So only the bare word acts: any argument is a
usage answer, and a pasted multi-line text starting with ".close" is not a
command at all (no multiline flag). "..close" acts neither: the in-context
channel would start a turn the kill then cuts.
"""
import os

import close
import dot_common as dc

USAGE = ["%s.close%s %s— WARN: ends this session (claude) and closes its terminal "
         "window, after the current turn if one runs%s" % (dc.ACT, dc.R, dc.TIME, dc.R),
         "   %sno argument; the agent's form is /claudette:close%s" % (dc.TIME, dc.R)]


def _emit(lines, to_context, noisy):
    if to_context:
        dc.emit_context_block(lines, ".close", noisy=noisy)
    else:
        dc.emit_block(lines, noisy=noisy)


def handle(data, tail, to_context):
    if dc.is_help(tail):
        return _emit(USAGE, to_context, False)
    if tail:
        return _emit(["%s.close%s %s— takes no argument; nothing closed%s"
                      % (dc.ACT, dc.R, dc.TIME, dc.R)] + USAGE, to_context, True)
    if to_context:
        return _emit(["%s..close%s %s— not in this form: it would start a turn that "
                      "the close then cuts. Type %s.close%s%s, or /claudette:close%s"
                      % (dc.ACT, dc.R, dc.TIME, dc.R + dc.ACT, dc.R, dc.TIME, dc.R)],
                     to_context, False)
    sid = str(data.get("session_id") or "")
    env = dict(os.environ)
    if not env.get("CLAUDE_PID") and sid:
        pid = close.pid_of_session(sid)
        if pid:
            env["CLAUDE_PID"] = str(pid)
    p, why = close.launch(sid, env=env, wait="trace")
    if why:
        return _emit(["%s.close%s %s— NOT closed: %s. Close the window by hand.%s"
                      % (dc.ACT, dc.R, dc.RED, why[0], dc.R),
                      "   %s(%s)%s" % (dc.TIME, why[1], dc.R)], False, False)
    what = "claude pid %d" % p["claude"]
    if p["console"]:
        what += " and its terminal window"
    else:
        what += "; the terminal stays (an editor, tmux, ssh or another session owns it)"
    _emit(["%s.close%s %s— closing: %s, as soon as the session is idle. Log: %s%s"
           % (dc.ACT, dc.R, dc.TIME, what, close.LOG, dc.R)], False, False)
