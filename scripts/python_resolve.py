#!/usr/bin/env python3
"""Which Python runs claudette's hooks and status line — resolved ONCE per
machine, stored as the `python` parameter, rendered as an absolute path.

Why an absolute path (CLAUDETTE_DESIGN.md, "Absolute interpreter path"): the
hook and statusline shells are not the agent's shell. The agent's Bash tool
sources the profile; hooks run a bare non-interactive shell; the statusline's
environment is undocumented. `$(command -v python3 || command -v python)`
therefore answers differently per context (an all-users Python not on the bare
PATH is found by the agent's shell and missed by the status line). An absolute
path is the same string in every context.

Every candidate is validated FROM A NON-LOGIN BASH (`bash --noprofile
--norc`), the closest documented approximation of the hook shell, and must
be >= 3.9 (I6) with `ssl` importable (the statusline fetches rate limits).

This module never writes prefs itself: `features_apply.py` decides and
records; `--doctor` reports. Import cost matters to nobody on the hot path —
the hooks receive the path, they never resolve it.
"""
import json
import os
import re
import shutil
import subprocess
import sys

# The bootstrap idiom — what renders when NO interpreter is stored yet, and
# what run.sh/pull.sh use to find an interpreter before prefs exist.
FALLBACK_CHAIN = "$(command -v python3 || command -v python)"

FLOOR = (3, 9)
PROBE_TIMEOUT = 20
IS_WIN = sys.platform == "win32"

# What a candidate must prove, printed as one JSON line by the candidate itself.
_PROBE = ("import json,sys,os;"
          "ok=True\n"
          "try:\n import ssl\nexcept Exception:\n ok=False\n"
          "p=sys.prefix;"
          "print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:3]),"
          "'ssl':ok,'venv':sys.prefix!=getattr(sys,'base_prefix',sys.prefix),"
          "'conda':os.path.isdir(os.path.join(p,'conda-meta'))}))")


def norm(path):
    """Storage form: native path, forward slashes (safe inside a JSON string
    and inside a double-quoted bash word on both platforms)."""
    if not path:
        return ""
    p = str(path).strip().strip('"').strip("'")
    if IS_WIN:
        p = p.replace("\\", "/")
    return p


def _same(a, b):
    """Same interpreter file? Case-insensitive on Windows, via realpath."""
    try:
        ra, rb = os.path.realpath(a), os.path.realpath(b)
    except Exception:
        return False
    return ra.lower() == rb.lower() if IS_WIN else ra == rb


def is_store_shim(path):
    """Windows 11 App Execution Alias for python*.exe: exists, `command -v`
    finds it, running it prints "Python was not found" and exits 9009."""
    return IS_WIN and "windowsapps" in norm(path).lower()


def _bash():
    """The bash Claude Code runs hooks with. On Windows that is Git Bash —
    never WSL's C:/Windows/System32/bash.exe, which `which` may find first
    and which sees a different filesystem and PATH entirely."""
    if IS_WIN:
        for cand in (os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"),
                     r"C:\Program Files\Git\bin\bash.exe",
                     r"C:\Program Files (x86)\Git\bin\bash.exe",
                     os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "Programs", "Git", "bin", "bash.exe")):
            if cand and os.path.isfile(cand):
                return cand
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        return None
    return shutil.which("bash")


