#!/usr/bin/env python3
"""Background fetch of a repo whose remote moved (feature `git_auto_fetch`).

    git_autofetch.py run <repo_root> <remote_hash>    the child: decide, fetch, record
    git_autofetch.py status                           every recorded repo, as JSON

The status line calls maybe_spawn() with git_upstreams' answer. The RENDER does
only what costs a file read: feature on, remote moved (`moved_unknown`), this
repo's last attempt old enough. Everything that runs git — resolving the
per-project override, locks, shallow, the upstream's config — happens in the
child, detached, so a render never waits on it.

Whether a repo is fetched is the param `git_auto_fetch_default`, resolved
through overrides.py, most specific wins: registry false -> prefs.local.json
(false = opt-in mode, true = every repo) -> the repo's committed .claudette.json
-> its .claudette.local.json. The FEATURE answer is the machine veto above all
of them: off, nothing is fetched, whatever the files say, and they are kept
(CLAUDETTE_DESIGN.md, "Auto-fetch, and who may switch it on"). ~/.claude and the claudette clone are
repos like any other here.

Every decision is recorded in logs/git_autofetch/<slug>.json — a skip too,
with its reason: `--doctor` reads them to say which repos moved and were not
fetched, and why. Nothing here is silent; it is only out of the render's way.

What a fetch may do is bounded by flags, never by git config: no prompts
(git_hardened), no hooks (core.hooksPath -> an empty dir: fetch fires
reference-transaction), no FETCH_HEAD, no gc or maintenance, no commit-graph
write, no submodules, no prune (`--no-prune` beats a global fetch.prune=true),
no tags, one branch. Kill switch: logs/git_autofetch/off.
"""
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import git_hardened  # noqa: E402

FEATURE = "git_auto_fetch"
PARAM = "git_auto_fetch_default"
STATE_DIR = paths.log("git_autofetch")
OFF_MARKER = os.path.join(STATE_DIR, "off")
NO_HOOKS = os.path.join(STATE_DIR, "nohooks")

FETCH_TIMEOUT = 60
GIT_TIMEOUT = 15
# Seconds before the next attempt on the same repo, by outcome. The render
# spawns only once `at + wait` has passed, so these pace the child as well.
WAIT = {
    "fetched": 5 * 60,      # the floor: never more than one fetch per 5 minutes
    "claimed": FETCH_TIMEOUT + 60,  # a child is (or was) running; a dead one expires
    "skipped": 15 * 60,     # not opted in, lock held, ...: re-decided a little later
    "failed": 30 * 60,      # offline, auth, timeout: back off
    "unmapped": 6 * 3600,   # fetched, but the tracking ref did not move — refspec
}
FETCH_FLAGS = ["--no-write-fetch-head", "--no-auto-maintenance",
               "--recurse-submodules=no", "--no-prune", "--no-prune-tags", "--no-tags"]
FETCH_CONFIG = ["-c", "fetch.writeCommitGraph=false", "-c", "gc.auto=0",
                "-c", "maintenance.auto=false"]


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _slug(root):
    name = os.path.basename(root.rstrip("/\\")).lstrip(".") or "repo"
    return name + "-" + hashlib.md5(_norm(root).encode("utf-8")).hexdigest()[:8]


def _state_path(root):
    return os.path.join(STATE_DIR, _slug(root) + ".json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record(root, outcome, reason, **extra):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {"repo": root, "at": int(time.time()), "outcome": outcome,
            "wait": WAIT.get(outcome, WAIT["skipped"]), "reason": reason}
    data.update(extra)
    path = _state_path(root)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data


def clear(root):
    """Forget a repo's last decision, so the next render re-decides at once
    (after an override was written, say)."""
    try:
        os.remove(_state_path(root))
        return True
    except OSError:
        return False


# --- render side: a file read, then maybe a spawn ------------------------------

def due(root, now=None):
    st = _load(_state_path(root))
    now = time.time() if now is None else now
    return now - int(st.get("at") or 0) >= int(st.get("wait") or 0)


