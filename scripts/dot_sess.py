#!/usr/bin/env python3
""".sess — every Claude session on THIS machine: list, search, open one.

".hist" answers "what did I type here"; ".sess" answers "where else have I been,
what is still open, and how do I get back in". Its transcripts are the same
JSONLs ".hist" reads, one per session under ~/.claude/projects/<slug>/.

GRAMMAR: four namespaces that cannot collide, so any option lands in one of them:

    digits      position    .sess 7        7th most recent session
                            .sess 7 3      its 3rd prompt, in full
    #hex        identity    .sess #ad2     session-id prefix, partial, stable
    :word       scope       :50 :* :here :live :full
    !word       action      .sess !r 7 12  reopen those sessions in new windows
                            .sess !f       no session named = the one you are in
                            .sess :30 !r   no session named = the ○ rows in that
                                           scope, newest first, at most MAX_SPAWN
                !word       .sess !sonnet  a model: fork here on it
                model       .sess !r !opus 3   only !r resumes; a model alone forks
    anything    content     .sess starmap scan   AND, case-insensitive

"!" is the SIDE-EFFECT SIGIL (dot_registry.py): three namespaces narrow a view,
one acts, and an invocation with no "!" cannot change anything. "-" is left
unspent: it reads as a getopt switch. WITH AN ACTION PRESENT, EVERY DIGIT IS A
SESSION: ".sess !r 7 12 40" is three sessions, not a session and two prompt
numbers; ".sess 7 3" (no action) is a session and its prompt.

Batching is a CORRECTNESS requirement, not a convenience. Rows are ordered by
transcript mtime and a resumed session appends to its own transcript, so the
first window to come up jumps to rank 1 and every other index shifts under you.
Resolving all of them against one snapshot is the only way ".sess !r 7 12" can
mean what it said — which is also why indices resolve against the LAST LISTING
(index_read) rather than against a fresh scan.

":N is DEPTH, not a result count" — ":5" considers the 5 most recent sessions,
so a filtered list comes out sparse (1, 3, 7, 40) and the index keeps meaning
the same session in every view. It is also the only reading that bounds the
work: ".sess 5" can never cost more than ".sess :100".

NO SIDE EFFECTS besides the listing index (index_write) and the windows an action
opens: it caches nothing and never copies prompt text to a second location; the
window (:N) is what bounds the cost of ":*".
"""
import calendar
import json
import os
import re
import time

import dot_common as dc
import paths
import sess_launch

WIDTH = dc.WIDTH
DEFAULT_WINDOW = 50       # depth; the footer always says which window ran
PROJECTS = os.path.join(paths.CLAUDE_DIR, "projects")
# The CLIENT's own live-session registry (undocumented, read-only): one file per
# running session, carrying its id, cwd, name and message socket. Same posture as
# the usage endpoint in claude_limits.py — best effort, never trusted blindly.
REGISTRY = os.path.join(paths.CLAUDE_DIR, "sessions")
SEEN_LIVE = 5 * 60        # ◐ claudette rendered a status line that recently
SEEN_WARM = 60 * 60       # ·
HEAD_BYTES = 1 << 16      # cwd/version/branch/first-stamp live in the first records
TAIL_BYTES = 200 << 10    # title/last-prompt are rewritten near the end
CHUNK = 1 << 22           # search prefilter reads big transcripts in slices
BUDGET = 8.0              # seconds a search may spend before it reports partial
HITS = 2                  # matching prompts shown per session

MAX_SPAWN = 20            # windows one invocation may open, typed or computed
SPAWN_GAP = 0.5           # s between two windows, so the terminal is not flooded at once
ACTIONS = {"r": "resume", "resume": "resume", "f": "fork", "fork": "fork"}
# REOPEN WHAT WAS LEFT OPEN: ".sess !r" naming nothing reopens the sessions nobody
# closed. Everything is read from the transcripts themselves - no side file.
#
# A session the user closed on purpose leaves a trace in one of two places.
# Clients up to ~2.1.273 file "/exit" in the transcript itself
# (<command-name>/exit</command-name>); 2.1.322 files no close there at all.
# Every client keeps ~/.claude/history.jsonl, its own log of what was TYPED in the
# terminal, with the session id: the close is there (read-only here). Prompts sent
# from the phone or the desktop app (promptSource "queued") never reach
# history.jsonl, hence /claudette:close, which a skill command files in the
# transcript wherever it was typed. Nothing else leaves a trace (a reboot, a
# crash, Ctrl+C, the window's X), so every terminal session with a prompt and no
# close is ○, "left open". The bound is the scope: the listing's depth (:50 by
# default, :N, :*, :here) and MAX_SPAWN (CLAUDETTE_DESIGN.md "Reopening after a
# reboot").
# /clear, its alias /new, and /resume leave the session on purpose too: the
# console moves to another session id, and history.jsonl files the command under
# the OLD one. A /resume picker cancelled is no close: the prompts typed after it
# become the last history entry.
CLOSED_BY = ("/exit", "/quit", "/claudette:close", "/claudette:terminate",  # terminate: old name of close
             "/clear", "/new", "/resume")
# .close is blocked by the hook, so its trace is the client's record of a blocked
# prompt (_blocked_close), matched whole: ".close x" closes nothing.
DOT_CLOSE = ".close"
CLOSED_WORDS = ("exit", "quit")   # typed bare, when the client keeps them as text
HISTORY = os.path.join(os.path.expanduser("~"), ".claude", "history.jsonl")
REOPEN_ENTRY = ("cli",)           # terminal sessions only: never sdk-cli jobs
# THE MODELS, SHARED WITH ".proj": one table so the two commands do not drift.
# ".proj" adds "new"/"n" on top, its spelling for "pass no --model at all"; here
# that is the default.
#
# A MODEL IS A "!" WORD: it never narrows a view and only appears beside an
# action, so an invocation with no "!" still cannot change anything. A bare word
# (".sess !f sonnet") would make content sometimes mean a model.
#
# Dict order is the display order.
MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}
MODEL_WORDS = tuple(MODELS)
MODEL_OK = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")   # what may reach a command line
# What the last listing showed, so an index keeps meaning what you READ. One file
# per session: a listing in another console never resolves an index here
# (CLAUDETTE_LOGS.md). Boot-stamped because --resume keeps the session id, so a
# session resumed after a reboot would find its own stale map.
INDEX_DIR = paths.log("sess_index")
INDEX_KEEP = 50
BOOT_TOL = 90             # s; "now - uptime" drifts a little between two reads

# The size column is a FIXED number field with the unit printed beside it, so "MB"
# lands on the same column in every row and the decimal points line up (padding
# the number+unit pair would not). COL_SIZE holds "1234.5", so the unit stays put
# into the gigabytes. The row is budgeted to exactly WIDTH, so the two characters
# this costs come out of the text column beside it: COL_TITLE here, COL_PATH in
# ".proj".
COL_PROJ, COL_TITLE, COL_SIZE = 16, 45, 6
SIZE_UNIT = "MB"
ACT, OK, TITLE, RED, TIME, TEXT, R = (dc.ACT, dc.OK, dc.TITLE, dc.RED,
                                      dc.TIME, dc.TEXT, dc.R)
