#!/usr/bin/env python3
"""Read-only upstream staleness checker + cache (see CLAUDETTE_LOGS.md).

Single purpose: answer "has <repo>'s current branch's upstream moved since the
last local fetch?" without downloading objects, mutating the repo, or prompting.

ANTI-GOALS (do not grow this file into a git utils library):
  - never `git fetch` / `git pull` (object downloads belong to explicit actions,
    or to git_autofetch.py, which reads this module's answer and fetches apart)
  - never write git config (hardening is passed per-invocation only)
  - never prompt — terminal AND GUI (empty GIT_ASKPASS, credential.interactive
    =false, GIT_TERMINAL_PROMPT=0, ssh BatchMode, short timeouts, tree kill)

Cache: logs/git_upstreams/<slug>.json — one file per repo root, branch-keyed
entries. Per-repo files make concurrent sessions collision-free: sessions in
different repos touch different files; sessions in the same repo share the
same checked-out branch and write identical data. A `checked_at` claim is
written BEFORE the network call so parallel renders skip instead of piling on.
Kill switch: create logs/git_upstreams/off to disable all checks.

A failed check NEVER answers "up to date": the last known remote hash is kept
in the cache (for diffing) but is not counted against, and the failure is
classified (auth vs unreachable) so callers can say WHY they don't know. An
expired credential on a private remote is otherwise indistinguishable from a
current checkout (CLAUDETTE_DESIGN.md, "Credential expiry: a failed check must
never answer "up to date"").

Callers: hook_statusline.py (import), features_apply.py --doctor (cache files),
pull.sh (--clear after a successful pull) and agents via CLI:
    python git_upstreams.py <repo_dir> [--ttl=SECONDS | --clear]
An explicit ttl is FORCE semantics: it overrides the error retry windows, so
--ttl=0 always re-probes now (clears a wedged pending claim, rechecks right
after a re-auth). The windows only pace the implicit default callers.
Prints JSON. Read-only for callers; this module is the cache's single writer.
"""
import hashlib, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

CACHE_DIR = paths.log("git_upstreams")
OFF_MARKER = os.path.join(CACHE_DIR, "off")
TTL = 5 * 60          # re-verify against the live remote this often
RETRY_TTL = 2 * 60    # after a transient error (offline), retry sooner but not per-render
AUTH_RETRY_TTL = 5 * 60   # prompting is impossible, so a retry costs milliseconds; a
                          # long window would only delay the moment a re-auth stops
                          # showing as broken
GIT_TIMEOUT = 10
PENDING_TTL = 3 * GIT_TIMEOUT  # a claim older than the call it paces has a dead writer

# stderr of a remote call that was REFUSED rather than unreachable. Kept wide
# on purpose: an expired fine-grained PAT on a private GitHub repo answers
# "Repository not found", never "401" — treating that as "gone" would render a
# dead token as a missing repo and stay silent either way.
AUTH_RE = re.compile(
    r"authentication failed|invalid username or password"
    r"|could not read (?:username|password)|terminal prompts disabled"
    r"|bad credentials|permission denied|access denied|repository not found"
    r"|\b40[13]\b", re.I)


from git_hardened import HARD_ENV as _HARD_ENV, HARD_ARGS as _HARD_ARGS  # noqa: E402,F401
from git_hardened import kill_tree as _kill_tree, run as _hard_run  # noqa: E402,F401


def _git(repo, *args, timeout=GIT_TIMEOUT):
    """(ok, stdout, stderr). stderr matters for one call — ls-remote, where it
    is the only place the reason for a failure exists. Hardening: git_hardened."""
    return _hard_run(repo, args, timeout)