def maybe_spawn(res):
    """Called by the status line with check_upstream()'s dict. Never raises,
    never runs git. Returns True when a child was started."""
    try:
        if not res or res.get("error") or not res.get("moved_unknown"):
            return False
        root, remote_hash = res.get("repo"), res.get("remote_hash")
        if not root or not remote_hash or os.path.exists(OFF_MARKER):
            return False
        import prefs
        if not prefs.effective(FEATURE):
            return False
        if not due(root):
            return False
        # Claim before spawning: parallel renders (several sessions in one
        # repo) see the claim and do not pile up children.
        _record(root, "claimed", "a background fetch was started")
        _spawn([sys.executable, os.path.abspath(__file__), "run", root, remote_hash])
        return True
    except Exception:
        return False


def not_fetched(res):
    """True when the status line should say `nofetch`: the remote moved, the
    feature is on, and this repo's last decision was "not opted in". A file
    read, no git."""
    try:
        if not res or not res.get("moved_unknown") or not res.get("repo"):
            return False
        import prefs
        if not prefs.effective(FEATURE):
            return False
        st = _load(_state_path(res["repo"]))
        return (st.get("outcome") == "skipped" and st.get("opted") is False
                and not st.get("excluded"))
    except Exception:
        return False


def _spawn(args):
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        # NOT DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW beside it, and
        # a console-less python then gives every git.exe it starts a console of
        # its own — a window flashing on each fetch. A hidden console instead,
        # inherited by git. Breakaway: the render may be killed with its job
        # before the fetch ends; not every job allows it, hence the retry.
        base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            subprocess.Popen(args, creationflags=base, **kw)
    else:
        subprocess.Popen(args, start_new_session=True, **kw)


# --- the child -----------------------------------------------------------------

def _git(root, *args, timeout=GIT_TIMEOUT, config=()):
    return git_hardened.run(root, args, timeout, extra_config=config)


def _locks(*dirs):
    found = []
    for d in dirs:
        if d:
            found += [os.path.basename(p) for p in glob.glob(os.path.join(d, "*.lock"))]
    return sorted(set(found))


def _decide(root):
    """(fetch?, reason, source) — the override resolution, in words."""
    import overrides
    value, _skips, msgs, source = overrides.resolve_full(PARAM, root)
    where = (f"{source[0]}: {source[1]}" if source else "machine default (opt-in mode)")
    if value is True:
        return True, f"opted in — {where}", source
    why = "not opted in" if not source else f"opted out — {where}"
    if msgs:
        why += "; " + "; ".join(msgs)
    return False, why, source