USAGE = [
    "%s.sess%s %s— usage:%s" % (ACT, R, TIME, R),
    "   %s.sess%s            %srecent sessions (window :%d)%s" % (ACT, R, TIME, DEFAULT_WINDOW, R),
    "   %s.sess N%s  %s.sess #ad2%s   %sone session: details, prompts, how to resume%s"
    % (ACT, R, ACT, R, TIME, R),
    "   %s.sess N M%s          %sthat session's prompt M, in full%s" % (ACT, R, TIME, R),
    "   %s.sess starmap scan%s  %ssearch prompts+titles (every word must match)%s"
    % (ACT, R, TIME, R),
    "   %s.sess !r 7 12%s        %sreopen those sessions, one new window each "
    "(%s!f%s %s= fork instead)%s" % (ACT, R, TIME, R + ACT, R + TIME, R, R),
    "   %s.sess !f%s             %sno session named — fork the one you are in%s"
    % (ACT, R, TIME, R),
    "   %s.sess :30 !r%s         %sno session named — reopen the %s○%s %srows "
    "(never closed with exit)%s" % (ACT, R, TIME, R + RED, R, TIME, R),
    # No model list here: the line is budgeted to WIDTH and the unknown-action
    # error already enumerates them.
    "   %s.sess !sonnet N%s      %sfork on a named model — only %s!r%s %sresumes%s"
    % (ACT, R, TIME, R + ACT, R + TIME, "", R),
    "   %s:50 :*%s %sdepth%s   %s:here%s %sthis project%s   %s:live%s %sopen now%s   "
    "%s:full%s %salso search output%s" % (ACT, R, TIME, R, ACT, R, TIME, R,
                                          ACT, R, TIME, R, ACT, R, TIME, R),
    '   %s"42"%s %squoted = search a number; a bare number is always an index%s'
    % (ACT, R, TIME, R),
    "   %s!%s %sis the only thing here with a side effect; without one nothing "
    "changes%s" % (ACT, R, TIME, R),
]


# ----------------------------------------------------------------- arguments

_TOK = re.compile(r'"([^"]*)"|(\S+)')


class Args(object):
    __slots__ = ("window", "here", "live", "full", "idx", "sid", "pno", "terms", "err",
                 "action", "model", "idxs", "sids")

    def __init__(self):
        self.window, self.here, self.live, self.full = DEFAULT_WINDOW, False, False, False
        self.idx = self.sid = self.pno = None
        self.terms, self.err = [], ""
        self.action, self.model, self.idxs, self.sids = None, None, [], []


def parse(tail):
    """Tail -> Args. Every rejection is a usage answer, never a pass-through."""
    a = Args()
    win_seen = False
    toks = []
    for m in _TOK.finditer(tail):
        quoted, raw = m.group(1), m.group(2)
        toks.append((quoted is not None, raw if raw is not None else quoted))

    # The action is read FIRST because it changes what a digit means, and it may
    # be written either side of its sessions (".sess !r 7" and ".sess 7 !r").
    # "!" holds two kinds of word: the verb, and a model (see MODELS).
    for quoted, tok in toks:
        if quoted or not tok.startswith("!"):
            continue
        word = tok[1:].lower()
        if not word:
            # A standalone trailing "!" reaches here (dot_registry._BANG).
            a.err = "! on its own does nothing here — !r resumes, !f forks"
            return a
        if word in ACTIONS:
            if a.action and a.action != ACTIONS[word]:
                a.err = "one action at a time"
                return a
            a.action = ACTIONS[word]
            continue
        if word in MODELS:
            if a.model and a.model != MODELS[word]:
                a.err = "one model at a time"
                return a
            a.model = MODELS[word]
            continue
        if word in ("new", "n"):
            # The one wrong word a habit produces: ".proj !new" means "a session
            # with no --model", which here is just ".sess !f".
            a.err = ("!new belongs to %s.proj%s %s— %s.sess !f%s %salready starts "
                     "from this session" % (ACT, R + TIME, "", ACT, R + TIME, ""))
            return a
        a.err = ("unknown action !%s — !r (resume), !f (fork), or a model: %s"
                 % (tok[1:], " ".join("!" + k for k in MODEL_WORDS)))
        return a

    # A MODEL WITH NO VERB FORKS: only an explicit "!r" resumes. Set before the
    # digit loop below, which reads a.action to decide that every number is a session.
    if a.model and not a.action:
        a.action = "fork"

    for quoted, tok in toks:
        if quoted:                      # a quoted token is content, always
            a.terms.append(tok.lower())
            continue
        if tok.startswith("!"):
            continue                    # consumed above
        if tok.startswith(":"):
            opt = tok[1:].lower()
            if opt in ("*", "all"):
                opt_win = 0
            elif opt.isdigit() and int(opt) > 0:
                opt_win = int(opt)
            elif opt in ("here", "live", "full"):
                if getattr(a, opt):
                    a.err = "repeated :%s" % opt
                    return a
                setattr(a, opt, True)
                continue
            else:
                a.err = "unknown option :%s" % opt
                return a
            if win_seen:
                a.err = "two windows given — pick one of :N or :*"
                return a
            a.window, win_seen = opt_win, True
            continue
        if tok.startswith("#"):
            h = tok[1:].lower()
            if not h or not re.fullmatch(r"[0-9a-f-]+", h):
                a.err = "#%s is not a session id" % tok[1:]
                return a
            if a.action:
                a.sids.append(h)
                continue
            if a.sid or a.idx:
                a.err = "one session at a time"
                return a
            a.sid = h
            continue
        if tok.isdigit():
            # digits = position. The second one is the prompt inside the session
            # already selected, which is why ".sess 7 3" needs no extra syntax —
            # unless an action is present, which turns them all into sessions.
            if a.action:
                if int(tok) < 1:
                    a.err = "session 0 does not exist — the list starts at 1"
                    return a
                a.idxs.append(int(tok))
                continue
            if a.idx is None and a.sid is None:
                if int(tok) < 1:
                    a.err = "session 0 does not exist — the list starts at 1"
                    return a
                a.idx = int(tok)
            elif a.pno is None:
                if int(tok) < 1:
                    a.err = "prompt 0 does not exist — prompts start at 1"
                    return a
                a.pno = int(tok)
            else:
                a.err = "too many numbers"
                return a
            continue
        a.terms.append(tok.lower())
    if a.action:
        n = len(a.idxs) + len(a.sids)
        if a.terms:
            a.err = "an action and a search at once — drop one"
        elif a.full or ((win_seen or a.here or a.live)
                        and (n or a.action != "resume")):
            # A scope picks rows; numbers already did. Only ".sess :30 !r"
            # (no session named) reads one: the ○ rows inside it.
            a.err = "scope options do not apply to an action"
        # NO session named is not an error. For a fork it means the one you are
        # typing in (_resolve() puts it there); for a resume it reopens the ○ rows
        # of its scope (reopen()).
        elif n > MAX_SPAWN:
            a.err = "%d sessions — at most %d windows at once" % (n, MAX_SPAWN)
        elif len(set(a.idxs)) != len(a.idxs) or len(set(a.sids)) != len(a.sids):
            a.err = "the same session named twice"
        return a
    if (a.idx or a.sid) and a.terms:
        a.err = "a session and a search at once — drop one"
    elif a.pno and not (a.idx or a.sid):
        a.err = "a prompt number needs a session before it"
    elif (a.idx or a.sid) and (win_seen or a.here or a.live):
        # An explicit session is resolved against EVERY session, window or not,
        # so a scope token beside it would silently mean nothing.
        a.err = "scope options do not apply to a single session"
    elif a.full and not a.terms:
        a.err = ":full needs something to search for"
    return a


# ------------------------------------------------------------------ the data

