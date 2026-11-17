#!/usr/bin/env python3
"""Open a session in a NEW terminal window — the one side effect ".sess" has.

The rule is least surprise: reuse the terminal and the shell that are already
running. Every terminal has its own "new window running X" CLI and some have none,
so an adapter exists only where it could be TESTED. Anything else falls back to
printing the commands, instead of half-working.

    Windows  wt.exe (WT_SESSION / ancestor walk) · conhost via CREATE_NEW_CONSOLE
    macOS    Terminal.app, iTerm2 (TERM_PROGRAM), both through AppleScript
    other    no adapter -> print the block, say why

DETACHMENT is the point: closing the console that ran ".sess" must never take
the spawned windows with it. Two mechanisms, by adapter:
  - HANDOFF (wt, AppleScript): our child asks an already-running terminal to make
    the window and exits, so the window is that process's child, never ours.
    DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB cover the case where no terminal
    exists yet; start_new_session is the POSIX belt.
  - OWN CONSOLE (conhost): CREATE_NEW_CONSOLE gives the shell a console of its
    own, which is what makes it survive ours. Its stdio is therefore NOT
    redirected — that console IS its terminal.

QUOTING is contained on purpose: the cwd travels through the terminal's own
working-directory flag — or through CreateProcess itself — never through a shell
string, so the only thing interpolated into a command line is a session id or a
model alias. macOS is the exception (AppleScript "do script" has no directory
flag), and is the only place a path is escaped.

THE SHELL IS LAUNCHED BY PATH, never by name. CreateProcess resolves a bare name
against PATH, and "bash.exe" there is System32's WSL launcher, which opens a WSL
window with no claude in it. detect() carries the full image path of the ancestor
it matched.

RE-PARSING IS THE HAZARD: every program between us and the shell re-reads the
command line with its own rules. "cmd /c start" splits on ";" despite the quotes,
and wt.exe splits on ";" too because that is its subcommand separator. The payload
contains one ("...; exec bash", which keeps a bash window open). So conhost makes
its own console and never touches cmd, and the wt path escapes the semicolon.
Prefer removing a parser to escaping for it.

WT ALSO DROPS INNER QUOTES: wt.exe rebuilds the tab's command line
by wrapping each argument that holds a space in double quotes, without escaping
the quotes inside it, so `claude --name "a b" "c d"` reached bash as `claude
--name a`. A command that holds a double quote therefore never crosses wt as
text: it is written to a file under logs/launch/, and only that file's path
does — bash and cmd run the file, PowerShell reads it as text (a -File would
meet the execution policy). A command with no quote keeps its one-line payload,
which wt has always carried intact.
"""
import os
import re
import subprocess
import sys

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

# Ancestors we recognize. The shell is what the user typed "claude" into; the
# terminal is what drew the window. Only the terminal decides whether a launch is
# possible at all.
SHELLS = {
    "powershell.exe": ("powershell", ["-NoExit", "-Command"]),
    "pwsh.exe": ("pwsh", ["-NoExit", "-Command"]),
    "cmd.exe": ("cmd", ["/k"]),
    "bash.exe": ("bash", ["-lc"]),
    "zsh": ("zsh", ["-lc"]),
    "bash": ("bash", ["-lc"]),
}
TERMINALS = {
    "windowsterminal.exe": "windows-terminal",
    "openconsole.exe": "windows-terminal",
    "conhost.exe": "conhost",
    "conemu64.exe": "conemu",
    "conemuc64.exe": "conemu",
    "alacritty.exe": "alacritty",
    "wezterm-gui.exe": "wezterm",
}
# Terminals we can actually drive. A detected-but-absent adapter says so by name
# rather than pretending the terminal is unknown.
ADAPTERS = ("windows-terminal", "conhost", "apple-terminal", "iterm2")

# Where a quoted command waits for the tab that runs it (see WT ALSO DROPS INNER
# QUOTES above). None = logs/launch in claudette's folder; tests point it elsewhere.
# A file is read once, when its tab starts; each launch prunes those older than
# LAUNCH_KEEP_S.
LAUNCH_DIR = None
LAUNCH_KEEP_S = 86400
_LAUNCH_SEQ = [0]

# The last line of every launch report (.proj !…, .sess !r/!f). dot_proc reads it
# back from the transcripts as the record of that launch, so the wording lives
# here once, for both writers and that reader.
OUTLIVE = "the new windows outlive this console"


