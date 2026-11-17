#!/usr/bin/env python3
"""One-shot read-only probe for the `claudette-done` skill.

Single purpose: collect, in ONE tool call, every mechanical fact that skill needs
so the agent does not pay a model round trip per `git`/`ls` it would otherwise
issue by hand. Batching the facts is the lever; the skill's remaining round trips
are judgment, which is the product.

A --repo below a repo root resolves upward (reported as `resolved_from`); a --repo
that is in no repo at all gets a bounded scan for CHILD repos (`subrepos`) instead
of a blind "no git" verdict - an untracked folder over tracked subfolders is not
"no git".

ANTI-GOALS (do not grow this file into "the skill in Python"):
  - never write, stage, commit, move or delete anything - read-only, always
  - never decide what is *key*, what is a deliverable, or what deserves a mark;
    those are judgments and they belong to the model reading this output
  - never build TOUCHED. Attribution comes from the agent's own tool history;
    mtimes cannot distinguish our write from a concurrent one (see the skill)
  - never read the CONTENTS of another session's scratchpad file. Filenames and sizes are
    metadata and ship by default (`--no-sibling-names` opts out); contents never do
  - never filter anything out by AGE. Age goes in the description so a human can judge it;
    an age cutoff would turn "I did not look" into a reported "nothing to adopt"
  - never read a memory file's body - frontmatter only, to get `type`
  - no network of our own: upstream staleness is delegated by SUBPROCESS to
    git_upstreams.py, which owns that cache, its TTL and its kill switch. Called
    here only so the skill's preflight costs one round trip instead of two;
    never reimplemented, never cached again on this side.

CLI:
    python done_probe.py [--repo DIR]... [--session ID] [--pretty]
                         [--no-sibling-names] [--memory-cap N]
`--repo` REPEATS - pass every repo root the session touched in ONE call. The first
is primary (keeps the `repo`/`upstream_check` keys and sets `project_key`); the rest
appear under `extra_repos`, which is absent when only one repo is given.
Prints one JSON object. Every section is independently fallible: a section that
cannot be read reports {"error": "..."} rather than failing the whole probe, so
a missing memory dir never costs the agent its git picture.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

GIT_EXE = shutil.which("git")     # None -> every git fact degrades, and says so

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

HOME = paths.HOME
LOGS = paths.LOGS
MEMORY_CAP = 15           # entries whose frontmatter we read; above this, report unchecked
GIT_TIMEOUT = 10
SUBREPO_DEPTH = 3         # levels below an untracked folder searched for child repos
SUBREPO_STATUS_CAP = 10   # child repos that get a status summary; the rest list path-only
SUBREPO_FIND_CAP = 200    # discovery stops here; a folder of hundreds is its own answer
SUBREPO_TIME_S = 10       # wall-clock budget for the whole scan
SUBREPO_SKIP = {".git", "node_modules", "__pycache__", "venv", "$RECYCLE.BIN",
                "System Volume Information"}
STALE_MIN = 10            # status-line age past which the skill must print an anchor
LIVE_WINDOW_MIN = 15      # a session whose statusline snapshot is newer than this looks LIVE
NAME_CAP = 40             # filenames listed per unowned scratchpad; overflow is counted, not hidden
GROUP_MAX = 6             # dirty-file groups before rolling the remainder into "elsewhere"


def _git(root, *args):
    """Run git read-only. Returns (ok, stdout). Never prompts, never mutates."""
    if not GIT_EXE:
        return False, "git not installed"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        # UTF-8 rather than the locale codec: git_hardened.run() says why.
        p = subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT, env=env)
        return p.returncode == 0, p.stdout
    except Exception as e:
        return False, str(e)


def project_key(path):
    """Claude Code's project-dir slug: every non-alphanumeric char becomes '-'.

    D:/_data/notes/trip   -> D---data-notes-trip
    C:/Users/me/.claude   -> C--Users-me--claude
    """
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path).replace("\\", "/"))


def _age(sec):
    """Human age from an epoch seconds value, coarse on purpose - this is triage."""
    d = max(0, int(time.time() - sec))
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % (d // 60)
    if d < 172800:
        return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)


# --------------------------------------------------------------------------- git

def probe_repo(root):
    out = {"root": root}
    if not GIT_EXE:
        # Without git, "not a repo" would be a lie - we cannot know. Repos below are
        # still detectable by their .git marker; nothing about them is verifiable.
        out = dict(scan_subrepos(root), root=root, is_git=None, git_missing=True)
        out["note"] = ("git is not installed or not on PATH; repos below are detected "
                       "by their .git marker only and none of C/R/P can be verified")
        return out
    ok, top = _git(root, "rev-parse", "--show-toplevel")
    if not ok:
        # Not a repo is NOT the same as "no git": a child folder can be one.
        out = dict(scan_subrepos(root), root=root, is_git=False)
        out["note"] = (("this folder is untracked but %d subfolder repo(s) are not - "
                        "C/R/P apply per subrepo; re-probe each one this session touched")
                       % out["subrepos_found"] if out["subrepos_found"] else
                       "no git tracking here; the numbered slots still apply")
        return out
    out["is_git"] = True
    out["root"] = top.strip() or root
    if os.path.normcase(out["root"]) != os.path.normcase(
            os.path.abspath(root).replace("\\", "/")):
        out["resolved_from"] = root       # the folder given sits below the repo root
    root = out["root"]

    ok, st = _git(root, "status", "--porcelain=v1", "-b", "-uall")
    if not ok:
        return dict(out, error="git status failed: %s" % st[:200])

    dirty = []
    for line in st.splitlines():
        if line.startswith("## "):
            head = line[3:]
            out["branch_line"] = head
            out["detached"] = head.startswith("HEAD (no branch)")
            if head.startswith("No commits yet on "):  # unborn branch, zero commits
                head = head[len("No commits yet on "):]
            m = re.match(r"([^.]+)\.\.\.(\S+)", head)
            if m:
                out["branch"], out["upstream"] = m.group(1), m.group(2)
            else:
                out["branch"] = head.split()[0] if head else None
                out["upstream"] = None
            a = re.search(r"ahead (\d+)", head)
            b = re.search(r"behind (\d+)", head)
            out["ahead"] = int(a.group(1)) if a else 0
            out["behind"] = int(b.group(1)) if b else 0
            continue
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        if " -> " in path:                      # rename: the destination is what exists
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        full = os.path.join(root, path)
        try:
            mt = os.stat(full).st_mtime
        except OSError:
            mt = None
        dirty.append({"xy": xy, "path": path, "staged": xy[0] not in " ?",
                      "mtime": mt, "age": _age(mt) if mt else None})
    out["dirty_count"] = len(dirty)
    out["dirty"] = dirty
    out["staged_count"] = sum(1 for d in dirty if d["staged"])
    out["dirty_groups"] = group_dirty(dirty)

    gitdir = os.path.join(root, ".git")
    ok, gd = _git(root, "rev-parse", "--git-dir")
    if ok and gd.strip():
        gitdir = gd.strip() if os.path.isabs(gd.strip()) else os.path.join(root, gd.strip())
    inprog = [n for n, rel in (("merge", "MERGE_HEAD"), ("rebase", "rebase-merge"),
                               ("rebase", "rebase-apply"), ("cherry-pick", "CHERRY_PICK_HEAD"),
                               ("revert", "REVERT_HEAD"), ("bisect", "BISECT_LOG"))
              if os.path.exists(os.path.join(gitdir, rel))]
    out["in_progress"] = sorted(set(inprog))

    ok, sm = _git(root, "submodule", "status", "--recursive")
    out["dirty_submodules"] = [l.strip() for l in sm.splitlines() if l[:1] in "+-U"] if ok else []

    # Repos nested inside this repo that are NOT registered submodules: git status shows
    # at most one opaque dir for them (nothing at all if gitignored), so the outer repo's
    # C/R/P never cover their content. Registered submodules are excluded - reported above.
    sub_paths = {l.split()[1] for l in sm.splitlines() if len(l.split()) >= 2} if ok else set()

    # Extra worktrees: git registers every one in this repo's .git/worktrees, so this
    # catches them WHEREVER they live - hidden folders, temp dirs - which no filesystem
    # scan can promise. Worktree-isolated agents auto-remove theirs only when unchanged;
    # a changed or crashed one persists, and its dirt is uncommitted work of this repo.
    ok, wt = _git(root, "worktree", "list", "--porcelain")
    out["worktrees"] = []
    if ok:
        blocks, cur = [], {}
        for line in wt.splitlines() + [""]:
            if not line.strip():
                if cur:
                    blocks.append(cur)
                cur = {}
                continue
            k, _, v = line.partition(" ")
            cur[k] = v if v else True
        for w in blocks[1:]:                       # block 0 is always this repo itself
            rec = {"path": w.get("worktree"),
                   "branch": ("detached" if "detached" in w else
                              (w.get("branch") or "").replace("refs/heads/", "") or None)}
            for flag in ("locked", "prunable"):
                if flag in w:
                    rec[flag] = w[flag]
            if len(out["worktrees"]) < SUBREPO_STATUS_CAP and rec["path"] \
                    and os.path.isdir(rec["path"]):
                rec.update(subrepo_summary(rec["path"]))
            out["worktrees"].append(rec)

    # Registered worktrees inside the tree would double-report as nested repos - one
    # repo under two names invites acting on it twice. Exclude them, like submodules.
    for w in out["worktrees"]:
        if w.get("path"):
            r = os.path.relpath(w["path"], root).replace("\\", "/")
            if not r.startswith(".."):
                sub_paths.add(r)
    nested = scan_subrepos(root, exclude=sub_paths)
    out["nested_repos_found"] = nested["subrepos_found"]
    out["nested_repos"] = nested["subrepos"]
    out["nested_scan"] = nested["scan"]

    # Other branches are deliberately NOT probed. A local branch is committed, so it
    # survives everything leaving a session can do to it; whether it is meant to reach
    # a remote is a fact git does not store: a backup branch kept locally on purpose would
    # come out as commits to push, with a push recommendation.

    ok, log = _git(root, "log", "--oneline", "-5")
    out["recent_commits"] = log.splitlines() if ok else []
    if out.get("ahead"):
        ok, un = _git(root, "log", "--oneline", "@{upstream}..HEAD")
        out["unpushed"] = un.splitlines() if ok else []
    return out


def scan_subrepos(root, exclude=frozenset()):
    """Repos below a folder, whether or not the folder itself is one. Bounded on every
    axis - depth, count, wall clock - and each cap discloses itself when it bites: a
    silent cap would turn "did not look" into a reported "nothing there". A repo
    found is walked into like any folder, under the same caps: a repo nested in it is
    invisible to its git status, and no other probe run looks there unless the
    session touched it. `exclude` takes
    root-relative paths (forward slashes) to prune without reporting or walking -
    registered submodules and worktrees, which probe_repo already carries."""
    t0 = time.time()
    repos, capped = [], []
    root_abs = os.path.abspath(root)
    for dirpath, dirs, _files in os.walk(root_abs):
        rel = os.path.relpath(dirpath, root_abs).replace("\\", "/")
        if time.time() - t0 > SUBREPO_TIME_S:
            capped.append("time")
            break
        # .git may be a dir or, in worktrees and submodules, a file - exists() takes both
        if dirpath != root_abs and os.path.exists(os.path.join(dirpath, ".git")):
            if rel in exclude:
                dirs[:] = []
                continue
            repos.append(rel)
            if len(repos) >= SUBREPO_FIND_CAP:
                capped.append("count")
                break
        # A hidden folder can BE a repo root (~/.claude is one) - so it gets the .git
        # check above like any other - but a hidden non-repo is never walked into.
        elif dirpath != root_abs and os.path.basename(dirpath).startswith("."):
            dirs[:] = []
            continue
        depth = 0 if rel == "." else rel.count("/") + 1
        if depth >= SUBREPO_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SUBREPO_SKIP]
    repos.sort(key=lambda p: (p.count("/"), p))     # shallowest first get the status calls
    out = {"subrepos_found": len(repos), "subrepos": [],
           "subrepos_statused": min(len(repos), SUBREPO_STATUS_CAP) if GIT_EXE else 0,
           "scan": {"depth": SUBREPO_DEPTH, "capped": capped}}
    for i, rel in enumerate(repos):
        rec = {"path": rel}
        if i < SUBREPO_STATUS_CAP and GIT_EXE:
            rec.update(subrepo_summary(os.path.join(root_abs, rel)))
        out["subrepos"].append(rec)
    return out


def subrepo_summary(path):
    """One porcelain call per repo: enough to triage R/P, never enough to commit
    against - the skill re-probes a subrepo in full before acting in it. `behind`
    is vs the last-fetched ref; only git_upstreams.py verifies against the remote."""
    ok, st = _git(path, "status", "--porcelain=v1", "-b", "-uall")   # -uall: same count
    if not ok:                                                       # probe_repo would give
        return {"error": ("git status failed: %s" % st)[:120]}
    lines = st.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    m = re.match(r"([^.]+)\.\.\.(\S+)", head)
    a = re.search(r"ahead (\d+)", head)
    b = re.search(r"behind (\d+)", head)
    if head.startswith("No commits yet on "):     # unborn branch, zero commits
        head = head[len("No commits yet on "):]
    return {"branch": m.group(1) if m else (head.split()[0] if head else None),
            "upstream": m.group(2) if m else None,
            "ahead": int(a.group(1)) if a else 0,
            "behind": int(b.group(1)) if b else 0,
            "dirty": sum(1 for l in lines[1:] if len(l) >= 4)}


def group_dirty(dirty):
    """Group dirty paths by folder at the DEEPEST depth still under GROUP_MAX groups.

    Folder + age is what makes a human recognise their own work: `trip/alderaan 3d`
    and `trip/naboo 2m` are not the same news. Ages are a HINT, never proof of
    ownership - the skill says so and must keep saying so.
    """
    if not dirty:
        return []
    best = None
    for depth in range(1, 6):
        buckets = {}
        for d in dirty:
            parts = d["path"].split("/")[:-1]
            key = "/".join(parts[:depth]) + "/" if parts else "(repo root)"
            buckets.setdefault(key, []).append(d)
        if best is None or len(buckets) <= GROUP_MAX:
            best = buckets
        if len(buckets) > GROUP_MAX:
            break
    rows = []
    for key, items in best.items():
        mts = [i["mtime"] for i in items if i["mtime"]]
        newest = max(mts) if mts else None
        rows.append({"folder": key, "files": len(items),
                     "newest_mtime": newest, "newest_age": _age(newest) if newest else None})
    rows.sort(key=lambda r: (-(r["newest_mtime"] or 0), r["folder"]))
    if len(rows) > GROUP_MAX:
        tail = rows[GROUP_MAX:]
        rows = rows[:GROUP_MAX] + [{"folder": "elsewhere", "files": sum(r["files"] for r in tail),
                                    "newest_mtime": None, "newest_age": None,
                                    "rolled_up_groups": len(tail)}]
    return rows


# ----------------------------------------------------------------------- memory

def probe_memory(key, cap):
    d = os.path.join(HOME, ".claude", "projects", key, "memory")
    out = {"dir": d, "exists": os.path.isdir(d)}
    if not out["exists"]:
        return out
    try:
        names = sorted(n for n in os.listdir(d)
                       if n.endswith(".md") and n != "MEMORY.md")
    except OSError as e:
        return dict(out, error=str(e))
    out["count"] = len(names)
    out["index_present"] = os.path.isfile(os.path.join(d, "MEMORY.md"))
    entries, unchecked = [], []
    for n in names:
        if len(entries) >= cap:
            unchecked.append(n)
            continue
        meta = {"file": n, "type": None, "description": None}
        try:                                   # frontmatter ONLY - never the body
            with open(os.path.join(d, n), encoding="utf-8") as f:
                if f.readline().strip() != "---":
                    entries.append(dict(meta, error="no frontmatter"))
                    continue
                for _ in range(30):
                    line = f.readline()
                    if not line or line.strip() == "---":
                        break
                    m = re.match(r"\s*(type|description|name)\s*:\s*(.+?)\s*$", line)
                    if m:
                        meta[m.group(1)] = m.group(2)
        except OSError as e:
            meta["error"] = str(e)
        entries.append(meta)
    out["entries"] = entries
    out["type_counts"] = {}
    for e in entries:
        t = e.get("type") or "unknown"
        out["type_counts"][t] = out["type_counts"].get(t, 0) + 1
    out["capped"] = bool(unchecked)
    out["unchecked"] = unchecked          # the skill MUST print this when non-empty
    return out


# ------------------------------------------------------------------- scratchpad

HASHED_ROOT = re.compile(r"^[0-9a-f]{16}$")


def scratch_roots(key, session):
    """Every folder holding this project's session temp dirs, and why the answer may be partial.

    Two layouts exist. Older clients: `<temp>/claude/<project key>/<session>/scratchpad`.
    Newer ones (seen on Windows): `<temp>/claude/<16 hex digits>/<session>/...`,
    one folder per project, named by a rule that cannot be derived (no common hash of the
    path matches). So the new root is found through the one thing known for sure, the
    session id: whichever folder holds a child named after it is this project's root.

    Returns (roots, unknown). `unknown` is a sentence when hashed folders exist and none
    could be tied to this session - the siblings are then somewhere this probe cannot
    name, and an empty list must not be read as "nothing left behind".
    """
    base = os.path.join(tempfile.gettempdir(), "claude")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []
    roots, seen, found_self = [], set(), False

    def add(path):
        k = os.path.normcase(os.path.normpath(path))
        if k not in seen:
            seen.add(k)
            roots.append(path)

    if session:
        for n in names:
            if os.path.isdir(os.path.join(base, n, session)):
                add(os.path.join(base, n))
                found_self = True
    legacy = os.path.join(base, key)
    if os.path.isdir(legacy):
        add(legacy)
    unknown = None
    if not found_self and any(HASHED_ROOT.match(n) for n in names):
        unknown = ("this machine keeps session temp folders under <temp>/claude/<16 hex "
                   "digits>/, and %s; other sessions' leftovers were NOT checked"
                   % ("no folder there is named after session %s" % session if session
                      else "no --session was given to find this project's folder"))
    return roots, unknown


def probe_scratchpads(key, session, no_names):
    roots, unknown = scratch_roots(key, session)
    legacy = os.path.join(tempfile.gettempdir(), "claude", key)
    out = {"root": roots[0] if roots else legacy, "roots": roots, "exists": bool(roots),
           "unknown": unknown, "self": None, "siblings": [],
           "siblings_omitted": 0}   # empty AND not live: collapsed, see below
    for root in roots:
        _scan_scratch_root(root, session, no_names, out)
    # NO age cutoff: hiding old directories would make the skill report "nothing to
    # adopt" about directories it declined to open. Age belongs in the description,
    # never in the filter.
    out["unowned"] = [s for s in out["siblings"] if s["files"] and not s.get("likely_live")]
    out["live_siblings"] = [s["session"] for s in out["siblings"] if s.get("likely_live")]
    # Every sibling dir examined, collapsed ones included - what "N sibling dirs, all
    # empty or live" must count; `len(siblings)` omits the collapsed ones.
    out["siblings_total"] = len(out["siblings"]) + out["siblings_omitted"]
    return out


def _scan_scratch_root(root, session, no_names, out):
    try:
        children = sorted(os.listdir(root))
    except OSError as e:
        out.setdefault("errors", []).append("%s: %s" % (root, e))
        return
    for child in children:
        pad = os.path.join(root, child, "scratchpad")
        target = pad if os.path.isdir(pad) else os.path.join(root, child)
        if not os.path.isdir(target):
            continue
        files, newest, total = [], None, 0
        for dirpath, _dirs, names in os.walk(target):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    stt = os.stat(p)          # metadata only - contents never opened
                except OSError:
                    continue
                total += 1
                newest = max(newest or 0, stt.st_mtime)
                files.append({"path": os.path.relpath(p, target), "bytes": stt.st_size})
        rec = {"session": child, "dir": target, "files": total,
               "newest_mtime": newest, "newest_age": _age(newest) if newest else None}
        if session and child == session:
            prev = out["self"]
            if prev:     # the same session under both layouts: one record, nothing dropped
                rec["files"] += prev["files"]
                rec["also_dir"] = prev["dir"]
                if prev["newest_mtime"] and (prev["newest_mtime"] > (newest or 0)):
                    rec["newest_mtime"] = prev["newest_mtime"]
                    rec["newest_age"] = prev["newest_age"]
            out["self"] = rec
        else:
            rec.update(liveness(child))
            # Filenames and sizes are METADATA and ship by default: "21 files, chunk_*.js and
            # dump_*.json" is usually all the user needs to dismiss a whole directory without
            # anyone opening a byte. Contents are still never read, by anyone, ever.
            if total and not rec["likely_live"] and not no_names:
                rec["names"] = files[:NAME_CAP]
                if len(files) > NAME_CAP:
                    rec["names_capped"] = len(files) - NAME_CAP
            rec["signal"] = (
                "empty" if not total else
                "that session appears LIVE (statusline %s ago) - leave it alone" % rec["snapshot_age"]
                if rec["likely_live"] else
                "non-empty, no recent statusline: nobody appears to own this")
            # COLLAPSE the empty-and-not-live ones to a count. Every field of such a
            # record is already implied by its own emptiness, and a long-lived project
            # accumulates them without bound, which would push the probe output over
            # the inline limit and into a file. A record is kept when it is non-empty
            # OR live: `unowned` needs the first, and `live_siblings` needs the second
            # even at zero files, because a live window elsewhere is what makes "leave
            # it" the right call in slot R.
            if total or rec["likely_live"]:
                out["siblings"].append(rec)
            else:
                out["siblings_omitted"] = out.get("siblings_omitted", 0) + 1


def liveness(session_id):
    """Is that session still running? Best available hint, and still only a hint.

    claudette's statusline hook rewrites logs/sessions/<id>.json on EVERY render,
    so a recent `ts` means that session was on screen minutes ago. Far better than
    scratchpad mtime, which stays fresh for hours after a session dies.
    """
    p = os.path.join(LOGS, "sessions", "%s.json" % session_id)
    try:
        with open(p, encoding="utf-8") as f:
            ts = int(json.load(f).get("ts", 0) or 0)
    except Exception:
        return {"snapshot": False, "snapshot_age": None, "likely_live": False}
    age = int(time.time()) - ts if ts else None
    return {"snapshot": True, "snapshot_age": _age(ts) if ts else None,
            "likely_live": bool(age is not None and age < LIVE_WINDOW_MIN * 60)}


# ------------------------------------------------------------- status + throttle

def probe_status(session):
    def load(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    out = {}
    for name, path in (("session", os.path.join(LOGS, "sessions", "%s.json" % (session or ""))),
                       ("global", paths.USAGE_LAST)):
        snap = load(path) if session or name == "global" else None
        if not snap:
            out[name] = {"path": path, "present": False}
            continue
        ts = int(snap.get("ts", 0) or 0)
        age_min = (int(time.time()) - ts) // 60 if ts else None
        out[name] = {
            "path": path, "present": True,
            "line": snap.get("line_session") or snap.get("line_global"),
            "ts": ts, "age_min": age_min,
            # The skill forbids a bare "(Nm ago)"; hand it the anchored form ready-made.
            "suffix": ("  (%s, %dm ago)" % (time.strftime("%H:%M", time.localtime(ts)), age_min)
                       if age_min is not None and age_min > STALE_MIN else ""),
        }
    return out


def probe_upstream(root):
    """Delegate to git_upstreams.py's documented CLI. Its cache, its rules."""
    script = os.path.join(paths.SCRIPTS, "git_upstreams.py")
    if not os.path.isfile(script):
        return {"error": "git_upstreams.py not found at %s" % script}
    try:
        p = subprocess.run([sys.executable, script, root], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT + 5)
        return json.loads(p.stdout) if p.stdout.strip() else {"error": p.stderr[:200]}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def probe_leak_scan():
    """Is the push leak scan RULE live on this machine? Slot P reports an
    ABSENT scan in one clause; it must never imply a scan that did not happen.
    Unknown is reported as unknown, not as off."""
    try:
        import prefs
        return {"enabled": bool(prefs.effective("claude_md_push_leak_scan"))}
    except Exception as e:
        return {"enabled": None, "error": "%s: %s" % (type(e).__name__, e)}


