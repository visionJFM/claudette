#!/usr/bin/env python3
"""".done" - the mechanical half of claudette-done, for zero tokens.

`done_probe.py` returns every git / memory / scratchpad / statusline fact in one
call, and the session's own TOUCHED set is recoverable from the transcript's
tool_use blocks - the source SKILL.md orders the model to use ("your own tool
history, NOT mtimes"). So six of the eight slots are rendered here with no model:

    3 C 6 7 R P  - mechanical, this command answers them
    1 2          - judgment, NOT answerable here

Hence this command's THIRD mark. ` ` / `?` / `!` cannot express "no model looked at
this", and a board that leaves 1 and 2 blank reads as an all-clear. `~` says
unjudged, and the footer says what to run to get them judged.

The recall listing is printed FIRST, above the board: it stands in for slot 1, since
the user reads back their own prompts and knows what this session produced.

This command NEVER writes, commits, pushes or moves anything - it cannot: a blocked
prompt never reaches the model. Every `!` still costs a `/claudette:done` to close.
"""
import json
import os
import subprocess
import sys
import time

import dot_common as dc

WIDTH = 94            # = dot_common.WIDTH; the comment there says where the client wraps
PROBE_TIMEOUT = 40    # seconds; a backstop
MAX_EXTRA_REPOS = 4   # repo roots from TOUCHED passed alongside cwd, in ONE probe call
LIST_CAP = 8          # files named per slot before the rest is counted

# Live state this session CREATED. "created" is the honest word - a blocked hook cannot
# ask whether a cron still exists or a background shell is still running.
LIVE_TOOLS = {"CronCreate": "cron", "TaskCreate": "task", "Agent": "subagent",
              "ScheduleWakeup": "wakeup", "EnterWorktree": "worktree"}


def _norm(p):
    return os.path.normcase(os.path.abspath(p).replace("\\", "/"))


