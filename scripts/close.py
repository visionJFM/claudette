#!/usr/bin/env python3
"""/claudette:close — end THIS session's claude process, and the console window
or tab it runs in when nothing else lives there.

Why it exists (CLAUDETTE_DESIGN.md, "Reopening after a reboot"): `.sess` marks ✓
the sessions the user closed on purpose. A close typed in the terminal ("quit",
"exit") reaches only the client's history.jsonl, and a prompt sent from the phone
or the desktop app never does. A skill command is filed in the transcript wherever
it was typed, so invoking this skill IS the closing trace; this script only ends
the process afterwards.

Two steps, because the model still has to write its last line:
  close.py [--session <id>]   checks everything, then starts the detached killer
  close.py --kill <json>      (internal) waits for the transcript to go quiet,
                              then ends the process tree
The `.close` dot-command (dot_close.py) calls launch() from the hook,
with no agent: its killer waits for the session to go idle and for the client's
record of the blocked prompt instead of for a last reply.

Which claude: CLAUDE_PID, which the client passes to every tool command, when it
is an ancestor of this script and the client's own registry
(~/.claude/sessions/<pid>.json) names it; else the nearest registered `claude`
ancestor. The desktop app runs its sessions under a binary also named claude.exe,
so a name alone is never enough.

It ends nothing unless the registry says entrypoint "cli" and the session is not a
cloud one: the desktop app and IDE extensions host other sessions, and a cloud
session's machine is not the user's.

The console: the run of shells directly above claude is ended with it when that
run ends on a terminal host, on explorer (a console opened from Win+R or the
Start menu) or on a parent that is gone (a window launched by `.proj !new` from a
session since closed). Then nothing is left in the console and it closes. Nested
shells the user opened by hand (PowerShell typed inside cmd) go too: /close
closes the window. A run that ends on anything else (an IDE,
tmux, sshd, another claude) keeps its console and only claude ends.

A parent counts only if it was created before its child: a dead parent's pid can
be reused by an unrelated process. Each pid is checked again, by creation time,
right before it is ended.
"""
import calendar
import json
import os
import subprocess
import sys
import time

import sess_launch

WINDOWS = os.name == "nt"
CLAUDE = ("claude.exe", "claude")
SHELLS = ("bash.exe", "sh.exe", "pwsh.exe", "powershell.exe", "cmd.exe", "zsh.exe",
          "fish.exe", "nu.exe", "bash", "sh", "zsh", "fish", "pwsh", "nu", "-bash",
          "-zsh", "-sh", "-fish")
# login sits between a macOS terminal and its shell; it is root-owned and the
# terminal's own, so the walk stops there rather than signalling it.
HOSTS = ("windowsterminal.exe", "openconsole.exe", "conhost.exe", "mintty.exe",
         "explorer.exe", "alacritty.exe", "wezterm-gui.exe", "terminal", "iterm2",
         "login", "alacritty", "kitty", "wezterm-gui", "ghostty", "konsole",
         "gnome-terminal-server", "xterm")