def slug_of(path):
    """The client's project-folder encoding: every non-alphanumeric becomes "-".

    It is one-way: "C--Work-a-b" cannot be decoded back, so every cwd shown comes
    from the transcript's own records, never from the folder name. Compare
    case-INSENSITIVELY: slugs can differ from their cwd only in case, and one
    folder can hold both "C:\\Work\\x" and "c:\\Work\\x".
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_key(data):
    """This session's project folder name — the client's own key, not ours.

    THE STARTUP FOLDER IS THE PROJECT: the directory claude was launched in is
    the sandbox root it needs permission to step outside of, AND the key its
    transcripts are filed under. A "cd" changes neither, so the live cwd says
    nothing about which project a session is in. (A /clear returns the cwd to the
    startup folder, so every transcript's FIRST cwd is its project root.)

    The transcript lives IN that folder, so its parent directory is the answer
    with no inference: no encoding to redo, no case to guess. slug_of(cwd) is the
    fallback for a hook handed no transcript path, right only while the cwd is
    still the startup folder.
    """
    tpath = data.get("transcript_path") or ""
    if tpath:
        return os.path.basename(os.path.dirname(tpath)).lower()
    return slug_of(data.get("cwd") or os.getcwd()).lower()


def scan():
    """Every transcript, newest first. stat() only — no file is opened here.

    Sorted by mtime = last activity, which is also what the day dividers group by:
    a session resumed today is today's work, not the day it was born.
    """
    rows = []
    try:
        for d in os.scandir(PROJECTS):
            if not d.is_dir():
                continue
            for f in os.scandir(d.path):
                if not f.name.endswith(".jsonl"):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                rows.append({"path": f.path, "sid": f.name[:-6], "slug": d.name,
                             "mtime": st.st_mtime, "size": st.st_size})
    except OSError:
        return []
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def meta(rec):
    """Head+tail read of one transcript: ~5 ms, whatever its size.

    Everything a row shows lives in the first records (cwd, version, branch) or is
    rewritten near the end (ai-title, custom-title, last-prompt). A prompt COUNT
    would need the whole file, so it belongs to the detail view, not the list.
    """
    out = {"cwd": "", "branch": "", "version": "", "entry": "", "title": "",
           "last": "", "started": "", "closed": ""}
    try:
        with open(rec["path"], "rb") as f:
            head = f.read(HEAD_BYTES)
            if rec["size"] > HEAD_BYTES:
                f.seek(max(HEAD_BYTES, rec["size"] - TAIL_BYTES))
                tail = f.read()
            else:
                tail = b""
    except OSError:
        return out
    for raw in head.decode("utf-8", "replace").splitlines():
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("cwd") and not out["cwd"]:
            # The FIRST cwd only: a session can chdir mid-flight, but the project
            # folder — and therefore what --resume needs — was fixed at startup.
            out.update(cwd=d["cwd"], branch=d.get("gitBranch") or "",
                       version=d.get("version") or "", entry=d.get("entrypoint") or "")
        if d.get("timestamp") and not out["started"]:
            out["started"] = d["timestamp"]
        if out["cwd"] and out["started"]:
            break
    for chunk in (head, tail):
        for raw in chunk.decode("utf-8", "replace").splitlines():
            if '"ai-title"' not in raw and '"custom-title"' not in raw \
                    and '"last-prompt"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if t == "custom-title":
                out["title"] = d.get("customTitle") or out["title"]
            elif t == "ai-title":
                out["title"] = out["title"] or d.get("aiTitle") or ""
            elif t == "last-prompt":
                out["last"] = d.get("lastPrompt") or out["last"]
    # The END of the file decides how the session was left: the tail when there
    # is one, otherwise the whole file is in the head.
    cmd, ts = _last_typed(tail if tail else head)
    out["closed"] = _closing(cmd) or history_close(rec["sid"], ts)
    return out


def _typed(d):
    """The text of a record the USER typed, or None for anything else: tool
    results, injected skill bodies (isMeta), local-command echoes and every
    bookkeeping record the client keeps appending to an open session."""
    if d.get("type") != "user" or d.get("isMeta"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        if c.startswith(("<local-command-stdout>", "<local-command-caveat>",
                         "<local-command-stderr>")):
            return None
        return c
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return None


def _last_typed(chunk):
    """(text, timestamp) of the LAST record the user typed in this chunk.

    Parsed records, never a substring: this very feature's transcript holds the
    string "<command-name>/exit" in tool output and replies. Bookkeeping records
    after it change nothing.
    """
    last, ts = None, ""
    for raw in chunk.decode("utf-8", "replace").splitlines():
        if '"user"' not in raw and DOT_CLOSE not in raw:
            continue                    # cheap gate: most tail lines are not
        try:
            d = json.loads(raw)
        except Exception:
            continue                    # the tail's first line is usually cut
        if isinstance(d, dict):
            t = _typed(d)
            if t is None and _blocked_close(d):
                t = DOT_CLOSE
            if t is not None:
                last, ts = t, d.get("timestamp") or ""
    return last, ts


def _blocked_close(d):
    """A ".close" the hook blocked: the client files it as a system entry ending
    "Original prompt: .close" (typed while idle) or a queue-operation (typed
    mid-turn) — never as a user record, and never in history.jsonl (0 blocked
    prompts there). Only .close counts as typed here: other
    blocked dot-commands change nothing about how a session was left."""
    c = d.get("content")
    if not isinstance(c, str):
        return False
    if d.get("type") == "system":
        return c.rstrip().endswith("Original prompt: " + DOT_CLOSE)
    return d.get("type") == "queue-operation" and c.strip() == DOT_CLOSE


def _closing(text):
    """The closing command a typed text is, normalised to "/exit", or ""."""
    if not text:
        return ""
    s = text.strip()
    if s.lower() in CLOSED_WORDS:
        return "/" + s.lower()
    if s == DOT_CLOSE:
        return DOT_CLOSE
    for cmd in CLOSED_BY:
        if s.startswith("<command-name>%s</command-name>" % cmd):
            return cmd
        if s.split(None, 1)[0:1] == [cmd]:       # history.jsonl keeps it as typed
            return cmd
    return ""


def closed_by(chunk):
    """The closing command if the LAST thing typed in the transcript was one,
    else "". Only the last typed record counts, so a session that was exited,
    resumed and then interrupted is not taken for closed."""
    return _closing(_last_typed(chunk)[0])


_HIST = {"key": None, "last": {}}


def _history():
    """{sid: (display, epoch ms)} of the LAST entry per session in the client's
    history.jsonl, read once per file version."""
    try:
        st = os.stat(HISTORY)
    except OSError:
        return {}
    key = (HISTORY, st.st_mtime, st.st_size)
    if _HIST["key"] != key:
        last = {}
        try:
            with open(HISTORY, "rb") as f:
                for raw in f:
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(d, dict) and d.get("sessionId"):
                        last[d["sessionId"]] = (d.get("display") or "",
                                                d.get("timestamp") or 0)
        except OSError:
            return {}
        _HIST.update(key=key, last=last)
    return _HIST["last"]


def history_close(sid, typed_ts=""):
    """The close the user typed last in the terminal for this session, from
    history.jsonl, or "". A prompt the transcript holds AFTER that close means
    the session was resumed and used since (from the phone, say, which history
    never sees): then it is not closed. Missing file or session: "" — a session
    reopened for nothing costs one window, a session lost from view costs more.
    """
    got = _history().get(sid)
    if not got:
        return ""
    cmd = _closing(got[0])
    if not cmd:
        return ""
    if typed_ts:
        try:
            t = calendar.timegm(time.strptime(typed_ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            t = None
        if t is not None and t > got[1] / 1000.0 + 2:
            return ""
    return cmd


def pending(rec, m):
    """Left open: a terminal session with something typed in it and no closing
    command at its end. Liveness is the caller's: a live session is ●, not ○."""
    if m.get("entry") not in REOPEN_ENTRY or m.get("closed"):
        return False
    return bool(m.get("last") or m.get("title"))    # nothing typed: nothing to reopen


def index_write(cur, pairs, d=None):
    """Record what this listing SHOWED: {rank: key}, for this session only.

    A blocked dot-command reaches the transcript only as rendered text, so this
    file is the only readable record of what was printed; a fresh scan is what
    moves. Holds ranks and keys (session ids here, project slugs for ".proj"): no
    titles, no paths, no prompt text. `d` is the directory, one per COMMAND, so
    a ".proj" listing never resolves a ".sess !r" index.
    """
    d = INDEX_DIR if d is None else d
    if not cur or not pairs:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "%s.tmp" % cur)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "boot": sess_launch.boot_time(),
                       "map": {str(n): sid for n, sid in pairs}}, f)
        os.replace(tmp, os.path.join(d, "%s.json" % cur))
        _index_prune(d)
    except OSError:
        pass                    # a listing that cannot log still lists