# ------------------------------------------------------------------ boot time

_BOOT = []


def boot_time():
    """Epoch seconds of the last boot, or None if this platform cannot say.

    Memoized: macOS pays a `sysctl` subprocess for it, and a listing asks twice
    (write, then prune). Within one process the answer cannot change.

    Only used to invalidate a session-index map across a reboot. Windows takes
    GetTickCount64, which counts wall time INCLUDING sleep, so a laptop waking up
    does not look like a new boot. macOS reads kern.boottime.
    """
    if not _BOOT:
        _BOOT.append(_boot_time())
    return _BOOT[0]


def _boot_time():
    try:
        if WINDOWS:
            import ctypes
            return _now() - ctypes.windll.kernel32.GetTickCount64() / 1000.0
        if MACOS:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"sec\s*=\s*(\d+)", out)
            return float(m.group(1)) if m else None
        with open("/proc/stat") as f:
            for ln in f:
                if ln.startswith("btime "):
                    return float(ln.split()[1])
    except Exception:
        return None
    return None


def _now():
    import time
    return time.time()


# ------------------------------------------------------------------- detection

def _win_ancestors():
    """[(exe_lowercase, pid), ...] from this process upward. ctypes only.

    A Toolhelp snapshot is one call and gives the whole tree, so the shell and
    the terminal host come out of a single walk. Env vars are NOT used for the
    shell: PSModulePath and friends are inherited, so they survive a drop into
    cmd.exe from PowerShell and would lie about what is running.
    """
    tree = _win_tree()
    chain, pid, seen = [], os.getpid(), set()
    while pid in tree and pid not in seen:
        seen.add(pid)
        ppid, exe = tree[pid]
        chain.append((exe, pid))
        pid = ppid
    return chain


def _win_tree():
    """{pid: (ppid, exe_lowercase)} for every process, from one Toolhelp snapshot."""
    import ctypes
    import ctypes.wintypes as wt

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == -1:
        return {}
    tree = {}
    try:
        e = ENTRY()
        e.dwSize = ctypes.sizeof(ENTRY)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            tree[int(e.th32ProcessID)] = (int(e.th32ParentProcessID),
                                          e.szExeFile.decode("mbcs", "replace").lower())
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return tree


def _win_image_path(pid):
    """Full path of a process's exe, or "" if it cannot be read.

    The Toolhelp snapshot gives szExeFile — the bare NAME — and a bare name is
    resolved against PATH by CreateProcess: "bash.exe" on the system PATH is
    C:\\Windows\\System32\\bash.exe, the WSL launcher, not the Git Bash the user is
    typing in. So the shell is launched by the path of the ANCESTOR WE MATCHED,
    the exact binary that is already running.
    """
    import ctypes
    import ctypes.wintypes as wt
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    try:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wt.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _posix_ancestors():
    """Same walk on POSIX, from one `ps` call rather than one per level."""
    tree = _posix_tree()
    chain, pid, seen = [], os.getpid(), set()
    while pid in tree and pid not in seen:
        seen.add(pid)
        ppid, exe = tree[pid]
        chain.append((exe, pid))
        pid = ppid
    return chain