def _slug(root):
    # lstrip(".") so dot-dirs (~/.claude) don't yield hidden cache files
    name = os.path.basename(root).lstrip(".") or "repo"
    return name + "-" + hashlib.md5(root.encode("utf-8")).hexdigest()[:8]


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _store(path, data):
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _count_both(root, ref, res):
    """Fill verified_behind/verified_ahead/diverged from HEAD vs ref.

    Both directions, because one is not enough: a rewrite (filter-branch,
    amend, rebase) leaves the repo behind AND ahead at once, and a behind-only
    reading renders as "stale, run pull" — the one action that would merge the
    abandoned history back in. Returns True when the counts were obtained."""
    ok, cnt, _ = _git(root, "rev-list", "--count", "HEAD.." + ref)
    if not ok:
        return False
    res["verified_behind"] = int(cnt or 0)
    ok, cnt, _ = _git(root, "rev-list", "--count", ref + "..HEAD")
    if ok:
        res["verified_ahead"] = int(cnt or 0)
        res["diverged"] = bool(res["verified_behind"] and res["verified_ahead"])
    return True


def check_upstream(repo_dir, ttl=None):
    """Staleness of repo_dir's current branch vs its upstream.

    ttl=None means the default TTL plus the per-error retry windows. A
    NUMERIC ttl is an explicit caller decision and wins over those windows
    (0 = re-probe now, whatever the cache says).

    Returns a dict:
      repo            resolved repo root (cache key), or None
      branch/upstream current branch and its upstream (e.g. "origin/main")
      verified_behind exact behind count verified against the LIVE remote
                      (0 = confirmed up to date), or None if not computable
      verified_ahead  commits we have that the live remote does not, same basis
      diverged        True = behind AND ahead: the two histories have parted
                      (rewrite, amend, rebase). NOT a pull situation — pulling
                      merges the abandoned history back in, so callers must not
                      render this as "behind, run pull"
      moved_unknown   True = remote moved since last fetch, count unknown
                      (objects not local); pair with the local rev-list count.
                      Only then: remote_hash, the live remote's commit
      checked_at      epoch of last ls-remote attempt (from cache)
      last_success_at epoch of the last ls-remote that actually answered
      stale_days      whole days since last_success_at, or None if it never
                      succeeded / is current. Non-None means "the numbers above
                      are not live knowledge" whatever the error says
      error           None | not-a-repo | no-branch | no-upstream |
                      no-local-ref | disabled | pending |
                      auth-failed (remote refused the credential — expired
                      token, revoked access; staleness is UNKNOWN, never 0) |
                      unreachable (offline, DNS, timeout)
    """
    res = {"repo": None, "branch": None, "upstream": None,
           "verified_behind": None, "verified_ahead": None, "diverged": False,
           "moved_unknown": False, "checked_at": None, "last_success_at": None,
           "stale_days": None, "error": None}
    if os.path.exists(OFF_MARKER):
        res["error"] = "disabled"
        return res
    ok, root, _ = _git(repo_dir, "rev-parse", "--show-toplevel")
    if not ok or not root:
        res["error"] = "not-a-repo"
        return res
    res["repo"] = root
    ok, branch, _ = _git(root, "branch", "--show-current")
    if not ok or not branch:  # detached HEAD etc.
        res["error"] = "no-branch"
        return res
    res["branch"] = branch
    ok, upstream, _ = _git(root, "rev-parse", "--abbrev-ref", "@{u}")
    if not ok or not upstream:
        res["error"] = "no-upstream"
        return res
    res["upstream"] = upstream
    ok, local_ref, _ = _git(root, "rev-parse", upstream)
    if not ok or not local_ref:
        res["error"] = "no-local-ref"
        return res

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, _slug(root) + ".json")
    cache = _load(cache_path)
    entry = cache.get(branch) or {}
    now = int(time.time())
    _err = entry.get("error")
    # A refused credential is not a blip: re-probing it every 2 minutes buys
    # nothing (nobody rotates a token in that window) and, on a private remote,
    # every probe is a failed auth attempt against the forge. But the windows
    # pace only the IMPLICIT callers (statusline renders): an explicit ttl
    # outranks them, or no caller could recheck a wedged pending claim or a
    # just-rotated token before the window ran out.
    if ttl is not None:
        window = ttl
    elif not _err:
        window = TTL
    elif _err == "auth-failed":
        window = AUTH_RETRY_TTL
    elif _err == "pending":
        # A claim exists only to stop pile-ons during ONE ls-remote, which
        # _git caps at GIT_TIMEOUT. Any claim older than that has a dead
        # writer (statusline renders are killed on timeout) and nobody coming
        # back to overwrite it — waiting RETRY_TTL on it just wedges callers.
        window = PENDING_TTL
    else:
        window = RETRY_TTL
    fresh = (entry.get("upstream") == upstream
             and now - int(entry.get("checked_at") or 0) < window)

    if not fresh:
        # Claim before the network call so concurrent renders skip it.
        # `repo` rides in the entry so --doctor can name the repo from the cache
        # file alone (the file name is a hash of the path, not the path).
        entry = {"upstream": upstream, "remote_hash": entry.get("remote_hash"),
                 "repo": root, "last_success_at": entry.get("last_success_at"),
                 "checked_at": now, "error": "pending"}
        cache[branch] = entry
        _store(cache_path, cache)
        remote_name, _, remote_branch = upstream.partition("/")
        ok, out, err = _git(root, "ls-remote", remote_name, "refs/heads/" + remote_branch)
        if ok and out:
            entry["remote_hash"] = out.split()[0]
            entry["last_success_at"] = now
            entry["error"] = None
        else:
            entry["error"] = "auth-failed" if AUTH_RE.search(err) else "unreachable"
            entry["last_error"] = err[:200]
        cache = _load(cache_path)  # re-read: keep entries a parallel writer added
        cache[branch] = entry
        _store(cache_path, cache)

    res["checked_at"] = entry.get("checked_at")
    res["error"] = entry.get("error")
    res["last_success_at"] = entry.get("last_success_at")
    remote_hash = entry.get("remote_hash")
    if res["error"]:
        # The remote did not answer THIS time. The cached hash is a memory, not
        # an observation: counting against it reports "up to date" for as long
        # as the failure lasts — the exact shape of a silent stale checkout
        # behind an expired token. Say "unknown, and for how long" instead.
        succ = entry.get("last_success_at")
        res["stale_days"] = (now - int(succ)) // 86400 if succ else None
        return res
    if not remote_hash:
        return res  # never reached the remote yet: no live knowledge

    if remote_hash == local_ref:
        # Remote-tracking ref is current -> the plain rev-list count is exact.
        _count_both(root, "@{u}", res)
        return res

    # Remote moved since last fetch. If we already have its commit locally
    # (e.g. pushed from another machine/tool), we can still count exactly.
    ok, _, _ = _git(root, "cat-file", "-e", remote_hash + "^{commit}")
    if ok:
        if _count_both(root, remote_hash, res):  # behind 0 = we contain it
            return res
    res["moved_unknown"] = True
    res["remote_hash"] = remote_hash  # what git_autofetch.py checks its fetch against
    return res


def clear(repo_dir):
    """Evict repo_dir's cache file, True if one was removed.

    For actions that just changed the truth the cache memorizes — a successful
    `git pull` above all: the next render re-probes live instead of serving the
    pre-pull ↓N/⚠auth for up to an error window. Lives here (not in pull.sh)
    so eviction stays with the cache's single writer instead of a shell copy
    of _slug()."""
    ok, root, _ = _git(repo_dir, "rev-parse", "--show-toplevel")
    if not ok or not root:
        return False
    try:
        os.remove(os.path.join(CACHE_DIR, _slug(root) + ".json"))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    ttl = None  # only an actual --ttl= argument becomes an explicit (forcing) ttl
    target = os.getcwd()
    do_clear = False
    for arg in sys.argv[1:]:
        if arg.startswith("--ttl="):
            ttl = int(arg.split("=", 1)[1])
        elif arg == "--clear":
            do_clear = True
        elif not arg.startswith("--"):
            target = arg
    if do_clear:
        print(json.dumps({"cleared": clear(target)}))
    else:
        print(json.dumps(check_upstream(target, ttl=ttl), indent=2))