GONE = "(gone)"
QUIET = 3.0            # s with no transcript write = the last reply is out
MIN_WAIT, MAX_WAIT = 4.0, 45.0
TURN_MAX = 4 * 3600    # .close typed mid-turn: wait this long for the turn to end
TRACE_MAX = 10.0       # .close: s idle without its trace before ending anyway
LOG = os.path.join(os.path.expanduser("~"), ".claude", "logs", "claudette-close.log")
SHOWN_ENV = ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
             "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_EXECPATH")


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _registry(pid):
    p = os.path.join(os.path.expanduser("~"), ".claude", "sessions", "%d.json" % pid)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ctime(pid):
    """Creation time of pid, comparable within one OS: a FILETIME int on Windows
    (the registry's procStart uses it too), `ps` lstart text elsewhere. None when
    unreadable."""
    if WINDOWS:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            c, e, k, u = (wt.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                       ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k32.CloseHandle(h)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except Exception:
        return None


def _older(parent, child):
    """True if parent was created before child; False if after or unknown."""
    return _after(_ctime(parent), _ctime(child))


def chain_here():
    """[(exe, pid)] from this process upward, ending with (GONE, ppid) when the
    last parent no longer exists (or its pid now names a younger process)."""
    return chain_from(os.getpid())


def chain_from(start):
    """chain_here() from any live pid upward; [] when start is not running."""
    tree = sess_launch._win_tree() if WINDOWS else sess_launch._posix_tree()
    chain, pid, seen = [], start, set()
    while pid in tree and pid not in seen:
        seen.add(pid)
        ppid, exe = tree[pid]
        chain.append((exe, pid))
        if not ppid:
            break
        if not WINDOWS and ppid == 1:
            chain.append((GONE, 1))     # POSIX re-parents an orphan to pid 1
            break
        if ppid not in tree or not _older(ppid, pid):
            chain.append((GONE, ppid))
            break
        pid = ppid
    return chain


def plan(sid="", chain=None, registry=None, env=None, chain_up=None, ctime=None):
    """{"claude", "shells", "sid", "transcript"} or {"refuse": why}.
    chain = [(exe, pid)] from this process upward, maybe ending with (GONE, pid);
    registry = pid -> dict; env = the environment; chain_up = chain_from;
    ctime = _ctime (the last two injectable for tests)."""
    env = os.environ if env is None else env
    chain = chain_here() if chain is None else chain
    registry = _registry if registry is None else registry
    chain_up = chain_from if chain_up is None else chain_up
    ctime = _ctime if ctime is None else ctime
    if (env.get("CLAUDE_CODE_REMOTE") or "").lower() in ("1", "true"):
        return {"refuse": "this is a cloud session: end it in the app"}
    sid = sid or env.get("CLAUDE_CODE_SESSION_ID") or ""
    names = [exe.lower() for exe, _pid in chain]
    pids = [p for _exe, p in chain]

    def ok(i):
        reg = registry(pids[i])
        return bool(reg and reg.get("sessionId")
                    and (not sid or reg["sessionId"] == sid))

    idx = None
    try:
        want = int(env.get("CLAUDE_PID") or 0)
    except ValueError:
        want = 0
    if want and want in pids and names[pids.index(want)] != GONE:
        i = pids.index(want)
        if ok(i):
            idx = i
    if idx is None:
        for i, n in enumerate(names):
            if n in CLAUDE and ok(i):
                idx = i
                break           # the NEAREST registered claude: the one running us
    if (idx is None and want and chain and names[-1] == GONE
            and not any(n in CLAUDE for n in names)):
        # Git Bash breaks the chain whenever bash starts bash, as `bash run.sh`
        # does: the intermediate exits, leaving "(gone)" below claude
        # (CLAUDETTE_DESIGN.md, "/close never reached claude through run.sh").
        # Then CLAUDE_PID stands in for the walk, proved by the registry: same
        # session, and the pid's creation time equal to the procStart the client
        # wrote, so a reused pid fails.
        up = chain_up(want)
        reg = registry(want)
        if (up and up[0][1] == want and up[0][0].lower() in CLAUDE and reg
                and reg.get("procStart")
                and str(ctime(want)) == str(reg["procStart"])):
            chain = up
            names = [exe.lower() for exe, _pid in chain]
            pids = [p for _exe, p in chain]
            if ok(0):
                idx = 0
    if idx is None:
        return {"refuse": "no claude process above this one is registered%s"
                          % (" as session %s" % sid[:8] if sid else "")}
    reg = registry(pids[idx])
    if reg.get("entrypoint") != "cli":
        return {"refuse": "this session runs in %s, which hosts other sessions: "
                          "close it there" % (reg.get("entrypoint") or "an app")}
    shells, j = [], idx + 1
    while j < len(chain) and names[j] in SHELLS:
        shells.append(pids[j])
        j += 1
    end = names[j] if j < len(chain) else GONE
    console = end in HOSTS or end == GONE
    if not console:
        shells = []             # an IDE, tmux, sshd or another claude owns that console
    return {"claude": pids[idx], "shells": shells, "console": console,
            "sid": reg["sessionId"],
            "proc_start": reg.get("procStart") or "",
            "transcript": _transcript(reg["sessionId"])}


def _transcript(sid):
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        for d in os.scandir(root):
            p = os.path.join(d.path, sid + ".jsonl")
            if os.path.isfile(p):
                return p
    except OSError:
        pass
    return ""


def _descendants(pid, tree=None, ctime=None):
    """[(pid, exe, creation time)] of every process under pid, deepest first, from
    one snapshot. A child counts only if it was created AFTER its parent: a
    parent pid names whatever process holds that number NOW, and when the real
    parent is dead and its number reused, an older unrelated tree hangs under
    the new holder, and ending it would take another live session with it
    (CLAUDETTE_DESIGN.md, "A session lost to a .close"). The upward walk
    (chain_from) applies the same rule."""
    tree = (sess_launch._win_tree() if WINDOWS else sess_launch._posix_tree()) \
        if tree is None else tree
    ctime = _ctime if ctime is None else ctime
    kids = {}
    for c, (pp, _exe) in tree.items():
        kids.setdefault(pp, []).append(c)
    born = {pid: ctime(pid)}
    out_l, stack, seen = [], [pid], set()
    while stack:
        p = stack.pop()
        for c in kids.get(p, []):
            if c in seen or c == p:
                continue
            seen.add(c)
            born[c] = ctime(c)
            if not _after(born[p], born[c]):
                continue                # older than its "parent": not its child
            out_l.append((c, tree[c][1], born[c]))
            stack.append(c)
    return list(reversed(out_l))


def _after(parent_born, child_born):
    """True if the child was created at or after the parent; False if before or
    unknown (an unreadable process is left alone rather than ended blind)."""
    if parent_born is None or child_born is None:
        return False
    if WINDOWS or isinstance(parent_born, int):
        return parent_born <= child_born
    try:
        fmt = "%a %b %d %H:%M:%S %Y"
        return time.mktime(time.strptime(" ".join(str(parent_born).split()), fmt)) <= \
            time.mktime(time.strptime(" ".join(str(child_born).split()), fmt))
    except ValueError:
        return False


def _end(pid, sig=None):
    if WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0001, False, int(pid))       # PROCESS_TERMINATE
        if not h:
            return False
        try:
            # Exit code 0 reads as a graceful exit: Windows Terminal closes the tab.
            return bool(k32.TerminateProcess(h, 0))
        finally:
            k32.CloseHandle(h)
    import signal
    try:
        os.kill(pid, signal.SIGTERM if sig is None else sig)
        return True
    except OSError:
        return False


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def trace_seen(path, offset, since=0, needle=".close"):
    """True once the transcript holds, past `offset` and stamped at or after
    `since`, the client's own record of a blocked `needle` prompt: a system
    entry ending "Original prompt: <needle>" (typed while idle) or a
    queue-operation carrying it (typed mid-turn)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return False
    for raw in chunk.decode("utf-8", "replace").splitlines():
        if needle not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        c = d.get("content") if isinstance(d, dict) else None
        if not isinstance(c, str) or _epoch(d.get("timestamp")) < int(since):
            continue
        if d.get("type") == "system" and c.rstrip().endswith(
                "Original prompt: " + needle):
            return True
        if d.get("type") == "queue-operation" and c.strip() == needle:
            return True
    return False


def _wait_turn_and_trace(p, same):
    """.close: no reply of ours to wait for, but the hook runs before the client
    writes the blocked prompt, and a .close typed mid-turn is blocked at once
    (the hook runs when the prompt is queued): end claude only once the session
    is idle and the trace is on disk. False = give up, end nothing."""
    path, offset, sid = p.get("transcript") or "", p.get("offset") or 0, p.get("sid", "")
    t0 = time.time()
    idle_since = None
    while True:
        if not same(p["claude"]):
            _log("session %s: claude %s ended before .close did; nothing ended"
                 % (sid[:8], p["claude"]))
            return False
        reg = _registry(p["claude"]) or {}
        if reg.get("status") == "busy":
            idle_since = None
            if time.time() - t0 > TURN_MAX:
                _log("session %s: still busy after %d s; .close gave up"
                     % (sid[:8], TURN_MAX))
                return False
        else:
            idle_since = idle_since or time.time()
            if trace_seen(path, offset, p.get("since") or 0):
                return True
            if time.time() - idle_since > TRACE_MAX:
                _log("session %s: idle, no .close trace after %d s; ending anyway"
                     % (sid[:8], TRACE_MAX))
                return True
        time.sleep(0.25)


def kill(p):
    """The detached half: wait for the last reply, then end the tree."""
    born = p.get("born") or {}
    same = lambda pid: str(_ctime(pid)) == born.get(str(pid))   # noqa: E731
    t0, path = time.time(), p.get("transcript") or ""
    if p.get("wait") == "trace":
        if not _wait_turn_and_trace(p, same):
            return
    else:
        while time.time() - t0 < MAX_WAIT:
            time.sleep(0.5)
            if time.time() - t0 < MIN_WAIT:
                continue
            try:
                quiet = time.time() - os.path.getmtime(path) >= QUIET
            except OSError:
                quiet = True
            if quiet:
                break
    if not same(p["claude"]):
        _log("session %s: claude %s is gone or another process now; nothing ended"
             % (p.get("sid", "")[:8], p["claude"]))
        return
    me = os.getpid()
    under = [c for c in _descendants(p["claude"]) if c[0] != me]
    # Each one checked again by creation time right before it is ended: one that
    # exited since the snapshot may have handed its number on.
    done = [c for c in under if _ctime(c[0]) == c[2] and _end(c[0])]
    _log("session %s: ending under claude %s: %s" % (
        p.get("sid", "")[:8], p["claude"],
        ", ".join("%s %d (born %s)%s" % (exe, c, b, "" if (c, exe, b) in done else
                                          " NOT ended") for c, exe, b in under)
        or "nothing"))
    # Every shell BEFORE claude: `bash -lc "claude; exec bash"` would start a
    # fresh shell the moment claude ends, and if the client keeps its children in
    # a job that dies with it, this process may not outlive claude by a call.
    # An interactive zsh ignores SIGTERM: shells get SIGHUP, as a closed window does.
    hup = None
    if not WINDOWS:
        import signal
        hup = signal.SIGHUP
    shells = [s for s in p.get("shells") or [] if same(s)]
    sh = [s for s in shells if _end(s, hup)]
    ok = _end(p["claude"])
    _log("session %s: claude %s ended=%s, %d of %d children, shells %s ended %s"
         % (p.get("sid", "")[:8], p["claude"], ok, len(done), len(under),
            p.get("shells"), sh))


def _why(chain, env):
    """What a refusal shows, so a failure elsewhere can be diagnosed from it."""
    shown = []
    for k in SHOWN_ENV:
        v = env.get(k)
        if v:
            shown.append("%s=%s" % (k, v[:8] + "..." if k.endswith("SESSION_ID") else v))
    return "chain: %s; env: %s" % (
        " <- ".join("%s %s" % (e, p) for e, p in chain) or "(none)",
        ", ".join(shown) or "(none)")


def pid_of_session(sid):
    """The pid the client registered for session `sid`, or 0. A hook gets the
    session id on stdin but may not get CLAUDE_PID, which plan() needs when the
    chain is broken."""
    d = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for n in names:
        stem = n[:-5] if n.endswith(".json") else ""
        if stem.isdigit() and (_registry(int(stem)) or {}).get("sessionId") == sid:
            return int(stem)
    return 0


def launch(sid="", env=None, wait=None):
    """Plan, check, start the detached killer: (plan, None) or (None, (why, chain)).
    wait="trace" is .close's: wait for the turn's end and the blocked prompt's
    trace instead of for the agent's last line."""
    env = os.environ if env is None else env
    chain = chain_here()
    p = plan(sid, chain=chain, env=env)
    if "refuse" in p:
        return None, (p["refuse"], _why(chain, env))
    p["born"] = {str(x): str(_ctime(x)) for x in [p["claude"]] + p["shells"]}
    if WINDOWS and p["proc_start"] and p["born"][str(p["claude"])] != p["proc_start"]:
        # The registry's procStart is the process creation time: a mismatch means
        # its pid now names another process.
        return None, ("the session registry is stale for pid %d" % p["claude"],
                      _why(chain, env))
    if wait == "trace":
        # Read back 64 KB and filter by time: a .close typed mid-turn may be
        # enqueued in the transcript just BEFORE the hook runs.
        p["wait"], p["since"] = "trace", time.time() - 2
        try:
            p["offset"] = max(0, os.path.getsize(p["transcript"]) - 65536)
        except OSError:
            p["offset"] = 0
    _spawn_killer(p)
    return p, None