def run(root, remote_hash):
    import prefs
    if os.path.exists(OFF_MARKER) or not prefs.effective(FEATURE):
        return _record(root, "skipped", "feature off or kill switch present")
    if root.startswith(("//", "\\\\")):
        return _record(root, "skipped", "network path — not fetched in the background")
    fetch, why, src = _decide(root)
    if not fetch:
        # A project file that says false excluded the repo on purpose: no
        # status line marker for it, only a doctor line naming file and note.
        excluded = bool(src and src[0] in (".claudette.json", ".claudette.local.json"))
        return _record(root, "skipped", why, opted=False, excluded=excluded)
    ok, shallow, _ = _git(root, "rev-parse", "--is-shallow-repository")
    if ok and shallow == "true":
        return _record(root, "skipped", "shallow clone — a fetch could rewrite .git/shallow", opted=True)
    ok, branch, _ = _git(root, "branch", "--show-current")
    if not ok or not branch:
        return _record(root, "skipped", "no current branch (detached HEAD)", opted=True)
    ok, remote, _ = _git(root, "config", "--get", f"branch.{branch}.remote")
    ok2, merge, _ = _git(root, "config", "--get", f"branch.{branch}.merge")
    if not (ok and ok2 and remote and merge):
        return _record(root, "skipped", f"{branch} has no upstream configured", opted=True)
    if remote == ".":
        return _record(root, "skipped", f"{branch} tracks a local branch — nothing to fetch", opted=True)
    ok, track, _ = _git(root, "rev-parse", "--symbolic-full-name", "@{u}")
    if not ok or not track.startswith("refs/remotes/"):
        return _record(root, "skipped", "upstream is not a remote-tracking ref", opted=True)
    ok, gdirs, _ = _git(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    dirs = gdirs.splitlines() if ok else []
    if len(dirs) != 2:
        return _record(root, "failed", "git could not name the repository's directories", opted=True)
    held = _locks(dirs[0], dirs[1], os.path.dirname(os.path.join(dirs[1], track)))
    if held:
        return _record(root, "skipped", "another git command holds " + ", ".join(held), opted=True)
    # One fetch per repository, whichever worktree asked: the claim is on the
    # COMMON dir, which every worktree shares.
    os.makedirs(STATE_DIR, exist_ok=True)
    claim = os.path.join(STATE_DIR, _slug(dirs[1]) + ".fetching")
    try:
        if time.time() - os.path.getmtime(claim) > WAIT["claimed"]:
            os.remove(claim)  # its writer died
    except OSError:
        pass
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        return _record(root, "skipped", "another worktree of this repository is fetching", opted=True)
    try:
        _rok, before, _ = _git(root, "rev-parse", track)
        os.makedirs(NO_HOOKS, exist_ok=True)
        cfg = ["-c", "core.hooksPath=" + NO_HOOKS.replace("\\", "/")] + FETCH_CONFIG
        t0 = time.time()
        ok, _out, err = _git(root, "fetch", *FETCH_FLAGS, remote, merge,
                             timeout=FETCH_TIMEOUT, config=cfg)
        took = round(time.time() - t0, 1)
        if not ok:
            import git_upstreams
            kind = ("timed out" if "timed out" in err else
                    "remote refused the credential" if git_upstreams.AUTH_RE.search(err)
                    else "fetch failed")
            first = (err.splitlines() or [""])[-1][:160]
            return _record(root, "failed", f"{kind}: {first}", opted=True, took=took)
        _aok, after, _ = _git(root, "rev-parse", track)
        if after == remote_hash or (after and after != before):
            return _record(root, "fetched", f"{track} now at {after[:9]}", opted=True, took=took)
        return _record(root, "unmapped",
                       f"fetch succeeded but {track} did not move — the remote's fetch "
                       "refspec does not map this branch", opted=True, took=took)
    finally:
        try:
            os.remove(claim)
        except OSError:
            pass


def status():
    out = []
    for p in sorted(glob.glob(os.path.join(STATE_DIR, "*.json"))):
        d = _load(p)
        if d.get("repo"):
            out.append(d)
    return out


def doctor_rows(feature_on=True):
    """(feature, ok|warn|off, detail, remedy) for --doctor, from the recorded
    decisions only (no probing — doctor is read-only and offline-safe). Only a
    failed fetch is a warn: a repo that has not opted in is information, and a
    warn would land in every update plan."""
    week = time.time() - 7 * 86400
    seen = [d for d in status()
            if os.path.exists(os.path.join(d["repo"], ".git")) and int(d.get("at") or 0) >= week]
    if not feature_on:
        opted = [d for d in seen if d.get("opted")]
        if not seen:
            return []
        return [("git auto-fetch", "off",
                 "off on this machine (the feature is the veto): no repo is fetched, whatever "
                 f"its files say; {len(opted)} recently seen repo(s) opted in keep that setting",
                 "")]
    rows, fetched = [], 0
    for d in seen:
        repo = d["repo"]
        when = time.strftime("%m-%d %H:%M", time.localtime(int(d.get("at") or 0)))
        oc = d.get("outcome")
        if oc == "fetched":
            fetched += 1
        elif oc == "skipped" and d.get("excluded"):
            rows.append(("git auto-fetch", "off", f"{repo}: {d.get('reason')}", ""))
        elif oc == "skipped" and d.get("opted") is False:
            rows.append(("git auto-fetch", "ok",
                         f"{repo}: remote moved, not fetched ({when}) — {d.get('reason')}",
                         'to opt in: "git_auto_fetch_default": true in its .claudette.json '
                         "(every clone) or .claudette.local.json (this machine), with a note"))
        elif oc in ("failed", "unmapped"):
            rows.append(("git auto-fetch", "warn", f"{repo}: {d.get('reason')} ({when})",
                         "check the repo's remote; the next attempt is automatic"))
        elif oc == "skipped":
            rows.append(("git auto-fetch", "ok", f"{repo}: {d.get('reason')} ({when})", ""))
    rows.insert(0, ("git auto-fetch", "ok",
                    f"{fetched} of {len(seen)} recently seen repo(s) fetched on their last attempt",
                    ""))
    return rows

if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "run":
        print(json.dumps(run(a[1], a[2])))
    elif a == ["status"]:
        print(json.dumps(status(), indent=2))
    elif len(a) == 2 and a[0] == "clear":
        print(json.dumps({"cleared": clear(a[1])}))
    else:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        sys.exit(2)