def validate(path):
    """Run `path` from a non-login bash and read what it says about itself.
    Returns a dict: {ok, path, reason, version, venv, conda, executable}.
    `reason` is the human-readable verdict when not ok."""
    out = {"ok": False, "path": norm(path), "reason": "", "version": None,
           "venv": False, "conda": False, "executable": ""}
    p = out["path"]
    if not p:
        out["reason"] = "no interpreter stored"
        return out
    if is_store_shim(p):
        out["reason"] = ("Microsoft Store alias, not a Python install — "
                         "disable it in Settings > Apps > App execution aliases, "
                         "or install Python and add it to PATH")
        return out
    if not os.path.isfile(p):
        out["reason"] = "file does not exist"
        return out
    bash = _bash()
    try:
        if bash:
            # $0/$1 positional hand-off: no quoting of the path or the probe
            # inside the bash string, whatever characters they carry.
            r = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", '"$0" -c "$1"', p, _PROBE],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        else:
            r = subprocess.run([p, "-c", _PROBE], capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        out["reason"] = f"did not answer within {PROBE_TIMEOUT}s"
        return out
    except OSError as e:
        out["reason"] = f"cannot execute: {e}"
        return out
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        out["reason"] = "exit %d%s" % (r.returncode, f": {err[-1][:120]}" if err else "")
        return out
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        out["reason"] = "unexpected output from the probe"
        return out
    ver = tuple(info.get("version") or ())
    out.update(version=ver, venv=bool(info.get("venv")), conda=bool(info.get("conda")),
               executable=norm(info.get("executable")))
    if ver < FLOOR:
        out["reason"] = "Python %s is below the 3.9 floor" % ".".join(map(str, ver))
        return out
    if not info.get("ssl"):
        out["reason"] = ("`import ssl` fails when run outside its activation "
                        "(typical of conda on Windows) — the status line could not "
                        "fetch rate limits")
        return out
    out["ok"] = True
    return out


def _from_bash_lookup():
    """What the bootstrap chain resolves to in a NON-LOGIN bash — the hook
    context's own answer, for both names. Empty entries dropped."""
    bash = _bash()
    if not bash:
        return []
    found = []
    for name in ("python3", "python"):
        try:
            # The NAME bash resolves, not what the interpreter says about
            # itself: /opt/homebrew/bin/python3 is the stable symlink, the
            # Cellar path behind it changes on every brew upgrade. cygpath -m
            # turns Git Bash's /c/... into the native C:/... form.
            r = subprocess.run(
                [bash, "--noprofile", "--norc", "-c",
                 'p="$(command -v "$0" || true)"; [ -n "$p" ] || exit 1; '
                 'if command -v cygpath >/dev/null 2>&1; then cygpath -m "$p"; '
                 'else printf "%s\\n" "$p"; fi', name],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT)
            line = (r.stdout or "").strip().splitlines()
            if r.returncode != 0 or not line:
                continue
            p = norm(line[-1])
            if IS_WIN and not os.path.isfile(p) and os.path.isfile(p + ".exe"):
                p += ".exe"
            found.append(p)
        except Exception:
            continue
    return found


def _from_py_launcher():
    """Windows `py --list-paths`: every registered install, one line each."""
    if not IS_WIN or not shutil.which("py"):
        return []
    try:
        r = subprocess.run(["py", "--list-paths"], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        m = re.search(r"([A-Za-z]:\\.*python\w*\.exe)\s*$", line.strip(), re.I)
        if m:
            out.append(norm(m.group(1)))
    return out


def candidates():
    """Ordered, de-duplicated: what a bare bash finds, the interpreter running
    this process, then the platform's registered/usual installs."""
    # Bash's answer first: when it is the same file as sys.executable, its
    # name is the stable one (/opt/homebrew/bin/python3, not the versioned
    # Cellar path a brew upgrade replaces).
    raw = _from_bash_lookup() + [norm(sys.executable)]
    if IS_WIN:
        raw += _from_py_launcher()
    else:
        raw += ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    out = []
    for p in raw:
        if p and os.path.isfile(p) and not any(_same(p, q) for q in out):
            out.append(p)
    return out


def rank(valid):
    """Best first: plain installs before venv/conda (they vanish with their
    project or need activation); otherwise candidates() order — what a bare
    bash finds, then this process, then the platform's registered installs."""
    return sorted(valid, key=lambda v: bool(v["venv"] or v["conda"]))


def resolve():
    """Probe every candidate. Returns (best or None, [valid], [rejected])."""
    valid, rejected = [], []
    for p in candidates():
        v = validate(p)
        (valid if v["ok"] else rejected).append(v)
    ranked = rank(valid)
    return (ranked[0] if ranked else None), ranked, rejected


def render(stored):
    """The interpreter word for a hook/statusline command: the stored absolute
    path, double-quoted — or the bootstrap chain when nothing usable is stored
    (so an unresolved machine is no worse off)."""
    p = norm(stored)
    if p and os.path.isfile(p) and not is_store_shim(p):
        return '"%s"' % p
    return FALLBACK_CHAIN


if __name__ == "__main__":
    best, valid, rejected = resolve()
    print(json.dumps({"best": best, "valid": valid, "rejected": rejected}, indent=2))
