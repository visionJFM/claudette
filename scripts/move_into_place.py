#!/usr/bin/env python3
"""move_into_place.py [--apply [--force]] | --undo BACKUP

Put the claudette copy this script belongs to (a fresh clone, audited, for example
~/claudette-audit) in place as ~/.claude/skills/claudette, the same way whether claudette
is installed there or not. It is also the repair for an update that can no longer pull
(the repository moved, its history was rewritten, an edited file blocks it): reinstall
from a fresh copy.

Without --apply it changes nothing and prints its plan. With --apply:
  - no claudette folder yet: this copy moves in as it is on disk;
  - an existing folder is replaced IN PLACE, never moved, so that a process working in
    it (a session, its hooks, a terminal, an editor) cannot block the reinstall:
    1. first, with nothing changed yet, a backup folder in the home folder,
       ~/claudette-backup-<date>-<time>, receives the old git history (history.bundle,
       verified), the old edits (local-edits.patch) and a copy of every file of yours at
       a path this copy tracks;
    2. the folder's tracked files become this copy's commit, and edits made in the copy
       during an audit are copied over;
    3. what git ignores stays where it is (prefs.local.json, the answers; logs/, with the
       undo records), except what the update rebuilds (hooks/hooks.json, generated/) and
       Python caches, which go to the backup, as do files git neither tracks nor ignores;
    4. the clone's address becomes the copy's, and its push address one that refuses
       every push: an install only receives updates.
Then run the update flow: it rebuilds hooks/hooks.json and the settings.

A run stopped halfway (a file held open by another program) is completed by running the
same command again: the folder remembers the reinstall under way.

A development clone (one holding docs/CLAUDETTE_DEV.md, which the published repo never
does) keeps its push address: installing a fresh clone of the development repo with this
script sets up a development machine and keeps its answers and logs.

It stops before changing anything, unless --force, when the old folder holds commits its
remote lacks (work found nowhere else), has a git history it cannot read, or is not
claudette; those are then moved, entry by entry, to the backup. A link in place of the
folder is refused: replace it by hand.

--undo BACKUP puts back the old state the backup holds.

Exit: 0 done, or plan printed; 1 failed (the message says where everything is and how to
finish); 2 refused, nothing changed.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD = "move_into_place.json"
MARKER = "claudette-reinstall.json"     # in the install's .git: a reinstall under way
OLD = "claudette"                       # the old folder, in a backup made by version 1
PATCH = "local-edits.patch"
BUNDLE = "history.bundle"
IN_REF = "refs/claudette/incoming"
IN_ORIGIN = "refs/claudette/incoming-origin"
# Ignored by git but not kept: the update flow rebuilds them from the registry and
# prefs.local.json, and an old hooks.json would run old hooks against new scripts.
REBUILT = ("hooks/hooks.json", "generated")
CACHES = ("__pycache__", ".pytest_cache")
RETRIES, RETRY_S = 5, 0.5
NO_PUSH = "no-push://a-claudette-install-only-receives-updates"


class Refused(Exception):
    """Nothing changed."""


class Failed(Exception):
    """Something went wrong after a change; the message says where things are."""


def git(cwd, *args, git_dir=None, work_tree=None):
    """(returncode, stdout); on a failure, stdout followed by stderr."""
    cmd = ["git"]
    if git_dir:
        cmd += ["--git-dir", git_dir, "--work-tree", work_tree]
    else:
        cmd += ["-C", cwd]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               GIT_ASK_YESNO="false")        # never "Unlink of file failed. Try again?"
    try:
        r = subprocess.run(cmd + list(args), capture_output=True, env=env, timeout=300,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        out += r.stderr.decode("utf-8", "replace")
    return r.returncode, out


def is_link(path):
    """The folder itself is a symlink or a Windows junction (os.path.islink misses those
    before 3.12). Only its last part counts: a parent reached through a link (/var on
    macOS, an 8.3 short name on Windows) is not this folder being one."""
    if os.path.islink(path):
        return True
    path = os.path.abspath(path)
    via_parent = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    return os.path.normcase(os.path.realpath(path)) != os.path.normcase(via_parent)


def _busy(e):
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33)


def _cross_device(e):
    return getattr(e, "errno", None) == 18 or getattr(e, "winerror", None) == 17


def _rename(src, dst):
    """os.rename, retried while a file is briefly held (an antivirus scan, a hook that is
    just finishing); a move to another drive copies."""
    for attempt in range(RETRIES):
        try:
            os.rename(src, dst)
            return
        except OSError as e:
            if _cross_device(e):
                shutil.move(src, dst)
                return
            if attempt == RETRIES - 1 or not _busy(e):
                raise
            time.sleep(RETRY_S)


def merge_move(src, dst, displaced):
    """Move src to dst. Where both are folders, merge them; where both hold a file, src
    wins and dst's file goes to `displaced`. Returns the displaced paths."""
    out = []
    if not os.path.lexists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _rename(src, dst)
        return out
    if os.path.isdir(src) and os.path.isdir(dst) and not os.path.islink(dst):
        for name in sorted(os.listdir(src)):
            out += merge_move(os.path.join(src, name), os.path.join(dst, name),
                              os.path.join(displaced, name))
        os.rmdir(src)
        return out
    os.makedirs(os.path.dirname(displaced), exist_ok=True)
    _rename(dst, displaced)
    _rename(src, dst)
    return [displaced]


