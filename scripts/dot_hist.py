#!/usr/bin/env python3
"""".hist [N]" — this session's prompts, chronological, without the model.

The listing numbers what the user typed;
compactions and day changes render as dividers, never as rows, so ".hist N" keeps
meaning "the Nth thing I typed". ".hist N" shows entry N in full, with its own line
breaks — which is why the two channels need two renderers: the app reflows and eats
newlines (code fence, wrap 64), the terminal wraps short of its width and must be
pre-wrapped (dot_common.WIDTH says where).
"""
import os

import dot_common as dc

WIDTH = dc.WIDTH


def handle(data, tail, to_context):
    IDX = int(tail) if tail.isdigit() else 0
    bad_arg = bool(tail) and not tail.isdigit()

    tpath = data.get("transcript_path") or ""
    # entries carries the dividers too; rows is what gets numbered. A usage answer
    # reads no transcript, so no ERR line lands under the usage text.
    entries, _tools, err = dc.read_transcript(tpath) if not bad_arg else ([], [], "")
    rows = dc.numbered(entries)
    foot = dc.compact_footer(entries)

    # An out-of-range index is answered on the SAME channel rather than passed
    # through: the user asked a dot-command, so a dot-command answers.
    sel = rows[IDX - 1] if 1 <= IDX <= len(rows) else None
    bad_idx = bool(IDX) and sel is None

    # ANSI palette and its inverted-default subtleties: see dot_common.py.
    # Both channels ship this one layout: the ..form is the same lines, ANSI
    # stripped and fenced (dc.emit_context_block).
    ACT, TIME, TEXT, R = dc.ACT, dc.TIME, dc.TEXT, dc.R
    out = []
    if bad_arg:
        out.append("%s.hist%s %s— usage: .hist [N]   (N = entry number; ..hist = in context)%s"
                   % (ACT, R, TIME, R))
    elif bad_idx:
        out.append("%s.hist %d%s %s— no such entry; this session has %d%s"
                   % (ACT, IDX, R, TIME, len(rows), R))
    elif sel:
        out.append("%s.hist %d%s %s— prompt details · %s · %d chars%s%s"
                   % (ACT, IDX, R, TIME, dc.hm(sel.ts), len(sel.raw),
                      " · interrupted" if sel.stopped else "", R))
        for ln in dc.wrapped(sel.raw, WIDTH):
            out.append("%s%s%s" % (TEXT, ln, R))
    else:
        out.append("%s.hist%s %s— prompt history · %d prompt(s), oldest first%s"
                   % (ACT, R, TIME, len(rows), R))
        # The layout itself lives in dc.hist_lines — ".sess N" renders another
        # session's prompts with it, and two copies would drift.
        out.extend(dc.hist_lines(entries, WIDTH))
        if not rows and tpath and not os.path.exists(tpath):
            out.append("%s(no prompts yet — the client has not written this session's "
                       "transcript)%s" % (TIME, R))
        elif not rows:
            out.append("(no prompts found in %s)" % (os.path.basename(tpath) or "?"))
        else:
            if foot:
                out.append("%s   %s%s" % (TIME, foot, R))
            out.append("%sUse %s%s.hist N%s %sto show details%s"
                       % (TIME, R, ACT, R, TIME, R))
    if err:
        out.append("ERR %s" % err)
    if to_context:
        dc.emit_context_block(out, ".hist", noisy=bad_arg)
    else:
        dc.emit_block(out, noisy=bad_arg)