def repo_root_of(path):
    """Walk up to the nearest `.git` (a dir in a normal clone, a FILE in a worktree
    or submodule - exists() takes both). Pure filesystem: no git, no subprocess."""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def run_probe(cwd, extra_roots, session):
    """ONE probe call for every repo. The FIRST --repo is primary and sets
    project_key, so it must be the session's FOLDER (cwd): memory and scratchpad are
    folder-scoped, and keying them off a touched repo would read the wrong store."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "done_probe.py")
    cmd = [sys.executable, script, "--repo", cwd]
    for r in extra_roots[:MAX_EXTRA_REPOS]:
        cmd += ["--repo", r]
    if session:
        cmd += ["--session", session]
    t0 = time.time()
    try:
        # The probe writes UTF-8 whatever the locale (done_probe.py main()).
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=PROBE_TIMEOUT)
        return json.loads(p.stdout), time.time() - t0, ""
    except Exception as e:
        return None, time.time() - t0, "%s: %s" % (type(e).__name__, e)


def repo_views(probe):
    """[(root, dirty_paths_set, repo_dict)] - primary first, then extra_repos."""
    views = []
    for rec in [probe] + list(probe.get("extra_repos") or []):
        r = rec.get("repo") or {}
        # is_git False still carries a "root" (the folder asked about). Counting it as a
        # repo would put an untracked home dir in the C/R/P arithmetic with 0 of
        # everything - and make a 1-repo report look like a 2-repo one.
        if not r.get("root") or not r.get("is_git"):
            continue
        views.append((r["root"], {d["path"] for d in (r.get("dirty") or [])}, r))
    return views


def build(data):
    """Every fact this command can state, as a dict. Rendering is per channel."""
    sid = str(data.get("session_id") or "")
    cwd = data.get("cwd") or os.getcwd()
    entries, tools, terr = dc.read_transcript(data.get("transcript_path") or "",
                                              want_tools=True)
    prompts = dc.numbered(entries)   # dividers are recall, never slot arithmetic

    # TOUCHED, from tool calls only. Ordered, de-duplicated, last write wins the place.
    touched, seen = [], set()
    for t in tools:
        if t["name"] in dc.WRITE_TOOLS:
            fp = t["input"].get("file_path") or t["input"].get("notebook_path")
            if fp and _norm(fp) not in seen:
                seen.add(_norm(fp))
                touched.append(fp)

    # The last claudette-done in this session, and the delta since: often the whole
    # decision ("3 prompts ago, 0 writes").
    last_done_ts = None
    for t in tools:
        if t["name"] == "Skill" and "done" in str(t["input"].get("skill", "")):
            last_done_ts = t["ts"]
    for e in prompts:
        # The INVOCATION only, never the words. A prompt that merely discusses
        # "/claudette:done" must not date a run that never happened - a false "you
        # already ran it" is the one wrong answer that costs work.
        if e.one.lstrip("/").startswith("claudette:done") and (not last_done_ts
                                                               or e.ts > last_done_ts):
            last_done_ts = e.ts
    since_prompts = sum(1 for e in prompts if last_done_ts and e.ts > last_done_ts)

    live = {}
    for t in tools:
        if t["name"] in LIVE_TOOLS:
            live[LIVE_TOOLS[t["name"]]] = live.get(LIVE_TOOLS[t["name"]], 0) + 1
        elif t["name"] == "Bash" and t["input"].get("run_in_background"):
            live["background shell"] = live.get("background shell", 0) + 1

    extra = []
    cwd_root = repo_root_of(cwd)
    for p in touched:
        r = repo_root_of(p)
        if r and (not cwd_root or _norm(r) != _norm(cwd_root)) \
                and _norm(r) not in {_norm(x) for x in extra}:
            extra.append(r)
    probe, secs, perr = run_probe(cwd, extra, sid)

    out = {"session": sid, "cwd": cwd, "prompts": prompts, "transcript_err": terr,
           "probe_err": perr, "secs": secs, "touched": touched, "live": live,
           "last_done_ts": last_done_ts, "since_prompts": since_prompts,
           "probe": probe, "repos_asked": [cwd] + extra}
    if not probe:
        return out

    # Where each touched file landed. Scratch is not committable, so it belongs to
    # slot 3 and must not count as a slot C gap.
    scratch_root = ((probe.get("scratchpad") or {}).get("root") or "\0")
    views = repo_views(probe)
    in_scratch, in_repo, homeless = [], [], []
    for p in touched:
        if _norm(p).startswith(_norm(scratch_root)):
            in_scratch.append(p)
            continue
        for root, dirty, _r in views:
            rel = os.path.relpath(p, root).replace("\\", "/")
            if not rel.startswith(".."):
                in_repo.append((root, rel, rel in dirty))
                break
        else:
            homeless.append(p)
    out.update({"in_scratch": in_scratch, "in_repo": in_repo, "homeless": homeless,
                "views": views})
    out["uncommitted"] = [(root, rel) for root, rel, d in in_repo if d]
    # Memory files this session wrote: slot 2's magnitude, never its verdict.
    mem_dir = (probe.get("memory") or {}).get("dir") or "\0"
    out["mem_written"] = [p for p in touched if _norm(p).startswith(_norm(mem_dir))]
    return out


# ------------------------------------------------------------------ slot lines

def slots(f):
    """[(mark, id, label, text, [continuation lines])] - channel-independent.

    mark: " " clean, "~" unjudged (no model looked), "?" content nobody opened,
          "!" confirmed gap. "~" is this command's own, and exists so a mechanically
          clean board can never be misread as "safe to close".
    """
    probe = f.get("probe") or {}
    rows = []
    n_touch, n_prompts = len(f["touched"]), len(f["prompts"])

    # 1 Context - never answerable here. The magnitude is still worth stating.
    ctx = "%d prompt(s) in this session" % n_prompts
    if f["last_done_ts"]:
        ctx += ", %d since the last claudette-done (%s)" % (f["since_prompts"],
                                                            dc.hm(f["last_done_ts"]))
    else:
        ctx += ", no claudette-done ran in it"
    rows.append(("~", "1", "Context", ctx, ["→ only /claudette:done can judge this"]))

    if not probe:
        rows.append(("!", "-", "probe", "done_probe.py failed: %s" % f["probe_err"], []))
        return rows

    mem = probe.get("memory") or {}
    mw = f.get("mem_written") or []
    rows.append(("~" if mw else " ", "2", "Memory",
                 ("%d memory file(s) written this session — mirror not checked" % len(mw))
                 if mw else "Ok, this session wrote no memory file", []))

    # 3 Scratch - the probe counts the dir, TOUCHED names the ones we put there.
    self_pad = (probe.get("scratchpad") or {}).get("self") or {}
    n_pad = self_pad.get("files") or 0
    if not n_pad:
        rows.append((" ", "3", "Scratch", "Ok, empty", []))
    else:
        names = [os.path.basename(p) for p in f.get("in_scratch") or []]
        # "%d from a tool call" is not the same claim as "%d files": the rest were written
        # by scripts this session ran, which TOUCHED cannot see. Say the gap, don't imply it.
        rows.append(("~", "3", "Scratch",
                     "%d file(s), %d from a tool call — deliverable? not judged"
                     % (n_pad, len(names)),
                     [", ".join(names[:LIST_CAP])] if names else []))

    # C Commit - TOUCHED ∩ dirty. The one slot where this command matches the skill.
    unc, in_repo = f.get("uncommitted") or [], f.get("in_repo") or []
    cont = []
    if unc:
        multi = len({r for r, _ in unc}) > 1
        for root, rel in unc[:LIST_CAP]:
            cont.append(("%s: %s" % (os.path.basename(root), rel)) if multi else rel)
        if len(unc) > LIST_CAP:
            cont.append("… and %d more" % (len(unc) - LIST_CAP))
        cont.append("→ /claudette:done C")
        txt = "%d of %d file(s) this session wrote are uncommitted" % (len(unc), len(in_repo))
    elif in_repo:
        txt = "Ok, all %d file(s) this session wrote are committed" % len(in_repo)
    else:
        txt = "Ok, this session wrote no file inside a repo"
    if f.get("homeless"):
        cont.append("+ %d file(s) written outside any repo: %s"
                    % (len(f["homeless"]), ", ".join(os.path.basename(p)
                                                     for p in f["homeless"][:4])))
    rows.append(("!" if unc else " ", "C", "Commit", txt, cont))

    # 6 Memory (folder) - a default run names it and does not open it; so does this.
    cnt = mem.get("count")
    if not mem.get("exists") or not cnt:
        rows.append((" ", "6", "Memory", "Ok, empty", []))
    else:
        tc = mem.get("type_counts") or {}
        proj = tc.get("project", 0)
        rows.append(("?" if proj else " ", "6", "Memory",
                     "%d entries, %d type:project — not opened" % (cnt, proj)
                     if proj else "Ok, %d entries, none type:project" % cnt,
                     ["→ /claudette:done check 6"] if proj else []))
        if mem.get("capped"):
            rows[-1][4].append("%d entry(ies) unchecked (cap)" % len(mem.get("unchecked") or []))

    # 7 Scratch (folder) - unowned siblings, liveness from the probe's statusline read.
    pad = probe.get("scratchpad") or {}
    unowned = pad.get("unowned") or []
    if not unowned:
        # siblings_total, NOT len(siblings): the probe collapses the empty-and-not-live
        # ones to a count, so the array does not hold one record per dir.
        rows.append((" ", "7", "Scratch", "Ok, %d sibling dir(s), all empty or live"
                     % pad.get("siblings_total", len(pad.get("siblings") or [])), []))
    else:
        tot = sum(u.get("files") or 0 for u in unowned)
        cont = []
        for u in unowned[:3]:
            names = [n["path"] for n in (u.get("names") or [])][:3]
            cont.append("%s  %d files  %s  %s" % (u["session"][:8], u.get("files") or 0,
                                                  u.get("newest_age") or "?",
                                                  ", ".join(names)))
        if len(unowned) > 3:
            cont.append("… and %d more dir(s)" % (len(unowned) - 3))
        cont.append("→ /claudette:done check 7")
        rows.append(("?", "7", "Scratch", "%d unowned dir(s), %d files, nobody appears to own them"
                     % (len(unowned), tot), cont))

    # R Commit - everyone's dirt minus ours. Ages are a HINT; the user knows the windows.
    views, mine = f.get("views") or [], {(r, p) for r, p in (f.get("uncommitted") or [])}
    others, cont = 0, []
    for root, dirty, r in views:
        n_mine = sum(1 for rr, pp in mine if rr == root)
        n_other = len(dirty) - n_mine
        if n_other <= 0:
            continue
        others += n_other
        for g in (r.get("dirty_groups") or [])[:3]:
            cont.append("%s%s  %d  %s" % ((os.path.basename(root) + "/") if len(views) > 1 else "",
                                          g["folder"], g["files"], g.get("newest_age") or "?"))
        # The probe groups ALL dirt; the headline counts only what is not ours. Without
        # this line the two numbers contradict each other and the report loses the reader.
        if n_mine:
            cont.append("(rows count all dirt, including this session's %d in C above)" % n_mine)
    live_sib = len((probe.get("scratchpad") or {}).get("live_siblings") or [])
    if others:
        cont.append("%d live session(s) elsewhere — some of this may be theirs" % live_sib
                    if live_sib else "no live session seen elsewhere")
        cont.append("→ /claudette:done check R")
    rows.append(("!" if others else " ", "R", "Commit",
                 "%d dirty file(s) not this session's" % others if others
                 else "Ok, nothing dirty beyond this session's", cont))

    # P Push - the current branch's ahead-count, and nothing else. Other branches are
    # not this command's business (skills/done/DESIGN.md).
    ahead, cont = 0, []
    for root, _d, r in views:
        a = r.get("ahead") or 0
        if a:
            ahead += a
            cont.append("%s: %d ahead on %s" % (os.path.basename(root), a, r.get("branch")))
    if ahead:
        cont.append("→ /claudette:done P")
    rows.append(("!" if ahead else " ", "P", "Push",
                 "%d commit(s) ahead of the remote" % ahead if ahead
                 else "Ok, current branch not ahead", cont))

    # Upstream gate - outranks the board: editing a repo that is behind is the expensive
    # mistake, and this is the same check CLAUDE.md demands before touching a repo.
    for rec in [probe] + list(probe.get("extra_repos") or []):
        u = rec.get("upstream_check") or {}
        if (u.get("verified_behind") or 0) > 0 or u.get("moved_unknown"):
            # id ↓ = the statusline's own "behind" glyph; `!` would collide with
            # the confirmed-gap mark.
            rows.insert(0, ("!", "↓", "Behind",
                            "%s is %s behind its upstream — do not edit before pulling"
                            % (os.path.basename(u.get("repo") or rec.get("repo_arg") or "?"),
                               u.get("verified_behind") or "?"), []))
    return rows


def footer(f):
    lines = []
    live = f.get("live") or {}
    if live:
        lines.append("· created this session: %s (still running? not checked here)"
                     % ", ".join("%d %s" % (n, k) for k, n in sorted(live.items())))
    lines.append("· TOUCHED read from tool calls; files written by scripts are not counted")
    lines.append("Run /claudette:done to judge 1 and 2 — and it is the only thing that can act.")
    return lines


def status_lines(f):
    sl = ((f.get("probe") or {}).get("status_lines") or {})
    out = []
    for name in ("session", "global"):
        s = sl.get(name) or {}
        if s.get("present") and s.get("line"):
            out.append("─── " + s["line"] + (s.get("suffix") or ""))
    return out


# -------------------------------------------------------------------- channels

def emit_blocked(f, usage, to_context=False):
    ACT, OK, RED = dc.ACT, dc.OK, dc.RED
    TIME, TEXT, R = dc.TIME, dc.TEXT, dc.R
    emit = ((lambda o, **kw: dc.emit_context_block(o, ".done", **kw))
            if to_context else dc.emit_block)
    out = []
    if usage:
        out.append("%s.done%s %s— usage: .done   (..done = in context). No arguments yet.%s"
                   % (ACT, R, TIME, R))
        emit(out, noisy=True)
        return
    out.append("%s.done%s %s— facts only, in %.1fs · nothing judged, nothing written%s"
               % (ACT, R, TIME, f["secs"], R))
    for i, e in enumerate(f["prompts"], 1):
        # "⏹" between time and prompt — the misalignment is the signal (same
        # rule as the .hist listing).
        out.append("%2d %s%s%s%s %s%s%s" % (i, TIME, dc.hm(e.ts), R,
                                            "⏹" if e.stopped else "", TEXT,
                                            dc.cut(e.one, WIDTH - 12), R))
    if not f["prompts"]:
        out.append("%s(no prompts found)%s" % (TIME, R))
    out.append("")
    out.append("DONE PERSISTENCE STATUS%s(mechanical only — 1 and 2 need the skill)%s"
               % (" " * 22 + TIME, R))
    for mark, sid, label, text, cont in slots(f):
        col = RED if mark == "!" else (OK if mark == " " else TEXT)
        # Columns: mark 2, id 4, label 6, colon 14 - the skill's report shape, minus its
        # diff-fence colour prefix, which has no meaning outside a fence.
        out.append("%s %s %s %-8s: %s%s" % (col, mark, sid, label, dc.cut(text, WIDTH - 16), R))
        for c in cont:
            out.append("%s%s%s%s" % (" " * 16, TIME, dc.cut(c, WIDTH - 16), R))
    for ln in footer(f):
        out.append("%s%s%s" % (TIME, dc.cut(ln, WIDTH), R))
    for ln in status_lines(f):
        out.append("%s%s%s" % (TEXT, ln, R))
    if f.get("transcript_err"):
        out.append("ERR %s" % f["transcript_err"])
    emit(out)


def handle(data, arg, to_context):
    """Entry point from hook_status_prompt.py. Always answers on the requested
    channel - never falls through to the model, which is the most expensive outcome
    a dot-command has (a turn, plus the model reverse-engineering the hook).
    Both channels ship the board's one layout: the ..form is the same lines,
    fenced (dc.emit_context_block), so a phone scrolls sideways rather than
    reflowing the board."""
    usage = bool(arg)
    f = {} if usage else build(data)
    emit_blocked(f, usage, to_context)