def start(sid=""):
    p, why = launch(sid)
    if why:
        print("NOT ENDED: %s. The closing trace is written; close it by hand.\n(%s)" % why)
        return 2
    print("ENDING: claude pid %d%s in about %d s, after your last line. Log: %s"
          % (p["claude"], (" and its console%s" % (
              " (shell pids %s)" % ", ".join(str(s) for s in p["shells"])
              if p["shells"] else "")) if p["console"]
             else ", console kept (an editor, tmux, ssh or another session owns it)",
             int(MIN_WAIT), LOG))
    try:
        import prefs
        if prefs.effective("dot_commands"):
            print("TIP: next time, type .close: the same close with no agent turn, "
                  "in a second or two.")
    except Exception:
        pass
    return 0


def _spawn_killer(p):
    args = [sys.executable, os.path.abspath(__file__), "--kill", json.dumps(p)]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if WINDOWS:
        # No window of its own: it lives seconds and its job is to close windows.
        # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW, and out of the
        # client's job when it allows that, so the killer outlives its target.
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(args, **kw)
    except OSError:
        if not WINDOWS:
            raise
        kw["creationflags"] &= ~0x01000000          # breakaway refused by the job
        subprocess.Popen(args, **kw)


def main(argv):
    if len(argv) == 1:
        return start()
    if len(argv) == 3 and argv[1] == "--session" and argv[2]:
        return start(argv[2])
    if len(argv) == 3 and argv[1] == "--kill":
        kill(json.loads(argv[2]))
        return 0
    print("usage: close.py [--session <session id>]")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
