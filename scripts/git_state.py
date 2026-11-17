#!/usr/bin/env python3
"""The status line's cwd git facts from ONE `git status`.

One porcelain call instead of several git spawns, since process creation is the
cost on Windows. `parse()` is the counting rule, kept pure so the tests can hold
it against the separate git calls.

GIT_OPTIONAL_LOCKS=0: a plain `git status` refreshes the index and takes
.git/index.lock to do it, so a render could make the user's own commit in
another window fail on "index.lock exists". The variable is git's switch for
background callers; a git older than 2.15 ignores it rather than failing, which
the `--no-optional-locks` flag would not.
"""
import os
import re
import subprocess

_BRACKET = re.compile(r"\s\[([^\]]*)\]$")
_UNBORN = ("No commits yet on ", "Initial commit on ")   # current git, then older git


def parse(text):
    """{branch, upstream, ahead, behind, untracked, modified, staged} from the
    output of `git status --porcelain=v1 -b --untracked-files=all`.

    branch is "" on a detached HEAD. upstream is False when none is configured
    or it is gone, and ahead/behind are then 0. Counts follow the separate git
    calls: untracked = `ls-files --others --exclude-standard`, modified = `diff
    --name-only` (worktree vs index), staged = `diff --cached --name-only`."""
    out = {"branch": "", "upstream": False, "ahead": 0, "behind": 0,
           "untracked": 0, "modified": 0, "staged": 0}
    lines = text.splitlines()
    if lines and lines[0].startswith("## "):
        head = lines.pop(0)[3:]
        m = _BRACKET.search(head)
        info = m.group(1) if m else ""
        if m:
            head = head[:m.start()]
        for p in _UNBORN:
            if head.startswith(p):
                head = head[len(p):]
        if head.startswith("HEAD (no branch)"):
            head = ""
        name, sep, _up = head.partition("...")
        out["branch"] = name.strip()
        if sep and info != "gone":
            out["upstream"] = True
            a = re.search(r"ahead (\d+)", info)
            b = re.search(r"behind (\d+)", info)
            out["ahead"] = int(a.group(1)) if a else 0
            out["behind"] = int(b.group(1)) if b else 0
    for ln in lines:
        if len(ln) < 3:
            continue
        x, y = ln[0], ln[1]
        if x == "?" and y == "?":
            out["untracked"] += 1
            continue
        if x == "!" and y == "!":
            continue                      # ignored files; only with --ignored
        if x not in " ?!":
            out["staged"] += 1
        if y not in " ?!":
            out["modified"] += 1
    return out


def _run(args, cwd, timeout):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    # UTF-8 rather than the locale codec: a non-ASCII branch name in the status header.
    return subprocess.run(["git"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=cwd, timeout=timeout, env=env)


def read(cwd, timeout=5):
    """None outside a repo; else parse()'s dict plus counts=True|False.

    A status that fails INSIDE a repo (cwd within .git, a bare repo, a
    timeout) falls back to repo + branch with no counts. Costs one spawn in a
    repo and one outside it; the fallback's two only on that failure."""
    try:
        r = _run(["status", "--porcelain=v1", "-b", "--untracked-files=all"],
                 cwd, timeout)
        if r.returncode == 0:
            res = parse(r.stdout)
            res["counts"] = True
            return res
        if "not a git repository" in (r.stderr or "").lower():
            return None
    except Exception:
        pass
    try:
        if _run(["rev-parse", "--git-dir"], cwd, timeout).returncode != 0:
            return None
        b = _run(["branch", "--show-current"], cwd, timeout)
        res = parse("")
        res["branch"] = b.stdout.strip() if b.returncode == 0 else ""
        res["counts"] = False
        return res
    except Exception:
        return None
