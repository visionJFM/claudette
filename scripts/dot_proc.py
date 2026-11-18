#!/usr/bin/env python3
""".proc" — processes Claude sessions started, and why, without the model.

Two views of one snapshot (CLAUDETTE_DESIGN.md, "`.proc`: which session started
this process, and why"):
  1. what still runs UNDER each live session — its descendants, found by ancestry;
  2. what OUTLIVED the call that started it — a process of this desktop session
     (Windows; the same uid elsewhere) whose parent is gone, matched to a shell
     call of some session when BOTH hold: it started inside the call's window,
     and its program name is a word of the call's command.
     A `.proj`/`.sess` launch report counts as such a call, and outranks one.
Either way the reason shown is the one the session gave for that call (the tool's
`description`), read back from the transcripts — they already are the launch record,
so nothing runs per command to keep one.

What it reads: process ids, names, start times and command lines — what Task Manager
shows. Never another process's environment, which holds the session's secrets.
Stopping a process from here is deliberately absent.
"""
import json
import os
import re
import subprocess
import sys
import time

import dot_common as dc
import dot_sess
import paths
import sess_launch
from redact import mask  # one masker for this display and the run logs

WIDTH = dc.WIDTH
USAGE = ".proc"
ALL = ":all"             # the one scope word: unfold what the footer counts

SLACK = 3.0              # seconds either side of a call's window: stamps vs process start
OPEN_CAP = 7200.0        # a call with no result in a closed session: the longest background run
WITH_SESSION = 60.0      # a child this close to its session's start came with it (MCP, plugin)
SCAN_BUDGET = 512 << 20  # transcript bytes read per listing; disclosed when it bites
HIDE = ("conhost.exe", "openconsole.exe")   # console hosts: noise, never a launch
CLAUDE = ("claude.exe", "claude")           # walked through, never shown
FILETIME_EPOCH = 11644473600

# Test seams: a synthetic snapshot and call list replace the real ones.
SNAPSHOT = None          # callable() -> (procs, own_desktop_session, err)
CALLS = None             # callable(times, live) -> (calls, capped)


class Proc(object):
    __slots__ = ("pid", "ppid", "name", "created", "cmd", "sess")

    def __init__(self, pid, ppid, name, created, cmd="", sess=None):
        self.pid, self.ppid, self.name = int(pid), int(ppid), (name or "").lower()
        self.created, self.cmd, self.sess = created, cmd or "", sess


# ------------------------------------------------------------------ snapshot

_PS = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
       "Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,"
       "CreationDate,CommandLine,SessionId | ForEach-Object {"
       "[pscustomobject]@{p=$_.ProcessId;q=$_.ParentProcessId;n=$_.Name;"
       "c=$(if($_.CreationDate){$_.CreationDate.ToFileTimeUtc()}else{0});"
       "l=$_.CommandLine;s=$_.SessionId}} | ConvertTo-Json -Compress")


def _win_snapshot():
    """CIM, through Windows PowerShell by absolute path: a bare name resolves against
    PATH (the WSL bash.exe trap in sess_launch.py). One call gives start times and
    command lines with no memory read of ours; ConvertTo-Json would mangle a
    DateTime, hence the FILETIME integer. CREATE_NO_WINDOW: a listing that exists to
    explain windows must not flash one."""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(exe):
        exe = "powershell"
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _PS],
                           capture_output=True, timeout=45, creationflags=0x08000000)
        rows = json.loads(r.stdout.decode("utf-8-sig", "replace") or "[]")
    except Exception as e:
        return [], None, "process list unavailable (%s)" % type(e).__name__
    if isinstance(rows, dict):
        rows = [rows]
    procs = []
    for x in rows:
        try:
            ft = int(x.get("c") or 0)
            procs.append(Proc(x["p"], x.get("q") or 0, x.get("n"),
                              ft / 1e7 - FILETIME_EPOCH if ft else None,
                              x.get("l"), x.get("s")))
        except Exception:
            continue
    own = next((p.sess for p in procs if p.pid == os.getpid()), None)
    return procs, own, "" if procs else "process list came back empty"