def _index_prune(d=None):
    """Drop maps from earlier boots, then cap the folder. Self-cleaning: a map
    that survives a reboot is unusable by construction, so it is also garbage."""
    d = INDEX_DIR if d is None else d
    boot = sess_launch.boot_time()
    try:
        files = [os.path.join(d, n) for n in os.listdir(d)
                 if n.endswith(".json")]
    except OSError:
        return
    keep = []
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                b = json.load(f).get("boot")
            if boot is not None and b is not None and abs(b - boot) > BOOT_TOL:
                os.remove(p)
                continue
            keep.append((os.path.getmtime(p), p))
        except Exception:
            continue
    for _t, p in sorted(keep, reverse=True)[INDEX_KEEP:]:
        try:
            os.remove(p)
        except OSError:
            pass


def index_read(cur, d=None):
    """(map, "") for this session's last listing, or (None, why it is unusable)."""
    d = INDEX_DIR if d is None else d
    try:
        with open(os.path.join(d, "%s.json" % cur), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None, "no listing in this session yet"
    boot, was = sess_launch.boot_time(), d.get("boot")
    if boot is not None and was is not None and abs(was - boot) > BOOT_TOL:
        # --resume keeps the session id, so a session reopened after a reboot
        # arrives here holding its own pre-reboot map. Refuse rather than resolve.
        return None, "that listing is from before the last reboot"
    if not d.get("map"):
        return None, "the last listing recorded nothing"
    return d["map"], ""


def registry():
    """{session_id: record} for sessions the client says are RUNNING.

    Liveness is not a guess here: each record names its own message socket, and
    that socket exists exactly as long as the process does — a crashed session
    leaves the json behind but never the pipe.
    """
    live = {}
    try:
        names = os.listdir(REGISTRY)
    except OSError:
        return live
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(REGISTRY, n), encoding="utf-8") as f:
                d = json.load(f)
            sock = d.get("messagingSocketPath") or ""
            if d.get("sessionId") and sock and os.path.exists(sock):
                live[d["sessionId"]] = d
        except Exception:
            continue
    return live


def seen(sid):
    """Age in seconds of claudette's last status-line render for that session."""
    try:
        with open(os.path.join(paths.USAGE_SESSIONS, "%s.json" % sid),
                  encoding="utf-8") as f:
            ts = int(json.load(f).get("ts") or 0)
        return int(time.time()) - ts if ts else None
    except Exception:
        return None


def mark(sid, live, cur, rec=None, m=None):
    """● open now (green) · ○ left open, never closed with exit (red) · ✓ closed
    with exit (dim) · ◐ · seen recently (dim) · blank.

    ○ is the empty ●: it was open, it is not, and nobody closed it — the rows
    ".sess !r" alone would reopen. One cell wide like ● (⚠ renders as two in
    many terminals and would push the row past WIDTH).

    The glyph carries its own colour: GREEN for "open now", dim for the
    seen-recently fallbacks. Otherwise it would inherit the yellow the bare rank
    opened, and yellow is reserved for what you can TYPE (dot_common).
    """
    if sid == cur or sid in live:
        return "%s●%s" % (OK, R)
    if rec is not None and m is not None:
        if m.get("closed"):
            return "%s✓%s" % (TIME, R)
        if pending(rec, m):
            return "%s○%s" % (RED, R)
    age = seen(sid)
    if age is None:
        return " "
    if age < SEEN_LIVE:
        return "%s◐%s" % (TIME, R)
    return "%s·%s" % (TIME, R) if age < SEEN_WARM else " "


# --------------------------------------------------------------- the renders

def size_n(n):
    """The NUMBER of a size, no unit — one decimal, no leading zero: ".6", "31.0".

    One unit, no auto-scaling: the number of digits IS the magnitude ("894K"
    would render wider than "37M"). Dropping the leading zero keeps ".0", ".1",
    ".6" visibly shorter than "1.4", so a nearly-empty session reads as such.

    "MB", not "M", because claudette also prints TOKEN counts ("315k",
    dot_common.tokens). These are bytes on disk (the transcript's st_size), never
    tokens: a token total needs the whole file read.

    Binary MB (1<<20), what Windows Explorer reports for the same file. A
    gigabyte transcript renders "2048.0MB"; COL_SIZE reserves room for it.
    """
    s = "%.1f" % (n / float(1 << 20))
    return s[1:] if s.startswith("0.") else s


def size_h(n):
    """Number + unit, UNPADDED — for prose (".sess N" header), where the value sits
    in a sentence and has no column to line up with. A listing wants size_cell."""
    return size_n(n) + SIZE_UNIT


def size_cell(n, colour):
    """The size COLUMN: number right-aligned in COL_SIZE, worn in the row's own
    colour, with a dim unit beside it.

    Two colours: the number takes the colour of the folder or title it measures,
    while "MB" repeats on every row and is dim, leaving the digits as the only ink
    that varies.
    """
    return "%s%*s%s%s%s%s" % (colour, COL_SIZE, size_n(n), R, TIME, SIZE_UNIT, R)


def cell(s, w):
    """A column of at most w display cells, "…" inside (dc.cut)."""
    return dc.cut(s, w)


UNWRITTEN = "no transcript yet"     # short: the label column cuts at ~38


def unwritten(data, cur, here_slug):
    """A row for THIS session when it has no transcript yet. On a session's first
    prompt (a new one, or right after /clear) the prompt hook runs before the
    client creates the file, so scan() cannot see it. Built from the hook data:
    the path the client will write (every reader treats a missing file as empty),
    the project folder so ":here" still matches, now as its last activity, size
    0. Only when the hook names a transcript path that does not exist yet: a
    session whose file exists elsewhere is not this case."""
    return {"path": data.get("transcript_path") or "", "sid": cur,
            "slug": here_slug, "mtime": time.time(), "size": 0, "unwritten": True,
            "cwd": data.get("cwd") or ""}     # shown until the file says it


def label(rec, m, live):
    """What a row calls the session: the name YOU can message it by when it is
    live, the title the client derived otherwise, the last prompt if neither."""
    if rec.get("unwritten"):
        r = live.get(rec["sid"]) or {}
        return "(%s)%s" % (UNWRITTEN, (" " + r["name"]) if r.get("name") else "")
    r = live.get(rec["sid"])
    if r and r.get("name"):
        return r["name"]
    return m["title"] or dc.flat(m["last"]) or "(untitled)"


def proj_of(m, rec):
    p = m["cwd"] or rec.get("cwd") or rec["slug"]
    return os.path.basename(p.rstrip("\\/")) or p


def span_mark(rec, m):
    """"+2d" when the session STARTED earlier than the day it is filed under —
    rows are grouped by last activity, so without this a session resumed today
    looks like it was born today."""
    o = dc.when(m["started"])
    if not o:
        return ""
    d = (time.localtime(rec["mtime"]).tm_yday - o.timetuple().tm_yday)
    days = (int(rec["mtime"]) - int(o.timestamp())) // 86400
    return " +%dd" % max(days, 1) if (days >= 1 or d >= 1) else ""