def _lines(text, sep="\n"):
    return [l for l in text.split(sep) if l.strip()]


def is_dev_clone(folder):
    """Holds the developer notes, which the published repo never ships; an older clone
    holds them at its root rather than in docs/."""
    return any(os.path.isfile(os.path.join(folder, *p))
               for p in (("docs", "CLAUDETTE_DEV.md"), ("CLAUDETTE_DEV.md",)))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def inspect_old(target, src):
    """What the existing folder holds, read only."""
    info = {"claudette": os.path.isfile(os.path.join(target, "features_def.json")),
            "dev_notes": is_dev_clone(target),
            "git": None, "remote": None, "head": None, "head_full": None, "branch": None,
            "local_commits": None, "edited": [], "patch": "", "untracked": None,
            "tracked": [], "config": []}
    if os.path.exists(os.path.join(target, ".git")):
        rc, head = git(target, "rev-parse", "HEAD")
        if rc != 0:
            info["git"] = "unreadable"
        else:
            info["git"] = "ok"
            info["head_full"] = head.strip()
            info["head"] = head.strip()[:7]
            rc, br = git(target, "symbolic-ref", "--short", "-q", "HEAD")
            info["branch"] = br.strip() if rc == 0 and br.strip() else None
            rc, url = git(target, "remote", "get-url", "origin")
            info["remote"] = url.strip() if rc == 0 else None
            rc, n = git(target, "rev-list", "--count", "HEAD", "--not", "--remotes")
            info["local_commits"] = int(n) if rc == 0 and n.strip().isdigit() else None
            if info["local_commits"] is None:
                info["git"] = "unreadable"
            rc, st = git(target, "status", "--porcelain", "--untracked-files=no")
            info["edited"] = [l[3:] for l in _lines(st)] if rc == 0 else []
            if info["edited"]:
                rc, patch = git(target, "diff", "HEAD", "--binary")
                info["patch"] = patch if rc == 0 else ""
            rc, un = git(target, "ls-files", "-z", "--others", "--exclude-standard")
            info["untracked"] = _lines(un, "\0") if rc == 0 else None
            rc, tr = git(target, "ls-files", "-z")
            info["tracked"] = _lines(tr, "\0") if rc == 0 else []
            rc, cfg = git(target, "config", "--local", "--get-regexp",
                          r"^(remote\.|branch\.)")
            info["config"] = [l.split(" ", 1) for l in _lines(cfg)] if rc == 0 else []
    # Ignored paths, by the new copy's rules and the old folder's own, read with the new
    # copy's git: this works when the old folder has no readable history.
    excl = ["--exclude-from=" + os.path.join(src, ".gitignore")]
    if os.path.isfile(os.path.join(target, ".gitignore")):
        excl.append("--exclude-from=" + os.path.join(target, ".gitignore"))
    rc, git_dir = git(src, "rev-parse", "--absolute-git-dir")
    if rc != 0:
        raise Refused("could not read the copy's git folder: %s" % git_dir.strip())
    rc, ign = git(None, "ls-files", "-z", "--others", "--ignored", "--directory", *excl,
                  git_dir=git_dir.strip(), work_tree=target)
    if rc != 0:
        raise Refused("could not list what git ignores in %s:\n%s" % (target, ign.strip()))
    # --directory names a folder once when all it holds is ignored (logs/, tens of thousands
    # of files). Such a folder that holds a rebuilt path (hooks/, where nothing is tracked)
    # is opened, so that hooks.json is taken out and the rest stays.
    keep, rebuilt, stack = [], [], [l.rstrip("/") for l in _lines(ign, "\0")]
    while stack:
        rel = stack.pop()
        parts = rel.split("/")
        full = os.path.join(target, *parts)
        if rel in REBUILT or any(p in CACHES for p in parts):
            rebuilt.append(rel)
        elif (os.path.isdir(full) and not os.path.islink(full)
              and any(r.startswith(rel + "/") for r in REBUILT)):
            stack += [rel + "/" + n for n in os.listdir(full)]
        else:
            keep.append(rel)

    def outermost(rels):
        # git may name a folder and a path inside it (hooks/ and hooks/hooks.json); a
        # path under one already listed goes with it, or was opened above.
        rels = sorted(set(rels))
        return [r for r in rels if not any(r.startswith(o + "/") for o in rels)]
    info["carry"], info["rebuilt"] = outermost(keep), outermost(rebuilt)
    if info["untracked"] is not None:
        ignored = set(keep) | set(rebuilt)
        info["untracked"] = sorted(u for u in info["untracked"]
                                   if not any(u == i or u.startswith(i + "/") for i in ignored))
    return info


