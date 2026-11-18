#!/usr/bin/env python3
"""update_screen.py pull <repo> | screen <repo> <from> <to> | advance <sha>

Screens an update of an INSTALL before it lands, mechanically: no model, no network
beyond the fetch. pull.sh calls `pull` for the claudette folder of an install; a
development clone keeps its plain pull.

  pull <repo>     fetch; when the upstream commit descends from HEAD, screen HEAD..upstream.
                  Clean: fast-forward to that exact commit (exit 0). A finding: change
                  nothing, record the hold in logs/update_hold.json (exit 5). Not a fast-
                  forward, no upstream, a failed fetch: git's message, exit 1.
  screen <repo> <from> <to>
                  print the findings as JSON, change nothing.
  advance <sha>   fast-forward this install to <sha>, only when <sha> is the commit the
                  recorded hold names and it descends from HEAD; then clear the hold.

What counts as a finding: what an update could make run, or change, without a new
question to the user.
  - update-path: a change to the update path itself (pull.sh, this script, run.sh,
    pyfind.sh), which judges the update after this one;
  - dev-marker: CLAUDETTE_DEV.md arriving, which would switch this install to the
    unscreened pull of a development clone;
  - registry: a feature's payload changed (anything but its descriptive fields), a new
    feature that inherits earlier answers (renamed_from), a change to the global params
    or zones. A plain new feature is not one: it waits for its consent question;
  - network, credentials, process, dynamic-code, deletion: an added line of code using
    one of these in a file whose previous version never did, written as code (an import,
    a call), not prose; in other text files, which an agent may follow, only a fetch, a
    pipe into a shell or a credential file; a URL to a host the previous version never
    named; deletion means bulk deletion (a tree, rm -r);
  - image-in-code: a code file that starts naming an image file, the one plain way to
    make an image run;
  - binary: a file that is not text, except a PNG, JPEG or GIF image whose first bytes
    match its extension: no script opens one, GitHub shows them in the README.
The screen runs from the INSTALLED copy, so the incoming commit never judges itself. It
catches the listed shapes and nothing else: a clean screen is not an audit.

Exit: 0 clean, advanced or up to date; 5 held; 1 failed; 2 usage.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

HOLD = paths.log("update_hold.json")
UPDATE_PATH = ("scripts/pull.sh", "scripts/update_screen.py", "scripts/run.sh", "scripts/pyfind.sh")
DEV_MARKERS = ("CLAUDETTE_DEV.md", "docs/CLAUDETTE_DEV.md")
DESCRIPTIVE = {"group", "name", "what", "benefit", "cost", "alternative", "recommend_note",
               "recommend_by", "default", "hooks_restart"}
GLOBAL_PAYLOAD = ("params", "zones")
# Code-shaped, so that prose naming the same things ("pull requests", "a message socket",
# "no subprocess") does not count. In code files every kind applies; in other text files
# (instructions an agent reads) only what a command line can do: fetch, pipe to a shell,
# touch a credential file.
CODE = (".py", ".sh", ".js", ".ps1", ".bat", ".cmd")
IMPORT = r"(?:^|\s)(?:import|from)\s+(?:[\w.]+\s*,\s*)*"
KINDS = [
    ("network", IMPORT + r"(?:urllib|http|requests|socket|ssl|ftplib|smtplib|websocket)\b|"
                r"\burlopen\(|\bhttp\.client\b|\brequests\.(?:get|post|put|request)\(|"
                r"\bsocket\.(?:socket|create_connection)\(|\bcurl\s|\bwget\s|"
                r"Invoke-(?:WebRequest|RestMethod)\b", True),
    ("credentials", r"credentials\.json|find-generic-password|SecKeychain|\bkeyring\b|\.netrc\b|"
                    r"id_rsa|\.ssh/|GITHUB_TOKEN|ANTHROPIC_API_KEY", True),
    ("process", IMPORT + r"subprocess\b|\bsubprocess\.\w+\(|\bPopen\(|"
                r"\bos\.(?:system|popen|exec\w*|spawn\w*|kill)\(|\btaskkill\b|Stop-Process\b|"
                r"Start-Process\b|\bpkill\b|\bkillall\b|ShellExecute\w*\(|CreateProcess\w*\(",
     False),
    ("dynamic-code", r"(?<![\w.])(?:eval|exec)\s*\(|(?<![\w.])compile\s*\(|" + IMPORT +
                     r"(?:base64|marshal|pickle|ctypes|importlib)\b|__import__\(|"
                     r"\|\s*(?:ba)?sh\b|\biex\b", True),
    ("deletion", r"\bshutil\.rmtree\(|\brm\s+-[A-Za-z]*r[A-Za-z]*\s|Remove-Item\b[^\n]*-Recurse",
     False),
    ("image-in-code", r"\.(?:png|jpe?g|gif)\b", False),
]
KINDS = [(k, re.compile(rx), anywhere) for k, rx, anywhere in KINDS]
URL = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def git(repo, *args, binary=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    r = subprocess.run(["git", "-c", "color.ui=false", "-C", repo, *args],
                       capture_output=True, env=env)
    out = r.stdout if binary else r.stdout.decode("utf-8", "replace")
    return r.returncode, out, r.stderr.decode("utf-8", "replace")


def show(repo, rev, path):
    rc, out, _ = git(repo, "show", "%s:%s" % (rev, path), binary=True)
    return out if rc == 0 else None


def _is_text(data):
    return b"\0" not in data[:8192]


IMAGES = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",),
          ".jpeg": (b"\xff\xd8\xff",), ".gif": (b"GIF87a", b"GIF89a")}


def _is_image(path, data):
    """Extension and first bytes agree on an image: neither a renamed file nor stray bytes."""
    sigs = IMAGES.get(os.path.splitext(path)[1].lower())
    return bool(sigs) and data.startswith(sigs)


def _registry(repo, rev):
    data = show(repo, rev, "features_def.json")
    if data is None:
        return None
    return json.loads(data.decode("utf-8"))


def registry_findings(repo, a, b):
    old, new = _registry(repo, a), _registry(repo, b)
    if new is None:
        return []
    old = old or {}
    found = []
    of, nf = old.get("features") or {}, new.get("features") or {}
    for fid, f in nf.items():
        if fid not in of:
            # A new feature waits for its consent question, unless it inherits an answer.
            old_ids = f.get("renamed_from") or []
            old_ids = [old_ids] if isinstance(old_ids, str) else list(old_ids)
            if old_ids:
                found.append({"kind": "registry", "file": "features_def.json",
                              "detail": "new feature %s carries the answers of %s"
                                        % (fid, ", ".join(old_ids))})
            continue
        keys = sorted(k for k in set(f) | set(of[fid])
                      if k not in DESCRIPTIVE and f.get(k) != of[fid].get(k))
        if keys:
            found.append({"kind": "registry", "file": "features_def.json",
                          "detail": "feature %s: %s changed" % (fid, ", ".join(keys))})
    for key in GLOBAL_PAYLOAD:
        if new.get(key) != old.get(key):
            found.append({"kind": "registry", "file": "features_def.json",
                          "detail": "global %s changed" % key})
    return found


def screen(repo, a, b, keep=None):
    """Findings for the update a..b. `keep(path)` limits the files looked at (a test or a
    measurement over a development history; an install looks at everything)."""
    rc, names, err = git(repo, "diff", "--name-status", "--no-renames", a, b)
    if rc != 0:
        raise RuntimeError("git diff failed: %s" % err.strip())
    found = []
    changed = []
    for line in names.splitlines():
        status, _, path = line.partition("\t")
        if not path or (keep and not keep(path)):
            continue
        changed.append((status[:1], path))
    for status, path in changed:
        if path in UPDATE_PATH:
            found.append({"kind": "update-path", "file": path,
                          "detail": "the update path changed: it judges the next update"})
        if status == "A" and path in DEV_MARKERS:
            found.append({"kind": "dev-marker", "file": path,
                          "detail": "would switch this install to an unscreened pull"})
    if any(p == "features_def.json" for _s, p in changed):
        found += registry_findings(repo, a, b)
    hosts_seen = {}
    for status, path in changed:
        if status == "D":
            continue
        new = show(repo, b, path)
        if new is None or _is_image(path, new):
            continue
        if not _is_text(new):
            found.append({"kind": "binary", "file": path,
                          "detail": "neither text nor an image: ask the user"})
            continue
        old = show(repo, a, path) if status != "A" else None
        old_text = old.decode("utf-8", "replace") if old is not None and _is_text(old) else ""
        rc, diff, _ = git(repo, "diff", "-U0", "--no-renames", a, b, "--", path)
        added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        code = path.lower().endswith(CODE)
        for kind, rx, anywhere in KINDS:
            if not (code or anywhere) or rx.search(old_text):
                continue
            hit = next((l for l in added if rx.search(l)), None)
            if hit is not None:
                found.append({"kind": kind, "file": path, "detail": hit.strip()[:160]})
        for l in added:
            for host in URL.findall(l):
                if host not in hosts_seen:
                    rc, _o, _e = git(repo, "grep", "-q", "-F", host, a)
                    hosts_seen[host] = (rc == 0)
                if not hosts_seen[host]:
                    hosts_seen[host] = True
                    found.append({"kind": "network", "file": path,
                                  "detail": "a host the installed version never named: %s" % host})
    return found


def _write_hold(hold):
    os.makedirs(os.path.dirname(HOLD), exist_ok=True)
    tmp = HOLD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(hold, fh, indent=1)
    os.replace(tmp, HOLD)


def _clear_hold():
    try:
        os.remove(HOLD)
    except OSError:
        pass


def read_hold():
    """The recorded hold, or None."""
    try:
        with open(HOLD, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def current_hold(repo=None):
    """The recorded hold while it still applies: the install is still at the commit it was
    screened from (a reinstall or a manual update leaves a stale file behind). For the
    update plan."""
    hold = read_hold()
    if not hold:
        return None
    rc, head, _ = git(repo or paths.ROOT, "rev-parse", "HEAD")
    return hold if rc == 0 and head.strip() == hold.get("from") else None


def _ff(repo, sha):
    rc, out, err = git(repo, "merge", "--ff-only", sha)
    sys.stdout.write(out)
    sys.stdout.write(err)
    return rc


def cmd_pull(repo):
    rc, out, err = git(repo, "fetch")
    sys.stdout.write(out + err)
    if rc != 0:
        return 1
    rc, up, err = git(repo, "rev-parse", "--verify", "@{u}")
    if rc != 0:
        print("fatal: no upstream branch to update from: %s" % err.strip())
        return 1
    up = up.strip()
    rc, head, _ = git(repo, "rev-parse", "HEAD")
    head = head.strip()
    if up == head:
        _clear_hold()
        print("Already up to date.")
        return 0
    rc, _o, _e = git(repo, "merge-base", "--is-ancestor", head, up)
    if rc != 0:
        print("fatal: Not possible to fast-forward, aborting. (%s is not a descendant of the "
              "installed %s: the repository moved or its history was rewritten)"
              % (up[:12], head[:12]))
        return 1
    try:
        found = screen(repo, head, up)
    except Exception as e:                      # a screen that cannot run holds
        found = [{"kind": "screen-error", "file": "", "detail": str(e)[:200]}]
    if not found:
        _clear_hold()
        print("update screened: nothing to flag in %s..%s" % (head[:12], up[:12]))
        return _ff(repo, up)
    _write_hold({"from": head, "to": up, "findings": found})
    print("update held: %d finding(s) in %s..%s, nothing changed on disk" % (len(found), head[:12], up[:12]))
    for f in found:
        print("  %s  %s  %s" % (f["kind"], f["file"], f["detail"]))
    return 5


def cmd_advance(sha, repo=None):
    repo = repo or paths.ROOT
    hold = current_hold(repo)
    if not hold:
        print("advance: no update is held", file=sys.stderr)
        return 2
    rc, full, _ = git(repo, "rev-parse", "--verify", sha + "^{commit}")
    if rc != 0 or full.strip() != hold.get("to"):
        print("advance: %s is not the held commit %s" % (sha, (hold.get("to") or "?")[:12]),
              file=sys.stderr)
        return 2
    rc, _o, _e = git(repo, "merge-base", "--is-ancestor", "HEAD", hold["to"])
    if rc != 0:
        print("advance: the held commit does not descend from HEAD", file=sys.stderr)
        return 2
    rc = _ff(repo, hold["to"])
    if rc == 0:
        _clear_hold()
    return 0 if rc == 0 else 1


def main(argv):
    if len(argv) >= 2 and argv[0] == "pull":
        return cmd_pull(argv[1])
    if len(argv) == 4 and argv[0] == "screen":
        print(json.dumps(screen(argv[1], argv[2], argv[3]), indent=1))
        return 0
    if len(argv) == 2 and argv[0] == "advance":
        return cmd_advance(argv[1])
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