YOU = " ←you"


def you_cell(text, colour, w, you):
    """A text column of EXACTLY w cells, with "←you" INSIDE it, flush with the
    column's right edge, when you — shared with ".proj" so the mark sits the same
    way in both.

    Inside, because the row is budgeted to exactly WIDTH: appended after the last
    column, the mark would push its row 5 cells past it and wrap. The text column
    pays, on that row only. RIGHT-ALIGNED so it lands on the same column whatever
    the text. Padding is counted on the bare text: "%-*s" would count the mark's
    escape codes as cells.
    """
    mark = "%s%s%s" % (OK, YOU, R) if you else ""
    return "%s%s%s%s%s" % (colour, text, R,
                           " " * max(0, w - len(text) - (len(YOU) if you else 0)), mark)


def row(n, rec, m, live, cur, proj=True):
    """proj=False drops the project column and gives its width to the title —
    for a listing that is already ABOUT one project (".proj N"), where repeating
    the name on every row says nothing and costs 16 characters of title."""
    sp = span_mark(rec, m)
    you = rec["sid"] == cur
    wide = COL_TITLE + (0 if proj else COL_PROJ + 1)
    lab = cell(label(rec, m, live), wide - len(sp) - (len(YOU) if you else 0)) + sp
    # The RANK IS BARE (the ".hist" contract): the numbers you can type into the
    # next command are the block's own yellow, every other column is not. It works
    # only because the rank is the first thing on the line - bare text stops
    # inheriting after our first escape code (dot_common, ACT_MID).
    return "%3d %s %s%s%s  %s%s%s %s  %s%s%s" % (
        n, mark(rec["sid"], live, cur, rec, m),
        TIME, time.strftime("%H:%M", time.localtime(rec["mtime"])), R,
        TEXT, ("%-*s " % (COL_PROJ, cell(proj_of(m, rec), COL_PROJ))) if proj else "",
        you_cell(lab, TEXT, wide, you),
        size_cell(rec["size"], TEXT),
        TIME, rec["sid"][:8], R)


def listing(rows, live, cur, head, proj=True):
    out, last_day = [head], None
    for n, rec, m in rows:
        d = dc.day_of_epoch(rec["mtime"])
        if d != last_day:
            h = "   ══ %s " % d
            out.append("%s%s%s%s" % (TIME, h, "═" * max(0, WIDTH - len(h)), R))
            last_day = d
        out.append(row(n, rec, m, live, cur, proj))
    return out


CLEANUP_DEFAULT = 30      # Claude Code's own default when the key is absent
CLEANUP_FLOOR = 365       # below this, the horizon is worth saying out loud


def retention():
    """(days of history actually on disk, configured cleanup horizon).

    Both, because history far shallower than the horizon means either a young
    machine or one that was pruned earlier. An ABSENT key is not "unset", it is
    30: the client's own default, and the silent deletion this line exposes.
    """
    days = None
    try:
        oldest = min(os.path.getmtime(f.path)
                     for d in os.scandir(PROJECTS) if d.is_dir()
                     for f in os.scandir(d.path) if f.name.endswith(".jsonl"))
        days = int((time.time() - oldest) / 86400)
    except (OSError, ValueError):
        pass
    keep = CLEANUP_DEFAULT
    try:
        with open(os.path.join(paths.CLAUDE_DIR, "settings.json"), encoding="utf-8") as f:
            keep = int(json.load(f).get("cleanupPeriodDays", CLEANUP_DEFAULT))
    except Exception:
        pass
    return days, keep


def legend(a, shown, considered, total, machine, extra=""):
    """The one line that must never let a filtered, windowed list look complete:
    what was shown, out of what scope, out of how many sessions exist."""
    out = []
    scope = []
    if a.here:
        scope.append("this project")
    if a.live:
        scope.append("open now")
    win = "all" if not a.window else str(a.window)
    where = ("%s: %d of %d" % (", ".join(scope), total, machine)) if scope \
        else "%d session(s) on this machine" % machine
    depth, keep = retention()
    # The horizon rides the totals line; it turns red only when it is the thing
    # quietly costing you history.
    horizon = "%s%dd kept%s" % (RED if keep < CLEANUP_FLOOR else "", keep,
                                R + TIME if keep < CLEANUP_FLOOR else "")
    parts = (["%d shown" % shown, where] + (["%dd deep" % depth] if depth else [])
             + ["window :%s" % win] + ([extra] if extra else []) + [horizon])
    out += ["%s%s%s" % (TIME, ln, R) for ln in dc.fit_parts(parts)]
    if keep < CLEANUP_FLOOR:
        out.append("%s   transcripts older than %dd are deleted by Claude Code — "
                   "%s/claudette prefs%s %sraises it (claude_history_floor)%s"
                   % (TIME, keep, R + ACT, R + TIME, "", R))
    if a.window and considered < total:
        rest = " ".join(a.terms)
        out.append("%s   %d older session(s) outside the window — widen with%s %s.sess "
                   "%s:100%s %sor%s %s:*%s"
                   % (TIME, total - considered, R, ACT, (rest + " ") if rest else "",
                      R, TIME, R, ACT, R))
    return out


def resume_lines(rec, m, live, cur, n=None):
    """How to get back in — and the cases where you should not try.

    Resuming keeps the id, so the id below is the whole handle. What changes per
    case is whether resuming is the right move at all.

    Commands come FIRST, one per line, indented and bare, so the block can be
    pasted as-is; the sentence that explains them goes underneath.
    """
    sid, cwd = rec["sid"], m["cwd"]
    idx = n if n is not None else "N"
    if sid == cur:
        # RESUMING this one is meaningless; FORKING it is not.
        return [
            "     %sclaude --resume %s --fork-session%s" % (ACT, sid, R),
            "   %sThis is the session you are typing in — resuming it is a no-op.%s"
            % (TIME, R),
            "   %sThe line above branches a copy · %s.sess !f %s%s %sdoes it in a "
            "new window%s" % (TIME, R + ACT, idx, R + TIME, "", R)]
    r = live.get(sid)
    if r:
        # THREE short lines, not one long one: a line past the terminal width wraps
        # mid-escape-run and the continuation comes back in a colour nobody chose.
        # Every line fits WIDTH on its own.
        name = (" as \"%s\"" % dc.cut(r.get("name"), 44)) if r.get("name") else ""
        return [
            "     %sclaude --resume %s --fork-session%s" % (ACT, sid, R),
            "   %sOPEN right now%s%s" % (RED, name, R),
            "   %sResuming would fight that window — the line above branches a copy%s"
            % (TIME, R),
            "   %sinstead · %s.sess !f %s%s %sdoes the same in a new window%s"
            % (TIME, R + ACT, idx, R + TIME, "", R)]
    if cwd and not os.path.isdir(cwd):
        return [
            "   %scannot resume here: %s is gone%s"
            % (RED, dc.mid(cwd, WIDTH - len("   cannot resume here:  is gone")), R),
            "   %srunning --resume elsewhere files this session under another "
            "project —%s" % (TIME, R),
            "   %srecreate the path first%s" % (TIME, R)]
    return [
        "     %scd \"%s\"%s" % (ACT, cwd or "?", R),
        "     %sclaude --resume %s%s" % (ACT, sid, R),
        "   %sCopy the two lines above to resume · add %s--fork-session%s %sto the "
        "claude line to branch instead%s" % (TIME, R + ACT, R, TIME, R),
        "   %sor %s.sess !r %s%s %s— same thing in a new window, without the paste%s"
        % (TIME, R + ACT, idx, R + TIME, "", R)]