def _posix_tree():
    """{pid: (ppid, exe_lowercase)} from one `ps` call."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    tree = {}
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            tree[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2]).strip().lower())
    return tree


def detect(env=None, ancestors=None):
    """{shell, shell_argv, terminal, adapter} — what is running around us."""
    env = os.environ if env is None else env
    chain = ancestors if ancestors is not None else (
        _win_ancestors() if WINDOWS else _posix_ancestors())

    shell, shell_argv, shell_path = None, None, ""
    for exe, pid in chain:
        if exe in SHELLS:
            shell, shell_argv = SHELLS[exe]
            # The PATH of the running shell, not its name — see _win_image_path.
            shell_path = _win_image_path(pid) if WINDOWS else ""
            break

    terminal = None
    if env.get("WT_SESSION"):
        terminal = "windows-terminal"          # definitive, and cheaper than the walk
    elif env.get("ConEmuPID"):
        terminal = "conemu"
    else:
        tp = (env.get("TERM_PROGRAM") or "").lower()
        if tp == "apple_terminal":
            terminal = "apple-terminal"
        elif tp == "iterm.app":
            terminal = "iterm2"
        elif tp == "vscode":
            terminal = "vscode"
        elif tp:
            terminal = tp
    if terminal is None:
        for exe, _pid in chain:
            if exe in TERMINALS:
                terminal = TERMINALS[exe]
                break
    if terminal is None and WINDOWS:
        # conhost.exe is ATTACHED to the console, never an ancestor, so the walk
        # above can never find it, and GetConsoleWindow() lies under an MSYS pty
        # (returns 0 in Git Bash). Nothing matched on Windows therefore means the
        # classic console. A terminal we DID recognize but cannot drive (conemu,
        # alacritty, wezterm) never reaches here: it keeps its name and takes the
        # print fallback.
        terminal = "conhost"

    return {"shell": shell, "shell_argv": shell_argv, "shell_path": shell_path,
            "terminal": terminal, "adapter": terminal if terminal in ADAPTERS else None}


# --------------------------------------------------------------------- the plan

class Plan(object):
    """What would happen, decided before anything is spawned.

    `shown` is what the user would type by hand — it is printed whether or not
    the launch is possible, so the fallback and the confirmation are the same
    text and cannot drift apart.
    """
    __slots__ = ("argv", "shown", "why", "terminal", "shell", "cwd", "new_console",
                 "script")

    def __init__(self, argv, shown, why, terminal, shell, cwd=None, new_console=False,
                 script=None):
        self.argv, self.shown, self.why = argv, shown, why
        self.terminal, self.shell = terminal, shell
        # new_console: WE make the window (CREATE_NEW_CONSOLE) and hand the cwd to
        # CreateProcess, instead of asking another program to do both for us.
        self.cwd, self.new_console = cwd, new_console
        # script: (path, text) that spawn() writes before launching — the command
        # itself, when it cannot cross wt as text. Planning writes nothing.
        self.script = script

    @property
    def ok(self):
        return self.argv is not None


def _sh_quote(p):
    return "'" + p.replace("'", "'\\''") + "'"


def _as_quote(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _launch_dir():
    if LAUNCH_DIR:
        return LAUNCH_DIR
    import paths
    return paths.log("launch")


def _launch_script(sh, claude):
    """(path, text, shell args) for a command that must not cross wt as text.

    Only the path reaches wt's command line, and it holds no quote; a path with
    a space is wrapped by wt and arrives whole. Each shell reads the file its own
    way:
      bash        runs it as a login shell, then `exec bash` keeps the tab open,
                  as "; exec bash" did on the one-line path;
      powershell  reads it as text and runs that (-NoExit keeps the tab): -File
                  would meet the execution policy (Restricted by default), and
                  relaxing it would leave the tab relaxed — what clean_env strips
                  PSExecutionPolicyPreference=Bypass for;
      cmd         runs it as a batch file: UTF-8 code page first, so an accent
                  in a title survives, and % doubled, since a batch file expands it.
    """
    _LAUNCH_SEQ[0] += 1
    stem = os.path.join(_launch_dir(), "launch-%d-%d-%d" % (
        os.getpid(), int(_now() * 1000), _LAUNCH_SEQ[0]))
    if sh == "bash":
        path = stem + ".sh"
        return path, claude + "\nexec bash\n", ["-l", path.replace("\\", "/")]
    if sh == "cmd":
        path = stem + ".cmd"
        return path, "@chcp 65001>nul\r\n" + claude.replace("%", "%%") + "\r\n", ["/k", path]
    path = stem + ".ps1"
    run = "& ([scriptblock]::Create([IO.File]::ReadAllText('%s')))" % path.replace("'", "''")
    return path, claude + "\n", ["-NoExit", "-Command", run]


def _write_launch_script(path, text):
    """Write the command where its tab will read it; prune launches over a day old."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    cutoff = _now() - LAUNCH_KEEP_S
    for name in os.listdir(folder):
        old = os.path.join(folder, name)
        try:
            if name.startswith("launch-") and os.path.getmtime(old) < cutoff:
                os.remove(old)
        except OSError:
            pass
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


def plan(cwd, sid, fork, det=None, model=None):
    """How to open `sid` in `cwd` here — or why we will only print it.

    `model` is "--model X" or nothing: it is a global flag of the client ("Model
    for the current session"), so it composes with --resume and --fork-session.
    Only values from dot_sess.MODELS ever reach here; the caller asserts it.
    """
    return plan_cmd(cwd, "claude --resume %s%s%s"
                    % (sid, " --fork-session" if fork else "",
                       " --model %s" % model if model else ""), det)


