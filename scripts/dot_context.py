#!/usr/bin/env python3
"""".context" — which files entered this context window, without the model.

The builtin "/context" totals the categories; this is the itemized LEDGER of the
window: the instruction files the harness auto-loads (AUTO rows), every file that
came in through the file tools, then the exact remainder measured from the API's
own usage counts — system baseline (first turn) and conversation. File rows are
chars/4 estimates; the totals are exact, so the "conversation" row absorbs the
estimation error and everything Bash carried in (cat, a script's stdout), which
no transcript scan can attribute to a file (the dot_common.WRITE_TOOLS contract).
"""
import collections
import json
import os
import re

import dot_common as dc

# Tool name -> the flag a row shows. Every edit flavour is an E, and a Write is
# a C: the distinction the listing serves is created/edited/read, not which tool
# did it (an Edit writes the disk too — the flags are about how content moved).
OPS = {"Read": "R", "Edit": "E", "MultiEdit": "E", "NotebookEdit": "E", "Write": "C"}

# How the harness announces an injected instruction file inside a
# <system-reminder>: "Contents of /path/CLAUDE.md (project instructions...)".
# Matched ONLY inside reminder blocks — a Read result quoting this phrase in
# source code or docs must not register as an injection.
INJECTED = re.compile(r"Contents of (\S+?) \(")
REMINDER = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)

HOME = os.path.expanduser("~")



def _short(p):
    return "~" + p[len(HOME):] if p.startswith(HOME) else p


def _row(chars, ts=""):
    return {"ops": collections.Counter(), "ts": ts, "post": True, "chars": chars}