def _resolve(a, allrows, cur):
    """Args -> ([(shown_as, rank, rec), ...], error_lines).

    Indices come from the LAST LISTING, never from a fresh scan: what the user
    typed was read off a screen, and the scan has moved since. An id (#hex) needs
    no listing at all — identity does not shift — which is also the escape hatch
    when the map is missing.

    NAMING NOTHING MEANS THIS SESSION: ".sess !f" on its own is "branch a copy of
    what I am doing now" (see _blockers). Like "#hex" it needs no map: the session
    id is in hand. Shown as "here", since there may be no listing to number it
    against.
    """
    picked, errs = [], []
    if not a.idxs and not a.sids:
        rec = next((r for r in allrows if r["sid"] == cur), None) if cur else None
        if rec is None:
            return [], ["   %sthis session has no transcript to %s — name one by "
                        "number or %s#id%s"
                        % (RED, "fork" if a.action == "fork" else "resume",
                           R + ACT, R)]
        return [("here", rec)], []
    if a.idxs:
        imap, why = index_read(cur)
        if imap is None:
            return [], ["   %s%s — run %s.sess%s %sfirst, then %s.sess !%s %s…%s"
                        % (RED, why, R + ACT, R + RED, "", ACT,
                           "r" if a.action == "resume" else "f", R + RED, R)]
        by_sid = {r["sid"]: r for r in allrows}
        for n in a.idxs:
            sid = imap.get(str(n))
            if sid is None:
                shown = sorted(int(k) for k in imap)
                errs.append("   %s%d%s %swas not in that listing (it showed %d–%d)%s"
                            % (ACT, n, R, RED, shown[0], shown[-1], R))
            elif sid not in by_sid:
                errs.append("   %s%d%s %s(#%s) — its transcript is gone%s"
                            % (ACT, n, R, RED, sid[:8], R))
            else:
                picked.append(("%d" % n, by_sid[sid]))
    for h in a.sids:
        cand = [r for r in allrows if r["sid"].lower().startswith(h)]
        if not cand:
            errs.append("   %s#%s%s %s— no session id starts with that%s"
                        % (ACT, h, R, RED, R))
        elif len(cand) > 1:
            errs.append("   %s#%s%s %s— %d sessions match; add a character%s"
                        % (ACT, h, R, RED, len(cand), R))
        else:
            picked.append(("#%s" % h, cand[0]))
    return picked, errs


def _blockers(shown, rec, m, live, cur, fork):
    """Why this one must not be launched — the cases resume_lines describes, enforced.

    Forking is legal on a live session (that is the point of the fork); resuming
    it is not. THE CURRENT SESSION IS A LIVE SESSION: forking it is "branch a copy
    of what I am doing now"; only resuming it is a no-op.
    """
    sid = rec["sid"]
    if sid == cur and not fork:
        # No index in the advice: ".sess !f" with nothing names this session and
        # nothing else, so it is right whether or not a listing is on screen.
        return ("%sthis is the session you are typing in — %s.sess !f%s "
                "%sbranches a copy%s" % (RED, R + ACT, R + RED, "", R))
    r = live.get(sid)
    if r and not fork:
        return ("%sOPEN right now%s%s %s— use %s.sess !f %s%s %sto branch a copy%s"
                % (RED, (" as \"%s\"" % r.get("name")) if r.get("name") else "", R,
                   RED, R + ACT, shown, R + RED, "", R))
    if not m["cwd"]:
        return "%sno working directory recorded — resume by hand%s" % (RED, R)
    if not os.path.isdir(m["cwd"]):
        return ("%scwd is gone: %s — recreate it first, or --resume files this "
                "session under another project%s" % (RED, m["cwd"], R))
    return ""


def act(a, allrows, live, cur, scope=None):
    """".sess !r 7 12" — open each in its own new window. All or nothing.

    Validation runs over every target BEFORE anything spawns: a half-done batch
    leaves windows to sort out by hand, a refusal costs one retype.
    """
    fork = a.action == "fork"
    if not fork and not a.idxs and not a.sids:
        return reopen(a, allrows if scope is None else scope, live, cur)
    head = "%s.sess !%s%s" % (ACT, "f" if fork else "r", R)
    picked, errs = _resolve(a, allrows, cur)

    rows = []
    for shown, rec in picked:
        m = meta(rec)
        rows.append((shown, rec, m, _blockers(shown, rec, m, live, cur, fork)))
    errs += ["   %s%s%s  %s%s" % (ACT, shown, R, why, R)
             for shown, _rec, _m, why in rows if why]

    if errs:
        return ([("%s %s— nothing launched; %d problem(s):%s"
                  % (head, TIME, len(errs), R))] + errs
                + ["%s   nothing has moved — fix and retype%s" % (TIME, R)])
    return _launch(a, head, [(s, rec, m) for s, rec, m, _ in rows], live)


def reopen(a, rows, live, cur):
    """".sess !r" naming nothing — reopen the ○ rows of its scope, newest first,
    MAX_SPAWN at most.

    rows = the scope, already filtered and cut to its depth, newest first.
    NOT all-or-nothing, unlike a typed batch: nobody typed these, so one with a
    vanished folder is skipped and said, instead of holding back the others.
    The preview is the listing before it — its ○ rows, counted on its last
    lines — since a dot-command cannot ask; the echo names every one by id.
    """
    head = "%s.sess !r%s" % (ACT, R)
    found = []
    for rec in rows:
        if rec["sid"] == cur or rec["sid"] in live:
            continue
        m = meta(rec)
        if pending(rec, m):
            found.append((rec, m))
    if not found:
        return ["%s %s— nothing to reopen: none of these %d sessions was left "
                "open%s" % (head, TIME, len(rows), R),
                "%s   (wider: %s.sess :100 !r%s %sor%s %s:*%s%s · this session: "
                "%s.sess !f%s %sforks it)%s"
                % (TIME, R + ACT, R, TIME, R, ACT, R, TIME, R + ACT, R, TIME, R)]
    go, skipped = [], []
    for rec, m in found:
        why = _blockers("#" + rec["sid"][:8], rec, m, live, cur, False)
        if why:
            skipped.append(("#" + rec["sid"][:8], why))
        elif len(go) < MAX_SPAWN:
            go.append(("#" + rec["sid"][:8], rec, m))
    left = len(found) - len(skipped) - len(go)
    out = (_launch(a, head, go, live) if go
           else ["%s %s— nothing launched%s" % (head, TIME, R)])
    for shown, why in skipped:
        out.append("   %s%s%s  %sskipped: %s%s" % (ACT, shown, R, RED, why, R))
    if left:
        out.append("%s   %d older %s○%s %srow(s) left: at most %d windows at once — "
                   "run it again, or name them: %s.sess !r 7 12%s"
                   % (TIME, left, R + RED, R, TIME, MAX_SPAWN, R + ACT, R))
    return out