def plan_cmd(cwd, claude, det=None):
    """How to run `claude` (a full command line) in a NEW window at `cwd`.

    Serves ".proj !new", which opens a session that has no id to resume. The caller
    contributes only the command string, and the QUOTING contract in this module's
    docstring makes that safe: the cwd travels through the terminal's own directory
    flag, so a caller may only interpolate characters that are harmless in a shell
    word (session ids: hex + dashes; model aliases: lowercase + dashes). Callers
    must not pass user text — the one exception is .new's title, double-quoted and held to dot_proj.NAME_OK first (letters,
    digits, spaces and - _ . , : ' ( ): nothing any of these shells or
    AppleScript reads as syntax inside double quotes). wt does break double
    quotes (module docstring), so under wt a command holding one travels in a
    file; NAME_OK still applies, since the shell reads the file by its own rules.
    """
    det = detect() if det is None else det
    shown = ["cd %s" % (_sh_quote(cwd) if not WINDOWS else '"%s"' % cwd), claude]
    adapter, term, shell = det["adapter"], det["terminal"], det["shell"]

    if adapter is None:
        why = ("no launcher for this terminal (%s)" % term if term
               else "could not tell which terminal is running")
        return Plan(None, shown, why, term, shell)

    if adapter in ("windows-terminal", "conhost"):
        sh, sh_argv = (det["shell"], det["shell_argv"])
        if not sh:
            sh, sh_argv = "powershell", SHELLS["powershell.exe"][1]
        # The matched path first; the bare name only when it could not be read
        # (access denied, or a synthetic detect() in a test). A bare name is a
        # PATH lookup at spawn time: "bash.exe" would reach WSL.
        exe = det.get("shell_path") or {
            "powershell": "powershell.exe", "pwsh": "pwsh.exe",
            "cmd": "cmd.exe", "bash": "bash.exe"}[sh]
        # bash needs an explicit re-exec or the window closes when claude exits;
        # the others have a no-exit flag.
        payload = claude + ("; exec bash" if sh == "bash" else "")
        if adapter == "windows-terminal":
            # No "-w": a bare wt creates a NEW window, which is what was asked
            # for. "-d" carries the cwd, so no path ever enters a shell string.
            # ";" MUST be escaped: it is wt's own subcommand separator and it
            # splits on it inside quotes too, so the bash payload's "; exec bash"
            # would otherwise arrive as a second wt command.
            if '"' in claude:
                path, text, args = _launch_script(sh, claude)
                argv = ["wt.exe", "nt", "-d", cwd, exe] + [a.replace(";", "\\;") for a in args]
                return Plan(argv, shown, None, term, sh, script=(path, text))
            argv = (["wt.exe", "nt", "-d", cwd, exe] + list(sh_argv)
                    + [payload.replace(";", "\\;")])
            return Plan(argv, shown, None, term, sh)
        # conhost: make the console OURSELVES rather than going through
        # "cmd /c start", which re-splits on ";" despite the quotes. With
        # CREATE_NEW_CONSOLE there is no second parser: argv goes to
        # CreateProcess as-is, and the cwd rides CreateProcess too, so neither a
        # semicolon nor a path with spaces or forward slashes can be re-read by
        # anything.
        if sh == "cmd":
            # cmd /k takes the rest of its command line raw and never reads the
            # \" that list2cmdline writes for a quote inside the payload, so
            # .new's quoted title would reach claude with literal backslashes
            # and split on its spaces. One raw string, for cmd only.
            return Plan('"%s" /k %s' % (exe, payload), shown, None, term, sh,
                        cwd=cwd, new_console=True)
        return Plan([exe] + list(sh_argv) + [payload], shown, None, term, sh,
                    cwd=cwd, new_console=True)

    # macOS. "do script" has no working-directory flag, so the cwd goes into the
    # command string here and nowhere else — shell-quoted, then AppleScript-escaped.
    cmd = "cd %s && %s" % (_sh_quote(cwd), claude)
    if adapter == "apple-terminal":
        script = ('tell application "Terminal"\n activate\n do script "%s"\nend tell'
                  % _as_quote(cmd))
    else:
        script = ('tell application "iTerm"\n activate\n set w to (create window '
                  'with default profile)\n tell current session of w to write text '
                  '"%s"\nend tell' % _as_quote(cmd))
    return Plan(["osascript", "-e", script], shown, None, term, shell)