def probe_throttle():
    out = {"jobs": []}
    for d in (os.path.join(paths.ROOT, "throttle"), os.path.join(LOGS, "throttle")):
        if not os.path.isdir(d):
            continue
        try:
            for n in sorted(os.listdir(d)):
                out["jobs"].append({"dir": d, "file": n})
        except OSError:
            pass
    return out


def _dur(sec):
    """Human length of a span, where _age() above takes an epoch stamp. Two helpers
    because passing a duration to _age() reads as "56 years ago" and looks plausible."""
    d = max(0, int(sec or 0))
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % (d // 60)
    if d < 172800:
        return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)


def probe_usage_chart():
    """Whether this machine's readings have reached the usage chart.

    A session's readings are as unpersisted as an uncommitted file until they are in the
    chart's database, so "am I safe to close" has to include them. Free to ask:
    `usage_chart.state()` is three small reads and a stat, no snapshot scan, and the
    whole section is absent when the machine has no chart at all.
    """
    try:
        import usage_chart
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    st = usage_chart.state()
    if not st.get("url"):
        return {"configured": False}
    behind = st.get("stale_s")            # a DURATION; _age() takes an epoch stamp
    return {"configured": True, "url": st["url"],
            "last_push_ts": st.get("last_push_ts") or 0,
            "last_push_age": _age(st["last_push_ts"]) if st.get("last_push_ts") else None,
            "last_push_rows": st.get("last_push_rows") or 0,
            # readings rendered since the last push are not in the chart yet; 15 min of
            # slack keeps a push that just ran from reading as behind
            "behind": bool(behind and behind > 900),
            "behind_by": _dur(behind) if behind else None,
            "verb": "/claudette:chart"}