def _launch(a, head, rows, live):
    """Spawn one window per (shown, rec, meta) — every guard already passed."""
    fork = a.action == "fork"
    verb, did = ("fork", "forked") if fork else ("resume", "resumed")
    # The only interpolation into a command line, and MODEL_OK is why it is safe:
    # the value comes from MODELS, never from what was typed.
    assert a.model is None or MODEL_OK.match(a.model), a.model
    det = sess_launch.detect()
    plans = [(shown, rec, m,
              sess_launch.plan(m["cwd"], rec["sid"], fork, det, a.model))
             for shown, rec, m in rows]

    if not plans[0][3].ok:
        out = ["%s %s— %s; %s by hand:%s"
               % (head, TIME, plans[0][3].why, verb, R)]
        for shown, rec, m, p in plans:
            out.append("   %s%s%s %s· %s%s"
                       % (TITLE, dc.cut(label(rec, m, live), 44), R, TIME,
                          rec["sid"][:8], R))
            for ln in p.shown:
                out.append("     %s%s%s" % (ACT, ln, R))
        return out

    done, failed = [], []
    for i, (shown, rec, m, p) in enumerate(plans):
        if i:
            time.sleep(SPAWN_GAP)
        ok, err = sess_launch.spawn(p)
        (done if ok else failed).append((shown, rec, m, err))

    # Echoed by ID, never by the index just used: the first window to write its
    # transcript renumbers the list.
    out = ["%s %s— %s %d session(s)%s, one new window each%s"
           % (head, TIME, did, len(done),
              (" on %s" % a.model) if a.model else "", R)]
    for _shown, rec, m, _e in done:
        out.append("   %s#%s%s  %s%-*s%s  %s%s%s"
                   % (ACT, rec["sid"][:8], R,
                      TITLE, COL_TITLE, cell(label(rec, m, live), COL_TITLE), R,
                      TIME, dc.mid(m["cwd"], WIDTH - 3 - 9 - 2 - COL_TITLE - 2), R))
    for shown, rec, m, err in failed:
        out.append("   %s%s%s %sfailed to launch: %s%s" % (ACT, shown, R, RED, err, R))
    out.append("%s   %s · %s · %s%s"
               % (TIME, plans[0][3].terminal or "?", plans[0][3].shell or "?",
                  sess_launch.OUTLIVE, R))
    return out


def detail(n, rec, m, live, cur, pno):
    entries, _t, err = dc.read_transcript(rec["path"])
    rows = dc.numbered(entries)
    if pno is not None:
        sel = rows[pno - 1] if 1 <= pno <= len(rows) else None
        if sel is None:
            return ["%s.sess %d %d%s %s— no such prompt; that session has %d%s"
                    % (ACT, n, pno, R, TIME, len(rows), R)], False
        out = ["%s.sess %d %d%s %s— %s · %s · %d chars%s%s"
               % (ACT, n, pno, R, TIME, rec["sid"][:8], dc.hm(sel.ts), len(sel.raw),
                  " · interrupted" if sel.stopped else "", R)]
        out += ["%s%s%s" % (TEXT, ln, R) for ln in dc.wrapped(sel.raw, WIDTH)]
        return out, bool(err)
    r = live.get(rec["sid"])
    if rec["sid"] == cur:
        status = "%s● you are here%s" % (OK, R)
    elif r:
        status = "%s● open now%s%s" % (OK, R,
                                       (" · %s" % r.get("status")) if r.get("status") else "")
    else:
        # Not registered as running is a FACT (no live message socket); when it
        # stopped is not, so the honest clock is the transcript's own last write.
        status = "%snot running · last wrote %s ago%s" \
            % (TIME, _ago(int(time.time() - rec["mtime"])), R)
    start_day, end_day = dc.day(m["started"]), dc.day_of_epoch(rec["mtime"])
    end_hm = time.strftime("%H:%M", time.localtime(rec["mtime"]))
    when = "%s %s → %s" % (start_day or "?", dc.hm(m["started"]),
                           end_hm if start_day == end_day else "%s %s" % (end_day, end_hm))
    # The label gets what the full id leaves (the id is the handle, never cut).
    room = WIDTH - len(".sess %d — " % n) - len(" · ") - len(rec["sid"])
    out = ["%s.sess %d%s %s— %s · %s%s"
           % (ACT, n, R, TIME, dc.cut(label(rec, m, live), room), rec["sid"], R)]
    # "HEAD" is what the client records outside a repo; printing it reads as a
    # detached checkout that never existed.
    branch = dc.cut(m["branch"], 30) if m["branch"] not in ("", "HEAD") else ""
    facts = "  %s  %s · %s · %d prompt(s) · %s" % (
        ("(%s)" % branch) if branch else "", when, size_h(rec["size"]), len(rows),
        m["version"] or "?")
    # The folder is a path: cut in the middle to what the facts leave it.
    out.append("   %s%s%s%s%s%s"
               % (TEXT, dc.mid(m["cwd"] or rec["slug"], WIDTH - 3 - len(facts)), R,
                  TIME, facts, R))
    out.append("   %s" % status)
    out += resume_lines(rec, m, live, cur, n)
    out.append("%s   %s%s" % (TIME, "─" * (WIDTH - 3), R))   # hist_lines' rules span WIDTH
    out += dc.hist_lines(entries, WIDTH)
    if not rows:
        out.append("%s   (no prompts in this transcript)%s" % (TIME, R))
    else:
        foot = dc.compact_footer(entries)
        if foot:
            out.append("%s   %s%s" % (TIME, foot, R))
        out.append("%sUse %s%s.sess %d N%s %sto show prompt N in full%s"
                   % (TIME, R, ACT, n, R, TIME, R))
    return out, bool(err)