def _under(path, roots):
    return any(path == r or path.startswith(r + "/") for r in roots)


def make_plan(src, target, backup_parent, now=None):
    src, target = os.path.abspath(src), os.path.abspath(target)
    if not os.path.isfile(os.path.join(src, "features_def.json")):
        raise Refused("%s is not a claudette copy (no features_def.json)" % src)
    rc, top = git(src, "rev-parse", "--show-toplevel")
    if rc != 0 or os.path.normcase(os.path.realpath(top.strip())) != \
            os.path.normcase(os.path.realpath(src)):
        raise Refused("%s is not a git clone: /claudette update could never pull into it. "
                      "Fetch it with git clone." % src)
    same = os.path.normcase(src) == os.path.normcase(target)
    nested = any(os.path.normcase(a + os.sep).startswith(os.path.normcase(b + os.sep))
                 for a, b in ((src, target), (target, src)))
    if nested and not same:
        raise Refused("the copy and the install folder are inside one another: %s, %s"
                      % (src, target))
    plan = {"src": src, "target": target, "same": same, "stops": [], "notes": []}
    if same:
        return plan
    rc, st = git(src, "status", "--porcelain", "--untracked-files=no")
    plan["src_status"] = _lines(st) if rc == 0 else []
    plan["src_edited"] = [l[3:] for l in plan["src_status"]]
    rc, head = git(src, "rev-parse", "HEAD")
    plan["src_commit"] = head.strip() if rc == 0 else None
    plan["src_head"] = plan["src_commit"][:7] if plan["src_commit"] else "?"
    rc, url = git(src, "remote", "get-url", "origin")
    plan["src_remote"] = url.strip() if rc == 0 else None
    rc, orig = git(src, "rev-parse", "--verify", "-q", "refs/remotes/origin/main")
    plan["src_origin"] = orig.strip() if rc == 0 and orig.strip() else None
    plan["src_dev"] = is_dev_clone(src)
    if not plan["src_remote"]:
        plan["notes"].append("this copy has no remote: /claudette update will have nothing "
                             "to pull from")
    plan["exists"] = os.path.lexists(target)
    if not plan["exists"]:
        return plan
    if is_link(target):
        raise Refused("%s is a link to another folder: replace it by hand" % target)
    if not os.path.isdir(target):
        raise Refused("%s is a file, not a folder" % target)
    if not plan["src_commit"]:
        raise Refused("could not read the copy's commit")
    old = inspect_old(target, src)
    plan["old"] = old
    marker = _read_json(os.path.join(target, ".git", MARKER)) or {}
    if marker.get("commit") == plan["src_commit"] and os.path.isfile(
            os.path.join(marker.get("backup") or "", RECORD)):
        plan["resume"] = marker["backup"]
        plan["backup"] = marker["backup"]
        plan["mode"] = marker.get("mode", "git")
    else:
        plan["resume"] = None
        if not old["claudette"]:
            plan["stops"].append("the existing folder is not claudette (no features_def.json)")
        if old["git"] == "unreadable":
            plan["stops"].append("the existing folder's git history cannot be read: commits "
                                 "found nowhere else may be in it")
        elif old["local_commits"]:
            plan["stops"].append("the existing folder holds %d commit(s) its remote lacks: "
                                 "after the reinstall they exist only in the backup"
                                 % old["local_commits"])
        plan["mode"] = "git" if old["git"] == "ok" and old["claudette"] else "entries"
        now = now or datetime.datetime.now()
        base = os.path.join(backup_parent, "claudette-backup-" + now.strftime("%Y%m%d-%H%M%S"))
        backup, n = base, 2
        while os.path.lexists(backup):
            backup, n = "%s-%d" % (base, n), n + 1
        plan["backup"] = backup
    # What goes to the backup.
    keep_roots = old["carry"]
    rc, tracked_new = git(src, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    tracked_new = _lines(tracked_new, "\0") if rc == 0 else []
    if plan["mode"] == "git":
        plan["entries"] = []
        plan["loose"] = [u for u in (old["untracked"] or []) if u not in set(tracked_new)]
        old_tracked = set(old["tracked"])
        plan["overwritten"] = [p for p in tracked_new if p not in old_tracked
                               and os.path.isfile(os.path.join(target, *p.split("/")))]
    else:
        tops_kept = {r for r in keep_roots if "/" not in r}
        opened = {r.split("/")[0] for r in keep_roots + old["rebuilt"] if "/" in r}
        plan["entries"] = sorted(n for n in os.listdir(target)
                                 if n not in tops_kept and n not in opened
                                 and not (n == ".git" and old["git"] is None)
                                 and n not in {r for r in old["rebuilt"] if "/" not in r})
        plan["loose"] = []
        plan["overwritten"] = [p for p in tracked_new if _under(p, keep_roots)
                               and os.path.isfile(os.path.join(target, *p.split("/")))]
    plan["undo"] = 'bash "%s" move_into_place.py --undo "%s"' % (
        os.path.join(target, "scripts", "run.sh"), plan["backup"])
    return plan


def describe(plan):
    out = []
    if plan["same"]:
        return ["claudette: this copy is already in place at %s; nothing to move."
                % plan["target"]]
    out.append("claudette: install this copy as %s" % plan["target"])
    out.append("  copy        : %s (commit %s, from %s)"
               % (plan["src"], plan["src_head"], plan["src_remote"] or "no remote"))
    if plan["src_remote"]:
        out.append("  push        : %s" % (
            "kept: this copy is a development clone (CLAUDETTE_DEV.md)" if plan["src_dev"]
            else "disabled: an install only receives updates; fetch and pull are unchanged"))
    if plan["src_edited"]:
        out.append("    edited since its commit (installed as they are): %s"
                   % ", ".join(plan["src_edited"]))
        out.append("    an update that changes one of these files fails; the repair is "
                   "this reinstall again")
    if not plan["exists"]:
        out.append("  no claudette folder there yet: the copy moves in")
    else:
        old = plan["old"]
        desc = "claudette" if old["claudette"] else "NOT claudette"
        if old["git"] == "ok":
            desc += ", commit %s from %s" % (old["head"], old["remote"] or "no remote")
        elif old["git"] is None:
            desc += ", no git history"
        if old["dev_notes"]:
            desc += ", with development notes (CLAUDETTE_DEV.md)"
        out.append("  existing    : %s" % desc)
        if plan["resume"]:
            out.append("    an unfinished reinstall of this same copy: running again "
                       "completes it, with the backup it already made")
        out.append("    replaced in place: the folder stays, its files become this copy's")
        if old["claudette"]:
            out.append("    its status line, and the hooks running sessions loaded, call the "
                       "scripts by path: this copy's scripts run as soon as it is in place, "
                       "before the update's questions")
        out.append("    kept where it is: %s" % (", ".join(old["carry"]) or "nothing"))
        out.append("    backup folder: %s" % plan["backup"])
        if plan["mode"] == "git":
            out.append("      the old history, as %s" % BUNDLE)
        if old["edited"]:
            out.append("      edited files, as %s, replaced: %s" % (PATCH, ", ".join(old["edited"])))
        if plan["overwritten"]:
            out.append("      your files at paths this copy tracks, copied there, then "
                       "replaced: %s" % ", ".join(plan["overwritten"]))
        if old["rebuilt"]:
            out.append("      taken out, rebuilt by the update: %s" % ", ".join(old["rebuilt"]))
        if plan["loose"]:
            out.append("      files git neither tracks nor ignores, moved there: %s"
                       % ", ".join(plan["loose"]))
        if plan["entries"]:
            out.append("      the folder's other contents, moved there: %s"
                       % ", ".join(plan["entries"]))
        out.append("    undo afterwards: %s" % plan["undo"])
    for n in plan["notes"]:
        out.append("  note: " + n)
    if plan["stops"]:
        out.append("STOP - each needs the user's explicit yes, then --apply --force:")
        out += ["  - " + s for s in plan["stops"]]
    return out


def _seal(target, plan, log):
    """Point the install's push address at nowhere: a commit made in an install can never
    reach the published repo, even for someone with write access to it. Fetch and pull
    read the fetch address, which stays. A development clone keeps its push address."""
    if not plan["src_remote"]:
        return
    git(target, "config", "--unset-all", "remote.origin.pushurl")
    if plan["src_dev"]:
        return
    rc, out = git(target, "config", "remote.origin.pushurl", NO_PUSH)
    if rc != 0:
        log("note: pushing from the install is not disabled (git config failed: %s)"
            % out.strip())


def _write_record(backup, record):
    with open(os.path.join(backup, RECORD), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)


def _write_marker(target, backup, plan):
    with open(os.path.join(target, ".git", MARKER), "w", encoding="utf-8") as fh:
        json.dump({"backup": backup, "commit": plan["src_commit"], "mode": plan["mode"]}, fh)


def _fetch_incoming(target, plan):
    """The copy's exact commit, and its view of the remote, into the install's git, from
    the copy on disk: no network, and the very commit that was audited."""
    refspecs = ["+HEAD:" + IN_REF]
    if plan["src_origin"]:
        refspecs.append("+refs/remotes/origin/main:" + IN_ORIGIN)
    # --update-shallow: an audit copy cloned with --depth 1 is shallow, and git otherwise
    # skips the ref without an error.
    rc, out = git(target, "fetch", "--no-tags", "--update-shallow", plan["src"], *refspecs)
    rc2, got = git(target, "rev-parse", "--verify", "-q", IN_REF)
    if rc != 0 or got.strip() != plan["src_commit"]:
        raise Refused("could not bring the copy's commit into %s: %s"
                      % (plan["target"], out.strip()[-400:]))


def _bundle(target, backup, old):
    """Every ref of the old history, in one verified file. Returns False when there is no
    ref to save."""
    path = os.path.join(backup, BUNDLE)
    rc, refs = git(target, "for-each-ref", "--format=%(objectname) %(refname)")
    want = {l.split()[0] for l in _lines(refs)} | {old["head_full"]}
    rc, out = git(target, "bundle", "create", path, "--all", "HEAD")
    if rc != 0:
        raise Refused("could not save the old history: %s" % out.strip()[-300:])
    rc, out = git(target, "bundle", "verify", path)
    rc2, heads = git(target, "bundle", "list-heads", path)
    have = {l.split()[0] for l in _lines(heads)}
    if rc != 0 or rc2 != 0 or not want <= have:
        raise Refused("the saved history did not verify: %s" % out.strip()[-300:])
    return True


def _take_out(target, rels, dest, log):
    """Move each path to dest, keeping its relative place. Returns those that could not
    move (held open), which stay where they are."""
    stuck = []
    for rel in rels:
        a = os.path.join(target, *rel.split("/"))
        if not os.path.lexists(a):
            continue
        b = os.path.join(dest, *rel.split("/"))
        try:
            merge_move(a, b, os.path.join(dest + "-displaced", *rel.split("/")))
        except OSError as e:
            stuck.append("%s (%s)" % (rel, e))
    return stuck


def _reset(target, commit, why):
    git(target, "update-ref", "refs/heads/main", commit)
    git(target, "symbolic-ref", "HEAD", "refs/heads/main")
    rc, out = git(target, "reset", "--hard", "--quiet", commit)
    if rc != 0:
        raise Failed("%s stopped halfway: %s\nA file in it is held open by another program "
                     "(an editor, a terminal, a running claudette script). Close it, then run "
                     "the same command again: it completes this run."
                     % (why, out.strip()[-300:]))


def _configure(target, plan):
    url = plan["src_remote"]
    rc, names = git(target, "remote")
    for name in _lines(names):
        if name != "origin":
            git(target, "remote", "remove", name)
    if url:
        if git(target, "remote", "get-url", "origin")[0] != 0:
            git(target, "remote", "add", "origin", url)
        else:
            git(target, "remote", "set-url", "origin", url)
        git(target, "config", "--replace-all", "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*")
        git(target, "config", "branch.main.remote", "origin")
        git(target, "config", "branch.main.merge", "refs/heads/main")
    elif git(target, "remote", "get-url", "origin")[0] == 0:
        git(target, "remote", "remove", "origin")


def _only_main(target, commit, origin):
    """The install's refs: main and origin/main, nothing of the old history (the bundle
    holds it)."""
    rc, refs = git(target, "for-each-ref", "--format=%(refname)")
    for ref in _lines(refs):
        if ref not in ("refs/heads/main",):
            git(target, "update-ref", "--no-deref", "-d", ref)
    if origin:
        git(target, "update-ref", "refs/remotes/origin/main", origin)


def _copy_edits(plan):
    """The audit's edits, file by file (a patch would meet line-ending conversions)."""
    for line in plan["src_status"]:
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dst = os.path.join(plan["target"], *path.split("/"))
        if "D" in code:
            if os.path.lexists(dst):
                os.remove(dst)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(plan["src"], *path.split("/")), dst)