def main(argv):
    repos = []
    session, pretty, no_names, cap = None, False, False, MEMORY_CAP
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo" and i + 1 < len(argv):
            repos.append(argv[i + 1]); i += 1
        elif a == "--session" and i + 1 < len(argv):
            session = argv[i + 1]; i += 1
        elif a == "--memory-cap" and i + 1 < len(argv):
            cap = int(argv[i + 1]); i += 1
        elif a == "--no-sibling-names":
            no_names = True
        elif a == "--pretty":
            pretty = True
        else:
            sys.stderr.write("unknown arg: %s\n" % a); return 2
        i += 1

    # --repo repeats. The FIRST is primary: it keeps the original `repo` /
    # `upstream_check` keys and defines `project_key`, so every existing reader of
    # this output is unaffected. The rest land in `extra_repos`, present only when
    # there is more than one. One call per repo would pay a round trip each time AND
    # recompute memory/scratchpad/status/throttle under a DIFFERENT project_key,
    # which would be wrong: those sections belong to the session's own project, not
    # to whichever repo was being asked about.
    if not repos:
        repos = [os.getcwd()]
    repo, key = repos[0], project_key(repos[0])
    result = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repo_arg": repo, "project_key": key, "session_id": session,
        "touched": None,
        "touched_note": ("TOUCHED is deliberately absent: attribution comes from the agent's own "
                         "tool history, never from mtimes. Ages below are triage hints only."),
    }
    for name, fn in (("repo", lambda: probe_repo(repo)),
                     ("upstream_check", lambda: probe_upstream(repo)),
                     ("memory", lambda: probe_memory(key, cap)),
                     ("scratchpad", lambda: probe_scratchpads(key, session, no_names)),
                     ("status_lines", lambda: probe_status(session)),
                     ("throttle", probe_throttle),
                     ("usage_chart", probe_usage_chart),
                     ("leak_scan", probe_leak_scan)):
        try:
            result[name] = fn()
        except Exception as e:                 # one broken section must not blind the rest
            result[name] = {"error": "%s: %s" % (type(e).__name__, e)}

    if len(repos) > 1:
        result["extra_repos"] = []
        for r in repos[1:]:
            rec = {"repo_arg": r}
            for name, fn in (("repo", lambda r=r: probe_repo(r)),
                             ("upstream_check", lambda r=r: probe_upstream(r))):
                try:
                    rec[name] = fn()
                except Exception as e:
                    rec[name] = {"error": "%s: %s" % (type(e).__name__, e)}
            result["extra_repos"].append(rec)

    # Written as UTF-8 whatever the locale: both readers decode UTF-8 (Claude Code
    # through the Bash tool, and dot_done.py, which names it). Left to a cp1252
    # stdout, the first character outside it, an arrow in a commit subject, would
    # raise here and lose the whole probe. It goes with _git() decoding UTF-8. Here
    # and not at import: tests import this module.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(json.dumps(result, indent=2 if pretty else None, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