def _posix_snapshot():
    """Two `ps` calls joined on pid: `lstart` is five words, and both `comm` and
    `command` may hold spaces, so each variable-width field must end its own line.
    LC_ALL=C pins lstart's format. Same-user only: `sess` carries the uid."""
    env = dict(os.environ, LC_ALL="C")
    try:
        a = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,uid=,lstart=,comm="],
                           capture_output=True, text=True, timeout=10, env=env).stdout
        b = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                           capture_output=True, text=True, timeout=10, env=env).stdout
    except Exception as e:
        return [], None, "process list unavailable (%s)" % type(e).__name__
    cmds = {}
    for ln in b.splitlines():
        parts = ln.strip().split(None, 1)
        if parts and parts[0].isdigit():
            cmds[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    procs = []
    for ln in a.splitlines():
        parts = ln.split(None, 8)
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        try:
            created = time.mktime(time.strptime(" ".join(parts[3:8]),
                                                "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            created = None
        pid = int(parts[0])
        procs.append(Proc(pid, parts[1], os.path.basename(parts[8].strip()),
                          created, cmds.get(pid, ""), int(parts[2])))
    return procs, os.getuid(), "" if procs else "process list came back empty"


def snapshot():
    if SNAPSHOT is not None:
        return SNAPSHOT()
    return _win_snapshot() if sys.platform == "win32" else _posix_snapshot()


# --------------------------------------------------------------- the tree

def _parent_alive(par, child):
    """A parent id is trusted only if that process is alive AND started before the
    child: ids are recycled, and a newer process under an old id is not a parent.
    One second of tolerance for `ps`'s whole-second start times. Unknown start →
    trusted, since "detached" is the claim that needs evidence."""
    if par is None or par.pid == child.pid:
        return False
    if par.created is None or child.created is None:
        return True
    return par.created <= child.created + 1.0


def build(procs):
    """(by_pid, children, parent_alive_set)."""
    by = {p.pid: p for p in procs}
    kids, attached = {}, set()
    for p in procs:
        par = by.get(p.ppid)
        if _parent_alive(par, p):
            kids.setdefault(par.pid, []).append(p)
            attached.add(p.pid)
    for v in kids.values():
        v.sort(key=lambda q: (q.created or 0, q.pid))
    return by, kids, attached


def own_chain(by, attached):
    chain, pid, seen = set(), os.getpid(), set()
    while pid in by and pid not in seen:
        seen.add(pid)
        chain.add(pid)
        if pid not in attached:
            break
        pid = by[pid].ppid
    return chain


# Matched as a path TAIL: a hook's command line names the scripts folder as
# C:/Users/x/.claude/… from python, but /c/Users/x/.claude/… from the bash that
# wraps it, and the tail below home is what both spellings share.
try:
    _INFRA_TAIL = os.path.relpath(paths.SCRIPTS, paths.HOME)
except ValueError:              # another drive than home: no shared tail, use it whole
    _INFRA_TAIL = paths.SCRIPTS
_INFRA_TAIL = "/" + _INFRA_TAIL.replace("\\", "/").lower().strip("/") + "/"


def _is_infra(p, chain, session_pids):
    """Never shown, subtree included: this listing's own chain (hook, snapshot
    shell), claudette's hooks and status line mid-render in any session, console
    hosts, and a registered session's claude nested under another."""
    if p.pid in chain or p.name in HIDE or p.pid in session_pids:
        return True
    return _INFRA_TAIL in p.cmd.replace("\\", "/").lower()


def tops(root_pid, kids, chain, session_pids):
    """The visible children of a session: walk through claude nodes, skip infra
    subtrees. Each top carries its subtree for name matching."""
    out, stack = [], list(reversed(kids.get(root_pid, [])))
    while stack:
        p = stack.pop()
        if _is_infra(p, chain, session_pids):
            continue
        if p.name in CLAUDE:
            stack.extend(reversed(kids.get(p.pid, [])))
            continue
        out.append(p)
    return out


def subtree(p, kids, chain, session_pids):
    res, stack = [], [p]
    while stack:
        q = stack.pop()
        if q is not p and _is_infra(q, chain, session_pids):
            continue
        res.append(q)
        stack.extend(kids.get(q.pid, []))
    return res


# ------------------------------------------------------------ the calls

def _epoch(ts):
    o = dc.when(ts)
    return o.timestamp() if o else None


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text") or ""
    return ""


_BANG = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)

# A launch report as the dot-command printed it, ANSI stripped: the head line
# with how many windows opened, and the trailer naming the terminal and shell.
_LAUNCH_HEAD = re.compile(r"^(\.(?:proj|sess) ![\w-]+) — (?:opened|resumed|forked) (\d+) ",
                          re.M)
_LAUNCH_TAIL = re.compile(r"^\s*(\S+) · (\S+) · %s" % re.escape(sess_launch.OUTLIVE), re.M)
_TYPED = re.compile(r"^Original prompt: (.+)$", re.M)
# What a new window can leave behind besides its shell: a Windows Terminal host
# started for it, named unlike the terminal id the trailer prints.
_HOSTS = {"windows-terminal": "wt windowsterminal"}


def _launch_record(d):
    """A `.proj !…` / `.sess !r|!f` report read back as the call that opened its
    windows: {cmd, why, who}, or None. Only the two records the client writes for
    a dot-command's output count — the blocked prompt (system) and the in-context
    form (hook_additional_context) — never a message or tool result that merely
    quotes the same text. The typed prompt is the reason; the client's
    "Original prompt:" carries it whole (indices too), the head line otherwise."""
    if d.get("type") == "system":
        texts = [d.get("content")]
    else:
        a = d.get("attachment")
        if not (isinstance(a, dict) and a.get("type") == "hook_additional_context"):
            return None
        texts = a.get("content")
        texts = texts if isinstance(texts, list) else [texts]
    text = dc.ANSI.sub("", "\n".join(t for t in texts if isinstance(t, str)))
    head, tail = _LAUNCH_HEAD.search(text), _LAUNCH_TAIL.search(text)
    if not head or not tail or head.group(2) == "0":
        return None
    typed = _TYPED.search(text)
    term, shell = tail.groups()
    return {"cmd": "%s %s claude" % (_HOSTS.get(term, term), shell), "who": "dot",
            "why": (typed.group(1) if typed else head.group(1)).strip()}


def shell_calls(path):
    """Every command a session ran, with its window: [{t0, t1, cmd, why, who}].

    A shell call is any tool_use whose input carries a string `command` (Bash,
    PowerShell, Monitor, …): t0 is its assistant stamp, t1 the stamp of its
    tool_result, paired by id. A `!` prompt command is the user's own: t0 its
    <bash-input>, t1 the next <bash-stdout>/<bash-stderr>. A `.proj`/`.sess`
    launch is the user's too, and has no shell call at all — it runs inside the
    prompt hook — so its report stands in for one (_launch_record), stamped
    ~10 ms after the windows it opened. Own reader, not dc.read_transcript: that
    one is .hist's hot path and pairs nothing."""
    calls, pending, bang, nbytes = [], {}, None, 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                nbytes += len(line)
                if ('"tool_use' not in line and "<bash-" not in line
                        and sess_launch.OUTLIVE not in line):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = _epoch(d.get("timestamp"))
                content = (d.get("message") or {}).get("content")
                if d.get("type") == "assistant" and isinstance(content, list):
                    for b in content:
                        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                            continue
                        inp = b.get("input") or {}
                        cmd = inp.get("command") if isinstance(inp, dict) else None
                        if isinstance(cmd, str) and cmd.strip() and ts is not None:
                            c = {"t0": ts, "t1": None, "cmd": cmd, "who": "claude",
                                 "why": inp.get("description") or ""}
                            calls.append(c)
                            pending[b.get("id")] = c
                elif d.get("type") == "user":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                c = pending.pop(b.get("tool_use_id"), None)
                                if c is not None:
                                    c["t1"] = ts
                    text = _text_of(content)
                    m = _BANG.search(text)
                    if m and ts is not None:
                        bang = {"t0": ts, "t1": None, "cmd": m.group(1).strip(),
                                "who": "you", "why": ""}
                        calls.append(bang)
                    elif bang is not None and ("<bash-stdout>" in text
                                               or "<bash-stderr>" in text):
                        bang["t1"] = ts
                        bang = None
                elif d.get("type") in ("system", "attachment") and ts is not None:
                    c = _launch_record(d)
                    if c is not None:
                        c.update(t0=ts, t1=ts)
                        calls.append(c)
    except OSError:
        pass
    return calls, nbytes


def transcript_calls(times, live):
    """Calls from every transcript whose life overlaps one of `times` — main and
    subagent files, session = the main transcript's id. Bounded by SCAN_BUDGET."""
    if CALLS is not None:
        return CALLS(times, live)
    if not times:
        return [], False
    lo = min(times) - SLACK
    out, spent, capped = [], 0, False
    for rec in dot_sess.scan():                     # newest first
        if rec["mtime"] < lo:
            break
        m = dot_sess.meta(rec)
        start = _epoch(m.get("started")) or 0
        if not any(start - SLACK <= t <= rec["mtime"] + SLACK for t in times):
            continue
        files = [rec["path"]]
        sub = os.path.join(os.path.dirname(rec["path"]), rec["sid"], "subagents")
        try:
            files += [os.path.join(sub, n) for n in os.listdir(sub) if n.endswith(".jsonl")]
        except OSError:
            pass
        for fp in files:
            if spent > SCAN_BUDGET:
                capped = True
                break
            try:
                if os.path.getmtime(fp) < lo:
                    continue
            except OSError:
                continue
            calls, n = shell_calls(fp)
            spent += n
            for c in calls:
                c.update(sid=rec["sid"], title=m.get("title") or "",
                         cwd=m.get("cwd") or "")
            out.extend(calls)
        if capped:
            break
    now = time.time()
    for c in out:
        # "open": still running in a live session — what it started has not
        # outlived it, whatever the parent link says.
        c["open"] = c["t1"] is None and c["sid"] in live
        if c["t1"] is None:
            c["t1"] = now if c["open"] else c["t0"] + OPEN_CAP
    return out, capped


def _stem(name):
    n = name.lower()
    return n[:-4] if n.endswith(".exe") else n


def match(procs_in_subtree, created, calls):
    """Calls whose window holds `created` AND whose command names one of the
    subtree's programs as a word. Time alone is never enough: with several
    sessions it attributes to whichever happened to be running a command."""
    if created is None:
        return []
    stems = {_stem(p.name) for p in procs_in_subtree if p.name}
    pats = [re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(s)) for s in stems if s]
    hits = []
    for c in calls:
        if not (c["t0"] - SLACK <= created <= c["t1"] + SLACK):
            continue
        low = c["cmd"].lower()
        if any(p.search(low) for p in pats):
            hits.append(c)
    # A launch report first: it is the record of opening exactly these windows,
    # whereas a shell call of the same instant may only share the shell's name.
    hits.sort(key=lambda c: (c["who"] != "dot", abs(created - c["t0"])))
    return hits


def match_in_session(sub, created, calls, sid):
    """For a process known by ancestry to be session `sid`'s: the name match
    first, else that session's own call running at the time. Time alone is safe
    HERE because the session is already settled — the misattribution it causes
    is between sessions, and only this session's calls are candidates."""
    hits = match(sub, created, calls)
    if hits or created is None:
        return hits
    own = [c for c in calls if c["sid"] == sid
           and c["t0"] - SLACK <= created <= c["t1"] + SLACK]
    own.sort(key=lambda c: abs(created - c["t0"]))
    return own


def _fold(s, o, c):
    """An orphan of a call still running in live session `s` is part of that
    command, not a survivor: Git Bash pipelines lose the Windows parent link
    (`ls | head` would read as two "outlived" processes). It joins the item that
    started in the same call's window, else becomes an item of its own."""
    for it in s["items"]:
        t = it["p"].created
        if t is not None and c["t0"] - SLACK <= t <= c["t1"] + SLACK:
            it["sub"] = it["sub"] + o["sub"]
            if not it["hits"]:
                it["hits"], it["with"] = [c], False
            return
    o["with"] = False
    s["items"].append(o)


# ---------------------------------------------------------------- collect

def collect(procs, own_sess, live, now=None):
    """The whole listing as data: {sessions, orphans, unmatched, rest, unread,
    capped} — `rest` the outlived processes no call matched, `unread` how many
    more were left out for having no readable command line."""
    now = time.time() if now is None else now
    by, kids, attached = build(procs)
    chain = own_chain(by, attached)
    session_pids = {int(r.get("pid") or -1): sid for sid, r in live.items()}
    sessions = []
    for sid, r in live.items():
        root = int(r.get("pid") or -1)
        if root not in by:
            continue
        items = [{"p": p, "sub": subtree(p, kids, chain, session_pids)}
                 for p in tops(root, kids, chain, set(session_pids) - {root})]
        sessions.append({"sid": sid, "pid": root, "title": r.get("name") or "",
                         "cwd": r.get("cwd") or "", "this": root in chain,
                         "born": by[root].created, "items": items})
    orphans = []
    for p in procs:
        if p.pid in attached or p.pid in session_pids or p.created is None:
            continue
        if own_sess is not None and p.sess != own_sess:
            continue
        # A claude that is not a registered session stays: a detached `claude -p`
        # is exactly the kind of launch this listing exists to explain.
        if p.pid <= 4 or _is_infra(p, chain, session_pids):
            continue
        orphans.append({"p": p, "sub": subtree(p, kids, chain, session_pids)})
    times = [it["p"].created for s in sessions for it in s["items"] if it["p"].created]
    times += [o["p"].created for o in orphans]
    calls, capped = transcript_calls(times, live)
    for s in sessions:
        for it in s["items"]:
            it["hits"] = match_in_session(it["sub"], it["p"].created, calls, s["sid"])
            it["with"] = (not it["hits"] and s["born"] is not None
                          and it["p"].created is not None
                          and it["p"].created - s["born"] < WITH_SESSION)
    by_sid = {s["sid"]: s for s in sessions}
    matched, rest, unread = [], [], 0
    for o in orphans:
        o["hits"] = match(o["sub"], o["p"].created, calls)
        if not o["hits"]:
            # No readable command line: another account's process (SYSTEM's csrss
            # and winlogon share the desktop session) or an elevated one. Not this
            # listing's to explain, so neither counted nor listed — but matched
            # above all the same, since its name is readable.
            if o["p"].cmd:
                rest.append(o)
            else:
                unread += 1
            continue
        c = o["hits"][0]
        s = by_sid.get(c["sid"]) if c.get("open") else None
        if s is None:
            matched.append(o)
        else:
            _fold(s, o, c)
    sessions.sort(key=lambda s: (not s["this"], not s["items"], s["title"].lower()))
    matched.sort(key=lambda o: o["p"].created or 0)
    rest.sort(key=lambda o: o["p"].created or 0)
    return {"sessions": sessions, "orphans": matched, "unmatched": len(rest),
            "rest": rest, "unread": unread, "capped": capped, "live": live, "now": now}


# ----------------------------------------------------------------- render


def ago(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02d" % (sec // 3600, sec % 3600 // 60)
    return "%dd" % (sec // 86400)


def started(t, now):
    """("18:41", "16h02"): the clock alone today, the weekday within a week, the
    date beyond — a bare weekday would repeat."""
    lt, ln = time.localtime(t), time.localtime(now)
    if lt[:3] == ln[:3]:
        fmt = "%H:%M"
    elif now - t < 6 * 86400:
        fmt = "%a %H:%M"
    else:
        fmt = "%d %b %H:%M"
    return time.strftime(fmt, lt), ago(now - t)


def _one_line(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _proc_lines(it, now, session_label=None):
    p, TEXT, TIME, R = it["p"], dc.TEXT, dc.TIME, dc.R
    when = "since %s (%s)" % started(p.created, now) if p.created else "start unknown"
    names = [q.name for q in it["sub"][1:] if q.name and q.name not in HIDE]
    head = "%s %d" % (p.name or "?", p.pid)
    if names:
        head += " → " + ", ".join(sorted(set(names)))
    out = ["%s   %s%s" % (TEXT, dc.cut(head + " · " + when, WIDTH - 3), R)]
    if session_label:
        colour, text = session_label
        out.append("%s      %s%s" % (colour, dc.cut(text, WIDTH - 6), R))
    hits = it.get("hits") or []
    if hits:
        c = hits[0]
        if c["who"] == "you":
            why = "typed by you with !"
        elif c["who"] == "dot":
            why = 'typed by you: "%s"' % _one_line(mask(c["why"]))
        else:
            why = '"%s"' % _one_line(mask(c["why"])) if c["why"] else "(no description)"
        if len(hits) > 1:
            why += "  (+%d other call%s matched)" % (len(hits) - 1, "s" if len(hits) > 2 else "")
        out.append("%s      %s%s" % (TEXT, dc.cut(why, WIDTH - 6), R))
    elif it.get("with"):
        out.append("%s      started with the session (plugin or MCP server)%s" % (TIME, R))
    cmd = _one_line(mask(p.cmd))
    if cmd:
        out.append("%s      %s%s" % (TIME, dc.cut(cmd, WIDTH - 6), R))
    return out


def _label(sid, title, cwd):
    folder = os.path.basename((cwd or "").rstrip("/\\")) or "?"
    return '"%s" · %s' % (title or sid[:8], folder)


def render(model, secs, checked, every=False):
    """`every` (":all") unfolds what the footer otherwise only counts: the live
    sessions with nothing under them, and the outlived processes no call matched."""
    ACT, TITLE, TIME, R = dc.ACT, dc.TITLE, dc.TIME, dc.R
    now, live = model["now"], model["live"]
    out = ["%s.proc%s%s %s— processes Claude sessions started · %d checked in %.1fs%s"
           % (ACT, " " + ALL if every else "", R, TIME, checked, secs, R)]
    busy = [s for s in model["sessions"] if s["items"] or s["this"]]
    shown = model["sessions"] if every else busy
    quiet = len(model["sessions"]) - len(shown)
    if shown:
        out.append("%sRunning under a live session%s" % (TITLE, R))
    # A live session is green with its dot, as in .sess and .proj; a closed one
    # is plain text.
    for s in shown:
        tag = " (this session)" if s["this"] else ""
        out.append("%s● %s%s" % (dc.OK, dc.cut(_label(s["sid"], s["title"], s["cwd"]) + tag,
                                               WIDTH - 2), R))
        if not s["items"]:
            out.append("%s   nothing else runs under it%s" % (TIME, R))
        for it in s["items"]:
            out.extend(_proc_lines(it, now))
    if model["orphans"]:
        out.append("%sOutlived the call that started them%s" % (TITLE, R))
    for o in model["orphans"]:
        c = o["hits"][0]
        if c["sid"] in live:
            # The live name, as the session's own header shows it (.sess does
            # the same): the transcript's title can lag a rename.
            r = live[c["sid"]]
            where = (dc.OK, "● session %s" % _label(c["sid"], r.get("name") or c["title"],
                                                    r.get("cwd") or c["cwd"]))
        else:
            where = (dc.TEXT, "session %s (closed)" % _label(c["sid"], c["title"], c["cwd"]))
        out.extend(_proc_lines(o, now, where))
    if every and model["rest"]:
        out.append("%sOutlived their launcher, no call matched%s" % (TITLE, R))
        for o in model["rest"]:
            out.extend(_proc_lines(o, now))
    foot = []
    if quiet:
        foot.append("%d other live session%s: nothing running under them"
                    % (quiet, "" if quiet == 1 else "s"))
    if model["unmatched"] and not every:
        foot.append("%d other process%s outlived their launcher; none matched a session's "
                    "command" % (model["unmatched"], "" if model["unmatched"] == 1 else "es"))
    if foot:
        foot.append("%s.proc %s%s lists them" % (ACT, ALL, TIME))
    if every and model["unread"]:
        foot.append("%d more with no readable command line (SYSTEM, another account or "
                    "elevated): not listed" % model["unread"])
    if model["capped"]:
        foot.append("%stranscript scan stopped at %d MB: older calls not checked%s"
                    % (dc.RED, SCAN_BUDGET >> 20, TIME))
    if not busy and not model["orphans"]:
        foot.insert(0, "no process started by a Claude session is running")
    legend = ["under a session = still its child",
              "outlived = matched to the call by start time and program name",
              "never a verdict on safety"]
    out.extend("%s%s%s" % (TIME, ln, R) for ln in dc.fit_parts(foot) + dc.fit_parts(legend))
    return out


def usage_lines():
    ACT, TEXT, TIME, R = dc.ACT, dc.TEXT, dc.TIME, dc.R
    return ["%s.proc%s %s— usage: .proc [%s]   (..proc = in context)%s"
            % (ACT, R, TIME, ALL, R),
            "%s   Lists what runs under each live Claude session, and what outlived the%s"
            % (TEXT, R),
            "%s   call that started it, matched to that call by start time and program name,%s"
            % (TEXT, R),
            "%s   with the reason the session gave. Reads names, start times and command%s"
            % (TEXT, R),
            "%s   lines, as Task Manager does; never another process's environment.%s"
            % (TEXT, R),
            "%s   %s%s %salso lists the sessions with nothing under them, and what no call%s"
            % (TEXT, ACT + ALL, R, TEXT, R),
            "%s   explains — except processes of another account, whose command line%s"
            % (TEXT, R),
            "%s   is not readable.%s" % (TEXT, R)]


def handle(data, tail, to_context):
    every = tail.lower() == ALL
    bad = bool(tail) and not every and not dc.is_help(tail)
    if tail and not every:
        out = usage_lines()
    else:
        t = time.time()
        procs, own, err = snapshot()
        if err:
            out = ["%s.proc%s %s— %s%s" % (dc.ACT, dc.R, dc.TIME, err, dc.R)]
        else:
            model = collect(procs, own, dot_sess.registry())
            out = render(model, time.time() - t, len(procs), every)
    if to_context:
        dc.emit_context_block(out, ".proc", noisy=bad)
    else:
        dc.emit_block(out, noisy=bad)