def clean_env(env=None, persistent=None):
    """os.environ minus everything Claude Code scoped to THIS session.

    A hook runs as a child of claude.exe and inherits its session markers, so a
    plain Popen hands them to the new window: CLAUDE_CODE_CHILD_SESSION (the new
    session sees itself as a child and SILENTLY STOPS SAVING ITS TRANSCRIPT),
    CLAUDE_CODE_SESSION_ID, CLAUDE_PID and CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN
    (this session's identity, pid and message pipe).

    Everything CLAUDE* goes, not a denylist of known markers: the set is the
    client's to change, and a new marker would fail the same silent way. Nothing
    legitimate is lost — the new claude re-applies settings.json's env block at
    startup, and the shell we launch runs the user's profile.

    AGENT SHELLS LEAK TOO: run from an agent's tool shell rather than from a hook,
    the launcher also passes on what Claude Code sets for its tools (NO_COLOR=1
    leaves the status line without colors). Those are standard tool variables with
    no shared prefix, so this half IS a denylist, bounded two ways: a variable goes
    only while it holds the value Claude Code injects (a user's
    GIT_EDITOR="code --wait" survives), and never
    when the user's persistent environment sets that name (Windows registry;
    elsewhere the login shell's profile restores it). AI_AGENT goes whatever its
    value: like CLAUDE*, it names the session that launched, not the new one.
    """
    env = os.environ if env is None else env
    if persistent is None:
        persistent = _persistent_env_names()
    out = {}
    for k, v in env.items():
        ku = k.upper()
        if ku.startswith("CLAUDE") or ku == "AI_AGENT":
            continue
        if AGENT_SHELL_VARS.get(ku) == v and ku not in persistent:
            continue
        out[k] = v
    return out


# What Claude Code's tool shells set (v2.1.292). The Bash tool sets only
# GIT_EDITOR and AI_AGENT; the PowerShell tool sets all of these. Keys
# upper-cased: Windows names are case-insensitive. NoDefaultCurrentDirectoryInExePath is left alone on
# purpose: it hardens executable lookup, and removing it gains nothing.
AGENT_SHELL_VARS = {
    "NO_COLOR": "1",
    "GIT_EDITOR": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    # Outlives claude: the -NoExit PowerShell left in the tab would keep it.
    "PSEXECUTIONPOLICYPREFERENCE": "Bypass",
}


def _persistent_env_names():
    """Upper-cased names the user's persistent environment sets — the Windows
    registry, user and machine. Empty elsewhere, and on any read failure: the
    worst case is then the one this guard narrows (a user who set NO_COLOR=1
    globally gets colors in a launched window), never a failed launch."""
    if not WINDOWS:
        return set()
    names = set()
    try:
        import winreg
        for hive, path in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                           (winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, path) as key:
                    i = 0
                    while True:
                        try:
                            names.add(winreg.EnumValue(key, i)[0].upper())
                        except OSError:
                            break
                        i += 1
            except OSError:
                continue
    except Exception:
        return set()
    return names


def spawn(p, env=None):
    """Fire and forget. Returns (ok, error_text)."""
    if not p.ok:
        return False, p.why
    if p.script:
        try:
            _write_launch_script(*p.script)
        except OSError as e:
            return False, "could not write the launch file %s: %s" % (p.script[0], e)
    kw = {"close_fds": True, "env": clean_env(env)}
    if p.cwd:
        kw["cwd"] = p.cwd
    if not p.new_console:
        # The handoff cases (wt, AppleScript): our child is a launcher that exits
        # immediately, so its streams are noise and go to the null device.
        kw.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
    # ... whereas a process we gave its OWN console must keep its stdio: redirect
    # it and the new window is a blank rectangle with an invisible claude in it.
    try:
        if WINDOWS:
            DETACHED, NEW_CONSOLE = 0x00000008, 0x00000010
            NEWGRP, BREAKAWAY = 0x00000200, 0x01000000
            base = NEWGRP | (NEW_CONSOLE if p.new_console else DETACHED)
            try:
                subprocess.Popen(p.argv, creationflags=base | BREAKAWAY, **kw)
            except OSError:
                # The job object may forbid breakaway; the handoff (or the new
                # console) already detaches us, so drop the flag rather than fail.
                subprocess.Popen(p.argv, creationflags=base, **kw)
        else:
            subprocess.Popen(p.argv, start_new_session=True, **kw)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return True, ""