def memory_chain(cwd):
    """The instruction files the harness loads at startup, computed from disk —
    they are injected at request-build time and never recorded in the transcript,
    so scanning cannot find them. Mirrors the builtin /context "Memory files"
    set: ~/.claude/CLAUDE.md plus its line-leading @imports, then CLAUDE.md /
    CLAUDE.local.md from cwd up to the root."""
    seen = collections.OrderedDict()

    def add(p):
        p = os.path.abspath(os.path.expanduser(p))
        if p in seen or not os.path.isfile(p):
            return
        try:
            body = open(p, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            return
        # The harness strips <!-- --> before injecting, so comments must not
        # count here. Read tool content is NOT stripped — only these AUTO rows
        # get this.
        seen[p] = _row(len(re.sub(r"<!--.*?-->", "", body, flags=re.S)))
        for m in re.finditer(r"^@(\S+)", body, re.M):
            add(os.path.join(os.path.dirname(p), os.path.expanduser(m.group(1))))

    add(os.path.join(HOME, ".claude", "CLAUDE.md"))
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        for n in ("CLAUDE.md", "CLAUDE.local.md"):
            add(os.path.join(d, n))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return seen


def scan(tpath):
    """(injected, files, compacts, usage, err) from one session JSONL.

    files: path -> {ops Counter, ts of last touch, post (touched since the last
    compact boundary), chars (best known content size, 0 = unknown)}. File order
    is first-touch order. Sidechain records are skipped: a subagent's reads
    never enter THIS window.
    """
    files = collections.OrderedDict()
    injected, compacts, err = [], 0, ""
    usage = {"first": None, "last": None, "model": "", "selected": ""}
    try:
        with open(tpath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict) or d.get("isSidechain"):
                    continue
                if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    compacts += 1
                    for v in files.values():
                        v["post"] = False
                    continue
                if d.get("type") == "attachment":
                    # The model as selected, "[1m]" suffix included: the API id
                    # in message.model drops it. Re-announced on resume and on a
                    # model switch, so the last one is the window in force.
                    a = d.get("attachment") or {}
                    if a.get("type") == "model":
                        usage["selected"] = ((a.get("identity") or {}).get("modelId")
                                             or usage["selected"])
                    continue
                if d.get("type") == "assistant":
                    # The API's own count: exact window occupancy at that turn.
                    u = (d.get("message") or {}).get("usage")
                    if isinstance(u, dict):
                        tot = sum(u.get(k) or 0 for k in
                                  ("input_tokens", "cache_creation_input_tokens",
                                   "cache_read_input_tokens"))
                        if tot:
                            if usage["first"] is None:
                                usage["first"] = tot
                            usage["last"] = tot
                    usage["model"] = usage["model"] or \
                        (d.get("message") or {}).get("model") or ""
                    for b in (d.get("message") or {}).get("content") or []:
                        if not (isinstance(b, dict) and b.get("type") == "tool_use"
                                and b.get("name") in OPS):
                            continue
                        inp = b.get("input") or {}
                        path = inp.get("file_path") or inp.get("notebook_path")
                        if not path:
                            continue
                        v = files.setdefault(path, {"ops": collections.Counter(),
                                                    "ts": "", "post": True, "chars": 0})
                        v["ops"][OPS[b["name"]]] += 1
                        v["ts"] = d.get("timestamp", "")
                        v["post"] = True
                        if b["name"] == "Write":
                            v["chars"] = len(inp.get("content") or "")
                    continue
                if d.get("type") != "user":
                    continue
                # A Read's result carries the returned content: the only size
                # signal there is (a truncated read sizes what actually landed).
                tr = d.get("toolUseResult")
                if isinstance(tr, dict):
                    fi = tr.get("file")
                    if isinstance(fi, dict) and fi.get("filePath") in files:
                        files[fi["filePath"]]["chars"] = len(fi.get("content") or "")
                for sr in REMINDER.finditer(dc.entry_text(d)):
                    for m in INJECTED.finditer(sr.group(1)):
                        p = m.group(1)
                        if p not in injected:
                            injected.append(p)
    except FileNotFoundError as e:
        # Not written yet (see dc.read_transcript): an empty ledger, not an error.
        if not tpath:
            err = "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    return injected, files, compacts, usage, err


def _flags(ops):
    # Fixed C/E/R positions, presence only: "C·R" scans as columns, counts did
    # not — and "·" keeps the column from collapsing in a proportional font.
    # C is the rare flag, so rows usually open with "·", which reads as part of
    # the 3-space indent under the title — why rows sit only 1 space deeper.
    return "".join(l if ops[l] else "·" for l in "CER")


def _tok(chars):
    # Always k with one decimal ("~.9k", "~9.9k", "~99.9k"): a unit that changes
    # per line makes the reader re-parse each row. Leading zero dropped so the
    # column stays 7 wide at ~999.9k.
    if not chars:
        return ""
    s = "%.1f" % (chars / 4000.0)
    return "~%sk" % (s[1:] if s.startswith("0.") else s)


def _tokk(tokens):
    return _tok(tokens * 4)


def _window(model):
    # The transcript records no window size; map what this machine is known to
    # run. "[1m]" is how Claude Code suffixes 1M-context model selections, and
    # only the selected id carries it (scan's usage["selected"]).
    m = (model or "").lower()
    return 1000000 if ("[1m]" in m or "fable" in m) else 200000


def handle(data, tail, to_context):
    if tail:
        usage = "usage: .context   (no arguments; ..context = in context)"
        if to_context:
            dc.emit_context(["**.context** — %s" % usage], ".context", noisy=True)
        else:
            dc.emit_block(["%s.context%s %s— %s%s"
                           % (dc.ACT, dc.R, dc.TIME, usage, dc.R)], noisy=True)
        return

    injected, files, compacts, usage, err = scan(data.get("transcript_path") or "")
    head = ("context-window ledger · file rows ~chars/4, totals exact "
            "(API usage counts)")
    chain = memory_chain(data.get("cwd") or "")
    for p in injected:                     # lazily-injected project CLAUDE.md
        chain.setdefault(p, _row(0))
    for v in chain.values():
        v["ops"]["R"] = 1
        v["auto"] = True
    live = [(p, v) for p, v in files.items() if v["post"] and p not in chain]
    old = [(p, v) for p, v in files.items() if not v["post"] and p not in chain]

    def drow(p, v):
        return (_flags(v["ops"]), _tok(v["chars"]),
                "AUTO" if v.get("auto") else dc.hm(v["ts"]), _short(p))

    listed = list(chain.items()) + live
    live_tok = sum(v["chars"] // 4 for _, v in listed)
    sections = [("created/edited/read%s %s" %
                 (" since the last compact" if compacts else "", _tokk(live_tok)),
                 [drow(p, v) for p, v in listed])]
    if old:
        sections.append(("before the last compact — may survive only in the "
                         "summary", [drow(p, v) for p, v in old]))

    # The exact remainder: baseline is the first turn's usage total (system
    # prompt + tools + skills + the AUTO rows, subtracted so the column sums);
    # "conversation" is everything else — turn text, tool results, Bash-carried
    # content, and the file rows' estimation error, clamped at the floor.
    total = None
    if usage["last"]:
        auto_tok = sum(v["chars"] // 4 for v in chain.values())
        base = max(0, (usage["first"] or 0) - auto_tok)
        rest = max(0, usage["last"] - (usage["first"] or 0) - (live_tok - auto_tok))
        sections.append(("rest of the window",
                         [("···", _tokk(base), "",
                           "system baseline (prompt, tools, skills) minus AUTO rows"),
                          ("···", _tokk(rest), "",
                           "conversation & tool results (incl. Bash-carried)")]))
        win = _window(usage["selected"] or usage["model"])
        total = (_tokk(usage["last"]), win // 1000,
                 round(100.0 * usage["last"] / win))

    if to_context:
        # One 4-backtick fence for the whole ledger (dot_hist's trick): only
        # monospace keeps the columns, and the app eats spacing outside a fence.
        # No wrapping/trimming: horizontal scroll on mobile beats a taller
        # layout. Tighter indents than the block channel: width is the scarce
        # dimension there.
        md = ["**.context** — %s" % head, "````"]
        for title, rows in sections:
            if not rows:
                continue
            md.append("%s:" % title)
            for f, t, hm, label in rows:
                md.append(" %s %7s %5s %s" % (f, t, hm, label))
        if total:
            md.append("tot:%8s of %dk (%d%%)" % total)
        if not files and not injected:
            md.append("(no file-tool activity found)")
        md.append("````")
        if err:
            md.append("ERR %s" % err)
        dc.emit_context(md, ".context", noisy=False)
        return

    ACT, TITLE, TIME, TEXT, R = dc.ACT, dc.TITLE, dc.TIME, dc.TEXT, dc.R
    out = ["%s.context%s %s— %s%s" % (ACT, R, TIME, head, R)]
    for title, rows in sections:
        if not rows:
            continue
        out.append("%s  %s:%s" % (TITLE, title, R))
        for f, t, hm, label in rows:
            # Tokens carry the row's information, so they get TEXT like the
            # filename; flags and time stay dim. R before TEXT: TIME's dim
            # ATTRIBUTE outlives a bare foreground change and must be reset.
            # The label is a path: cut in the MIDDLE, or the file name — the
            # one part a reader scans for — is what goes.
            out.append("   %s%s %s%7s %s%5s%s %s%s%s"
                       % (TIME, f, R + TEXT, t, TIME, hm, R, TEXT,
                          dc.mid(label, dc.WIDTH - 21), R))
    if total:
        # Indented like the other blue lines; %8s puts the k right edge exactly
        # under the rows' (3 indent + 3 flags + 1 + 7).
        out.append("  %stot:%8s of %dk (%d%%)%s"
                   % (TITLE, total[0], total[1], total[2], R))
    if not files and not injected:
        out.append("   (no file-tool activity found)")
    if err:
        out.append("ERR %s" % err)
    dc.emit_block(out, noisy=False)
