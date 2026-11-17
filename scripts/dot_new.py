#!/usr/bin/env python3
"""".new [title]" — a fresh session in a new window, here, named by the title.

The short form of ".proj !new", plus a title: the text after the command becomes
the new session's name (`claude --name`), which the client shows in its prompt
box, the /resume picker and the window's title. "Here" is what a bare
".proj !new" means: the folder this session started in, never the live cwd
(dot_proj.act).

It acts WITHOUT "!" — like ".close", an exception to the registry's bang rule,
flagged "WARN:" in the index. The title is typed text
reaching a command line, so it is held to dot_proj.NAME_OK and refused, not
cleaned, when it does not fit: a launch under a name the user did not type is
worse than a retype.
"""
import dot_common as dc
import dot_proj
import dot_sess as ds

USAGE = ["%s.new%s %s— WARN: opens a new session in a new window, in this session's "
         "folder%s" % (dc.ACT, dc.R, dc.TIME, dc.R),
         "   %s.new Fix the login page%s %s— the text after it is the new session's "
         "title%s" % (dc.ACT, dc.R, dc.TIME, dc.R),
         "   %sa title: letters, digits, spaces and - _ . , : ' ( ) — up to 80, "
         "not starting with -%s" % (dc.TIME, dc.R),
         "   %sanother folder or a model: %s.proj !new N%s" % (dc.TIME, dc.R + dc.ACT, dc.R)]


def _emit(lines, to_context, noisy):
    if to_context:
        dc.emit_context_block(lines, ".new", noisy=noisy)
    else:
        dc.emit_block(lines, noisy=noisy)


def handle(data, tail, to_context):
    if dc.is_help(tail):
        return _emit(USAGE, to_context, False)
    if tail and not dot_proj.NAME_OK.match(tail):
        bad = sorted(set(ch for ch in tail if not dot_proj.NAME_OK.match("a" + ch)))
        why = (("not allowed in a title: %s" % " ".join(bad)) if bad
               else "a title cannot start with - or exceed 80 characters")
        return _emit(["%s.new%s %s— nothing launched: %s%s"
                      % (dc.ACT, dc.R, dc.RED, why, dc.R)] + USAGE, to_context, True)
    a = dot_proj.Args()
    a.action, a.name, a.label = "new", tail or None, ".new"
    allrows = ds.scan()
    if not allrows:
        return _emit(["%s.new%s %s— no transcripts under %s%s"
                      % (dc.ACT, dc.R, dc.TIME, ds.PROJECTS, dc.R)], to_context, False)
    lines = dot_proj.act(a, dot_proj.group(allrows), data.get("session_id") or "",
                         ds.registry(), ds.project_key(data))
    if tail and lines and "opened" in lines[0]:
        lines.insert(1, "   %stitled%s %s%s" % (dc.TIME, dc.R + dc.TEXT, tail, dc.R))
    return _emit(lines, to_context, False)