def apply(plan, force=False, log=print):
    if plan["same"]:
        return
    if plan["stops"] and not force:
        raise Refused("stopped: " + "; ".join(plan["stops"]))
    src, target = plan["src"], plan["target"]
    if not plan["exists"]:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            _rename(src, target)
        except OSError as e:
            raise Refused("could not move %s to %s: %s" % (src, target, e))
        log("Moved %s to %s." % (src, target))
        _seal(target, plan, log)
        return
    old, backup = plan["old"], plan["backup"]
    if plan["resume"]:
        record = _read_json(os.path.join(backup, RECORD)) or {}
        log("Completing the reinstall started earlier (backup %s)." % backup)
    else:
        # 1. Everything that can fail without touching the folder.
        os.makedirs(backup)
        record = {"kind": "claudette-move-into-place", "version": 2, "mode": plan["mode"],
                  "state": "started", "target": target, "source": src,
                  "commit": plan["src_commit"],
                  "time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "old": {"git": old["git"], "head": old["head_full"],
                          "branch": old["branch"], "config": old["config"]},
                  "kept": old["carry"], "rebuilt": old["rebuilt"], "loose": plan["loose"],
                  "entries": plan["entries"], "overwritten": plan["overwritten"],
                  "bundle": False, "patch": bool(old["patch"])}
        try:
            if plan["mode"] == "git":
                record["bundle"] = _bundle(target, backup, old)
                _fetch_incoming(target, plan)
            if old["patch"]:
                with open(os.path.join(backup, PATCH), "w", encoding="utf-8", newline="") as fh:
                    fh.write(old["patch"])
            for rel in plan["overwritten"]:
                dst = os.path.join(backup, "overwritten", *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(target, *rel.split("/")), dst)
            _write_record(backup, record)
            if plan["mode"] == "git":
                _write_marker(target, backup, plan)
        except (Refused, OSError) as e:
            git(target, "update-ref", "--no-deref", "-d", IN_REF)
            git(target, "update-ref", "--no-deref", "-d", IN_ORIGIN)
            shutil.rmtree(backup, ignore_errors=True)
            raise Refused(str(e))
    # 2. The changes.
    stuck = []
    if plan["mode"] == "entries":
        stuck += _take_out(target, plan["entries"], os.path.join(backup, "old"), log)
        if stuck:
            raise Failed("these stayed in place, held open: %s. Close what holds them, then "
                         "run the same command again." % "; ".join(stuck))
        if not os.path.isdir(os.path.join(target, ".git")):
            rc, out = git(target, "init", "-q", "-b", "main")
            if rc != 0:
                raise Failed("could not start a git repository in %s: %s" % (target, out))
        _write_marker(target, backup, plan)
        try:
            _fetch_incoming(target, plan)
        except Refused as e:
            raise Failed("%s\nRun the same command again to complete it." % e)
    stuck += _take_out(target, plan["loose"], os.path.join(backup, "loose"), log)
    stuck += _take_out(target, old["rebuilt"], os.path.join(backup, "rebuilt"), log)
    _reset(target, plan["src_commit"], "replacing the files")
    _configure(target, plan)
    _seal(target, plan, log)
    rc, origin = git(target, "rev-parse", "--verify", "-q", IN_ORIGIN)
    _only_main(target, plan["src_commit"], origin.strip() if rc == 0 else None)
    _copy_edits(plan)
    if not plan["src_dev"]:
        stuck += _take_out(target, [p for p in ("CLAUDETTE_DEV.md", "docs/CLAUDETTE_DEV.md")
                                    if os.path.isfile(os.path.join(target, *p.split("/")))],
                           os.path.join(backup, "loose"), log)
    try:
        os.remove(os.path.join(target, ".git", MARKER))
    except OSError:
        pass
    record["state"] = "done"
    _write_record(backup, record)
    log("Installed %s in place; the backup is %s." % (target, backup))
    log("Kept where it was: %s" % (", ".join(old["carry"]) or "nothing"))
    if old["patch"]:
        log("Edits of the old folder: %s" % os.path.join(backup, PATCH))
    log("Undo: %s" % plan["undo"])
    log("The copy %s is no longer needed: delete it." % src)
    if stuck:
        raise Failed("installed, but these stayed in place (held open; move them to %s by "
                     "hand): %s" % (backup, "; ".join(stuck)))