def _ago(sec):
    if sec is None:
        return "?"
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % (sec // 60)
    if sec < 172800:
        return "%dh" % (sec // 3600)
    return "%dd" % (sec // 86400)


# ------------------------------------------------------------------- search

def raw_has_all(path, terms, size):
    """Cheap gate before any parsing: do all terms appear in the file at all?

    Read in slices with an overlap, so a term straddling a slice boundary is
    still found and a 32 MB transcript never lands in memory whole. Lowercased
    bytes — the transcripts are UTF-8 and the client does not \\u-escape accents,
    so a French search matches; an embedded quote in the needle would not.
    """
    need = [t.encode("utf-8", "replace") for t in terms]
    if size <= CHUNK:
        try:
            with open(path, "rb") as f:
                b = f.read().lower()
        except OSError:
            return False
        return all(t in b for t in need)
    over = max(len(t) for t in need) + 1
    found = [False] * len(need)
    try:
        with open(path, "rb") as f:
            prev = b""
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                buf = (prev + b).lower()
                for i, t in enumerate(need):
                    if not found[i] and t in buf:
                        found[i] = True
                if all(found):
                    return True
                prev = b[-over:]
    except OSError:
        return False
    return all(found)


def snippet(text, terms, width):
    """Show the part of the prompt that MATCHED, not its first 78 characters —
    the head of a long prompt usually contains none of the words searched for."""
    low = text.lower()
    at = min([low.find(t) for t in terms if low.find(t) >= 0] or [0])
    start = max(0, at - 18)
    while start and text[start] not in " \t":
        start -= 1
    s = text[start:].lstrip()
    return ("…" if start else "") + dc.cut(s, width - (1 if start else 0))


def search(cands, live, cur, a):
    """Terms match a session's prompts and its title; :full also its output."""
    out, hit_rows, t0, partial = [], [], time.time(), 0
    for i, (n, rec, m) in enumerate(cands):
        if time.time() - t0 > BUDGET:
            partial = len(cands) - i
            break
        hay = " ".join((label(rec, m, live), m["title"], dc.flat(m["last"]))).lower()
        title_hit = all(t in hay for t in a.terms)
        if not title_hit and not raw_has_all(rec["path"], a.terms, rec["size"]):
            continue
        entries, _t, _e = dc.read_transcript(rec["path"])
        rows = dc.numbered(entries)
        hits = [(j + 1, e) for j, e in enumerate(rows)
                if all(t in e.one.lower() for t in a.terms)]
        if not hits and not title_hit and not a.full:
            continue        # raw bytes matched only output: that is what :full is for
        hit_rows.append((n, rec, m, hits, title_hit))
    return hit_rows, partial


# -------------------------------------------------------------------- entry

def handle(data, tail, to_context):
    # "?" is help (dc.HELP), before parse: parsed, it would be a search for "?".
    if dc.is_help(tail):
        return _emit(USAGE, to_context, False)
    a = parse(tail)
    cur = data.get("session_id") or ""
    # The project this session belongs to, from the transcript's own folder, not
    # slug_of(cwd): the cwd may be a subfolder.
    here_slug = project_key(data)
    out, noisy = [], False

    if a.err:
        out = ["%s.sess%s %s— %s%s" % (ACT, R, TIME, a.err, R)] + USAGE
        noisy = True
        return _emit(out, to_context, noisy)

    allrows = scan()
    live = registry()
    tpath = data.get("transcript_path") or ""
    if cur and tpath and not os.path.exists(tpath) \
            and not any(r["sid"] == cur for r in allrows):
        allrows.insert(0, unwritten(data, cur, here_slug))
    total = len(allrows)
    if not total:
        return _emit(["%s.sess%s %s— no transcripts under %s%s"
                      % (ACT, R, TIME, PROJECTS, R)], to_context, False)

    # The scope, shared by a listing, a search and ".sess :30 !r". Filters first, then the window: ":50" means the 50 most
    # recent sessions THAT MATCH the filters, which is what "last 50 here" means.
    picked = []
    for i, r in enumerate(allrows):
        if a.here and r["slug"].lower() != here_slug:
            continue
        if a.live and r["sid"] not in live and r["sid"] != cur:
            continue
        picked.append((i + 1, r))          # (global rank, record)
    considered = picked if not a.window else picked[:a.window]

    if a.action:
        return _emit(act(a, allrows, live, cur, [r for _n, r in considered]),
                     to_context, False)

    # A session named outright is resolved against EVERY session: the index is a
    # global rank, so it must not depend on the window that happened to render it.
    if a.idx is not None or a.sid:
        if a.idx is not None:
            # An index means the same session in every view, so it resolves from
            # the last listing here too — otherwise ".sess 7" and ".sess !r 7"
            # could name different sessions once the list has moved. With no
            # listing to resolve against, the global rank is still the honest
            # answer, and this view changes nothing if it is wrong.
            imap, _why = index_read(cur)
            sid = (imap or {}).get(str(a.idx))
            rec = next((r for r in allrows if r["sid"] == sid), None) if sid else None
            if rec is not None:
                n = a.idx
            elif a.idx > total:
                return _emit(["%s.sess %d%s %s— no such session; this machine has %d%s"
                              % (ACT, a.idx, R, TIME, total, R)], to_context, False)
            else:
                n, rec = a.idx, allrows[a.idx - 1]
        else:
            cand = [(i + 1, r) for i, r in enumerate(allrows)
                    if r["sid"].lower().startswith(a.sid)]
            if not cand:
                return _emit(["%s.sess #%s%s %s— no session id starts with that%s"
                              % (ACT, a.sid, R, TIME, R)], to_context, False)
            if len(cand) > 1:
                lines = ["%s.sess #%s%s %s— %d sessions match; add a character:%s"
                         % (ACT, a.sid, R, TIME, len(cand), R)]
                for i, r in cand[:8]:
                    mm = meta(r)
                    lines.append("   %s#%s%s  %s%s · %s%s"
                                 % (ACT, r["sid"][:8], R, TIME,
                                    dc.cut(label(r, mm, live), 46), proj_of(mm, r), R))
                return _emit(lines, to_context, False)
            n, rec = cand[0]
        m = meta(rec)
        body, err = detail(n, rec, m, live, cur, a.pno)
        return _emit(body + (["ERR read_transcript"] if err else []), to_context, False)

    cands = [(n, r, meta(r)) for n, r in considered]

    if a.terms:
        t0 = time.time()
        hits, partial = search(cands, live, cur, a)
        elapsed = time.time() - t0
        # The header says WHERE it looked before it says what it found: a search
        # that reads only prompts and titles must never look like it read
        # everything, or an empty result reads as "I never wrote that".
        looked = "prompts + titles" + (" + output" if a.full else "")
        head = ("%s.sess%s %s— looked for %s in %s · %d session(s) matched%s" %
                (ACT, R, TIME, " + ".join('"%s"' % t for t in a.terms),
                 looked, len(hits), R))
        out = [head]
        last_day = None
        for n, rec, m, rows, title_hit in hits:
            d = dc.day_of_epoch(rec["mtime"])
            if d != last_day:
                h = "   ══ %s " % d
                out.append("%s%s%s%s" % (TIME, h, "═" * max(0, WIDTH - len(h)), R))
                last_day = d
            out.append(row(n, rec, m, live, cur))
            # WHERE it matched, always said out loud: a prompt, the title, or —
            # only under :full — text neither of us typed.
            if rows:
                out.append("    %s   │ matched in %d prompt(s)%s%s"
                           % (TIME, len(rows),
                              ", first %d:" % HITS if len(rows) > HITS else ":", R))
            elif title_hit:
                # Name WHICH title: the row shows the live session's name, which
                # may not be the string that matched.
                out.append('    %s   │ matched in the title "%s" — in no prompt%s'
                           % (TIME, dc.cut(m["title"], 50), R))
            else:
                out.append("    %s   │ matched in the session's output (replies, tool "
                           "results) — in no prompt%s" % (TIME, R))
            for pn, e in rows[:HITS]:
                out.append("    %s%3d│%s %s%s%s %s%s%s"
                           % (TITLE, pn, R, TIME, dc.hm(e.ts), R,
                              TEXT, snippet(e.one, a.terms, WIDTH - 16), R))
        # Only the rows actually rendered: an index the user could not read is
        # not one they can type.
        index_write(cur, [(n, rec["sid"]) for n, rec, _m, _r, _t in hits])
        out += legend(a, len(hits), len(considered), len(picked), total,
                      "searched %d in %.1fs" % (len(cands), elapsed))
        if partial:
            out.append("%s   stopped after %.0fs — %d session(s) unsearched, narrow with "
                       "%s:here%s %sor a smaller window%s"
                       % (TIME, BUDGET, partial, ACT, R, TIME, R))
        if not hits:
            # Prose swallowed as a search is the one recoverable accident here.
            noisy = True
            out.append("%s   nothing matched in the %s of the %d session(s) searched%s%s"
                       % (TIME, looked, len(cands),
                          "" if a.full else " — %s:full%s%s also looks in the output"
                          % (R + ACT, R, TIME), R))
        else:
            out.append("%sUse %s%s.sess N%s %sfor one session, %s%s.sess N M%s "
                       "%sfor a prompt%s" % (TIME, R, ACT, R, TIME, R, ACT, R, TIME, R))
    else:
        head = "%s.sess%s %s— sessions on this machine, newest first%s" % (ACT, R, TIME, R)
        out = listing(cands, live, cur, head)
        index_write(cur, [(n, rec["sid"]) for n, rec, _m in cands])
        out += legend(a, len(cands), len(considered), len(picked), total)
        # Counted over the rows shown: this line previews ".sess !r" with the
        # same scope options, which reads exactly these rows.
        left = sum(1 for _n, r, m in cands
                   if r["sid"] != cur and r["sid"] not in live and pending(r, m))
        if left:
            out.append("%s   %s○%s %s%d never closed with exit — %s.sess !r%s %s+ the "
                       "same options reopens up to %d, newest first%s"
                       % (TIME, R + RED, R, TIME, left, R + ACT, R, TIME, MAX_SPAWN, R))
        out.append("%sUse %s%s.sess N%s %sfor details + how to resume · %s%s.sess word%s "
                   "%sto search%s" % (TIME, R, ACT, R, TIME, R, ACT, R, TIME, R))
    return _emit(out, to_context, noisy)


def _emit(lines, to_context, noisy):
    if to_context:
        dc.emit_context_block(lines, ".sess", noisy=noisy)
    else:
        dc.emit_block(lines, noisy=noisy)