def _undo_v1(backup, rec, log):
    """A backup made by version 1 of this script, which moved the whole folder."""
    target, old_in_backup = rec["target"], os.path.join(backup, OLD)
    parked = os.path.join(backup, "reinstalled")
    if not os.path.isdir(old_in_backup):
        raise Refused("the old folder is not in %s" % backup)
    if not os.path.isdir(target) or os.path.lexists(parked):
        raise Refused("%s is missing, or %s already exists" % (target, parked))
    for rel in reversed(rec.get("carried", [])):
        a = os.path.join(target, *rel.split("/"))
        if os.path.lexists(a):
            merge_move(a, os.path.join(old_in_backup, *rel.split("/")),
                       os.path.join(backup, "displaced-undo", *rel.split("/")))
    try:
        _rename(target, parked)
        _rename(old_in_backup, target)
    except OSError as e:
        raise Failed("could not swap the folders back (%s): the old folder is %s, the new "
                     "install %s" % (e, old_in_backup, parked))
    log("Put back the old folder at %s; the new install is in %s." % (target, parked))


def undo(backup, log=print):
    backup = os.path.abspath(backup)
    rec = _read_json(os.path.join(backup, RECORD))
    if not rec:
        raise Refused("no %s in %s" % (RECORD, backup))
    if rec.get("kind") != "claudette-move-into-place":
        raise Refused("%s was not written by this script" % RECORD)
    if rec.get("version", 1) < 2:
        return _undo_v1(backup, rec, log)
    target = rec["target"]
    if not os.path.isdir(target):
        raise Refused("%s is missing" % target)
    if rec.get("state") == "undone":
        raise Refused("this backup was already put back")
    mode, old = rec.get("mode"), rec.get("old") or {}
    if mode == "git":
        if not rec.get("bundle"):
            raise Refused("this backup holds no history to put back")
        bundle = os.path.join(backup, BUNDLE)
        rc, out = git(target, "fetch", "--no-tags", "--update-head-ok", bundle,
                      "+refs/*:refs/claudette-undo/*")
        if rc != 0:
            raise Refused("could not read the saved history: %s" % out.strip()[-300:])
        rc, refs = git(target, "for-each-ref", "--format=%(objectname) %(refname)")
        saved = {}
        for line in _lines(refs):
            sha, ref = line.split(" ", 1)
            if ref.startswith("refs/claudette-undo/"):
                saved[ref[len("refs/claudette-undo/"):]] = sha
            elif ref != "refs/heads/main":
                git(target, "update-ref", "--no-deref", "-d", ref)
        if old.get("branch"):
            git(target, "symbolic-ref", "HEAD", "refs/heads/" + old["branch"])
        for ref, sha in saved.items():
            if ref.startswith("refs/"):
                git(target, "update-ref", ref, sha)
            git(target, "update-ref", "--no-deref", "-d", "refs/claudette-undo/" + ref)
        if not old.get("branch"):
            git(target, "update-ref", "--no-deref", "HEAD", old["head"])
        if "refs/heads/main" not in saved:
            git(target, "update-ref", "--no-deref", "-d", "refs/heads/main")
        rc, out = git(target, "reset", "--hard", "--quiet", old["head"])
        if rc != 0:
            raise Failed("putting back the files stopped halfway: %s\nClose what holds the "
                         "file, then run the same undo again." % out.strip()[-300:])
        rc, cfg = git(target, "config", "--local", "--get-regexp", r"^(remote\.|branch\.)")
        for key in {l.split(" ", 1)[0] for l in _lines(cfg)}:
            git(target, "config", "--unset-all", key)
        for key, value in old.get("config") or []:
            git(target, "config", "--add", key, value)
        patch = os.path.join(backup, PATCH)
        if rec.get("patch") and os.path.isfile(patch):
            rc, out = git(target, "apply", "--binary", "--whitespace=nowarn", patch)
            if rc != 0:
                log("note: the old edits did not re-apply; they are in %s" % patch)
    else:
        parked = os.path.join(backup, "reinstalled")
        kept_tops = {k.split("/")[0] for k in rec.get("kept") or []}
        names_back = set(os.listdir(os.path.join(backup, "old"))) if os.path.isdir(
            os.path.join(backup, "old")) else set()
        for name in sorted(os.listdir(target)):
            if name in kept_tops:
                continue
            merge_move(os.path.join(target, name), os.path.join(parked, name),
                       os.path.join(parked + "-displaced", name))
        for name in sorted(names_back):
            merge_move(os.path.join(backup, "old", name), os.path.join(target, name),
                       os.path.join(backup, "displaced-undo", name))
    for sub in ("loose", "rebuilt"):
        d = os.path.join(backup, sub)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                merge_move(os.path.join(d, name), os.path.join(target, name),
                           os.path.join(backup, "displaced-undo", name))
    over = os.path.join(backup, "overwritten")
    for rel in rec.get("overwritten") or []:
        a = os.path.join(over, *rel.split("/"))
        if os.path.isfile(a):
            b = os.path.join(target, *rel.split("/"))
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(a, b)
    rec["state"] = "undone"
    _write_record(backup, rec)
    log("Put back the state before the reinstall at %s." % target)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="do it (default: print the plan)")
    ap.add_argument("--force", action="store_true", help="go past a STOP the user accepted")
    ap.add_argument("--undo", metavar="BACKUP", help="put back what this backup holds")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    home = os.path.expanduser("~")
    try:
        os.chdir(home)          # this process must not hold the folder it replaces
    except OSError:
        pass
    target = os.path.join(home, ".claude", "skills", "claudette")
    try:
        if args.undo:
            undo(args.undo)
            return 0
        plan = make_plan(SRC, target, home)
        for line in describe(plan):
            print(line)
        if not args.apply:
            if not plan["same"]:
                print("Plan only: nothing changed. Run again with --apply to do it.")
            return 0
        apply(plan, force=args.force)
        return 0
    except Refused as e:
        print("REFUSED, nothing changed: %s" % e, file=sys.stderr)
        return 2
    except Failed as e:
        print("FAILED: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
