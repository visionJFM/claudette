#!/usr/bin/env python3
"""move_into_place.py [--apply [--force]] | --undo BACKUP

Put the claudette copy this script belongs to (a fresh clone, audited, for example
~/claudette-audit) in place as ~/.claude/skills/claudette, the same way whether claudette
is installed there or not. It is also the repair for an update that can no longer pull
(the repository moved, its history was rewritten, an edited file blocks it): reinstall
from a fresh copy.

Without --apply it changes nothing and prints its plan. With --apply, in one run:
  1. the existing folder, whole (its git history included), moves to a backup folder
     in the home folder, ~/claudette-backup-<date>-<time>: outside ~/.claude, where
     Claude Code would load it as a second skill;
  2. this copy moves into its place as it is on disk, edits made during an audit included;
  3. what git ignores in the old folder moves into the new one (prefs.local.json, the
     answers; logs/, with the undo records), except what the update rebuilds
     (hooks/hooks.json, generated/) and Python caches;
  4. the old folder's edited files are saved as local-edits.patch in the backup, to
     re-apply on request; files git neither tracks nor ignores stay in the backup;
  5. the install's clone gets a push address that refuses every push: an install only
     receives updates, and its fetch address, which updates read, stays as it was.
Then run the update flow: it rebuilds hooks/hooks.json and the settings.

A development clone (one holding docs/CLAUDETTE_DEV.md, which the published repo never
does) keeps its push address: moving a fresh clone of the development repo in with this
script sets up a development machine and keeps its answers and logs.

It stops before changing anything, unless --force, when the old folder holds commits its
remote lacks (work found nowhere else), has a git history it cannot read, or is not
claudette. A link in place of the folder is refused: replace it by hand.

--undo BACKUP puts the old folder back, and this install in BACKUP/reinstalled.

Exit: 0 done, or plan printed; 1 failed (the message says where everything is);
2 refused, nothing changed.
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
OLD = "claudette"                       # the old folder, inside the backup
PATCH = "local-edits.patch"
# Ignored by git but not carried: the update flow rebuilds them from the registry and
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
    cmd = ["git"]
    if git_dir:
        cmd += ["--git-dir", git_dir, "--work-tree", work_tree]
    else:
        cmd += ["-C", cwd]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        r = subprocess.run(cmd + list(args), capture_output=True, env=env, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, r.stdout.decode("utf-8", "replace")


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


def inspect_old(target, src):
    """What the existing folder holds, read only."""
    info = {"claudette": os.path.isfile(os.path.join(target, "features_def.json")),
            "dev_notes": is_dev_clone(target),
            "git": None, "remote": None, "head": None, "local_commits": None,
            "edited": [], "patch": "", "untracked": None}
    if os.path.exists(os.path.join(target, ".git")):
        rc, head = git(target, "rev-parse", "--short", "HEAD")
        if rc != 0:
            info["git"] = "unreadable"
        else:
            info["git"] = "ok"
            info["head"] = head.strip()
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
    # of files, moves in one rename). Such a folder that holds a rebuilt path (hooks/, where
    # nothing is tracked) is opened, so that hooks.json stays behind.
    carry, left, stack = [], [], [l.rstrip("/") for l in _lines(ign, "\0")]
    while stack:
        rel = stack.pop()
        parts = rel.split("/")
        full = os.path.join(target, *parts)
        if rel in REBUILT or any(p in CACHES for p in parts):
            left.append(rel)
        elif (os.path.isdir(full) and not os.path.islink(full)
              and any(r.startswith(rel + "/") for r in REBUILT)):
            stack += [rel + "/" + n for n in os.listdir(full)]
        else:
            carry.append(rel)

    def outermost(rels):
        # git may name a folder and a path inside it (hooks/ and hooks/hooks.json); a
        # path under one already listed moves with it, or was opened above.
        rels = sorted(set(rels))
        return [r for r in rels if not any(r.startswith(o + "/") for o in rels)]
    info["carry"], info["rebuilt"] = outermost(carry), outermost(left)
    if info["untracked"] is not None:
        ignored = set(carry) | set(left)
        info["untracked"] = sorted(u for u in info["untracked"]
                                   if not any(u == i or u.startswith(i + "/") for i in ignored))
    return info


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
    plan["src_edited"] = [l[3:] for l in _lines(st)] if rc == 0 else []
    rc, head = git(src, "rev-parse", "--short", "HEAD")
    plan["src_head"] = head.strip() if rc == 0 else "?"
    rc, url = git(src, "remote", "get-url", "origin")
    plan["src_remote"] = url.strip() if rc == 0 else None
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
    old = inspect_old(target, src)
    plan["old"] = old
    if not old["claudette"]:
        plan["stops"].append("the existing folder is not claudette (no features_def.json)")
    if old["git"] == "unreadable":
        plan["stops"].append("the existing folder's git history cannot be read: commits "
                             "found nowhere else may be in it")
    elif old["local_commits"]:
        plan["stops"].append("the existing folder holds %d commit(s) its remote lacks: "
                             "after the move they exist only in the backup"
                             % old["local_commits"])
    now = now or datetime.datetime.now()
    base = os.path.join(backup_parent, "claudette-backup-" + now.strftime("%Y%m%d-%H%M%S"))
    backup, n = base, 2
    while os.path.lexists(backup):
        backup, n = "%s-%d" % (base, n), n + 1
    plan["backup"] = backup
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
        out.append("    edited since its commit (kept as they are): %s"
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
        if old["claudette"]:
            out.append("    its status line, and the hooks running sessions loaded, call the "
                       "scripts by path: this copy's scripts run as soon as it is in place, "
                       "before the update's questions")
        out.append("    moves whole to the backup: %s" % plan["backup"])
        out.append("    carried into the new install: %s"
                   % (", ".join(old["carry"]) or "nothing"))
        if old["rebuilt"]:
            out.append("    left in the backup, rebuilt by the update: %s"
                       % ", ".join(old["rebuilt"]))
        if old["edited"]:
            out.append("    edited files, saved as %s in the backup, not carried: %s"
                       % (PATCH, ", ".join(old["edited"])))
        if old["untracked"]:
            out.append("    files git neither tracks nor ignores, left in the backup: %s"
                       % ", ".join(old["untracked"]))
    for n in plan["notes"]:
        out.append("  note: " + n)
    if plan["stops"]:
        out.append("STOP - each needs the user's explicit yes, then --apply --force:")
        out += ["  - " + s for s in plan["stops"]]
    return out


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
        _seal(plan, log)
        return
    backup = plan["backup"]
    old_in_backup = os.path.join(backup, OLD)
    os.makedirs(backup)
    record = {"kind": "claudette-move-into-place", "version": 1, "target": target,
              "source": src, "carried": [], "displaced": [],
              "time": datetime.datetime.now().isoformat(timespec="seconds")}
    if plan["old"]["patch"]:
        with open(os.path.join(backup, PATCH), "w", encoding="utf-8", newline="") as fh:
            fh.write(plan["old"]["patch"])
    _write_record(backup, record)
    try:
        _rename(target, old_in_backup)
    except OSError as e:
        shutil.rmtree(backup, ignore_errors=True)
        raise Refused("could not move the existing folder out of the way: %s\nSomething "
                      "has a file open in it, or works inside it: a Claude Code session "
                      "started there or its hooks, an editor, a terminal. Close it, then "
                      "run again." % e)
    try:
        _rename(src, target)
    except OSError as e:
        try:
            _rename(old_in_backup, target)
            shutil.rmtree(backup, ignore_errors=True)
        except OSError as e2:
            raise Failed("could not move the copy in (%s), nor the old folder back (%s): "
                         "the old folder is %s, the copy %s" % (e, e2, old_in_backup, src))
        raise Refused("could not move the copy in, the old folder is back in place: %s" % e)
    _seal(plan, log)
    problems = []
    for rel in plan["old"]["carry"]:
        a = os.path.join(old_in_backup, *rel.split("/"))
        b = os.path.join(target, *rel.split("/"))
        try:
            record["displaced"] += merge_move(a, b, os.path.join(backup, "displaced",
                                                                 *rel.split("/")))
            record["carried"].append(rel)
        except OSError as e:
            problems.append("%s: %s" % (rel, e))
    _write_record(backup, record)
    log("Installed %s; the old folder is in %s." % (target, backup))
    log("Carried into the new install: %s" % (", ".join(record["carried"]) or "nothing"))
    if record["displaced"]:
        log("The copy's own files at those places went to: %s"
            % ", ".join(record["displaced"]))
    if plan["old"]["patch"]:
        log("Edits of the old folder: %s" % os.path.join(backup, PATCH))
    log("Undo: bash \"%s\" move_into_place.py --undo \"%s\""
        % (os.path.join(target, "scripts", "run.sh"), backup))
    if problems:
        raise Failed("installed, but these stayed in the backup (move them by hand from "
                     "%s): %s" % (old_in_backup, "; ".join(problems)))


def _seal(plan, log):
    """Point the install's push address at nowhere: a commit made in an install can never
    reach the published repo, even for someone with write access to it. Fetch and pull
    read the fetch address, which stays. A development clone keeps its push address."""
    if plan["src_dev"] or not plan["src_remote"]:
        return
    rc, out = git(plan["target"], "config", "remote.origin.pushurl", NO_PUSH)
    if rc != 0:
        log("note: pushing from the install is not disabled (git config failed: %s)"
            % out.strip())


def _write_record(backup, record):
    with open(os.path.join(backup, RECORD), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)


def undo(backup, log=print):
    backup = os.path.abspath(backup)
    try:
        with open(os.path.join(backup, RECORD), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        raise Refused("no %s in %s" % (RECORD, backup))
    if rec.get("kind") != "claudette-move-into-place":
        raise Refused("%s was not written by this script" % RECORD)
    target, old_in_backup = rec["target"], os.path.join(backup, OLD)
    parked = os.path.join(backup, "reinstalled")
    if not os.path.isdir(old_in_backup):
        raise Refused("the old folder is not in %s" % backup)
    if not os.path.isdir(target) or os.path.lexists(parked):
        raise Refused("%s is missing, or %s already exists" % (target, parked))
    moved_back = []
    try:
        for rel in reversed(rec.get("carried", [])):
            a = os.path.join(target, *rel.split("/"))
            if os.path.lexists(a):
                merge_move(a, os.path.join(old_in_backup, *rel.split("/")),
                           os.path.join(backup, "displaced-undo", *rel.split("/")))
                moved_back.append(rel)
        _rename(target, parked)
    except OSError as e:
        for rel in moved_back:
            try:
                merge_move(os.path.join(old_in_backup, *rel.split("/")),
                           os.path.join(target, *rel.split("/")),
                           os.path.join(backup, "displaced-redo", *rel.split("/")))
            except OSError:
                pass
        raise Failed("could not take the new install out (%s); it is back as it was, "
                     "unless a path above failed" % e)
    try:
        _rename(old_in_backup, target)
    except OSError as e:
        raise Failed("the new install is in %s, but the old folder could not go back "
                     "from %s: %s" % (parked, old_in_backup, e))
    log("Put back the old folder at %s; the new install is in %s." % (target, parked))
    if rec.get("displaced"):
        log("Files of the new copy displaced at install time: %s"
            % ", ".join(rec["displaced"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="do it (default: print the plan)")
    ap.add_argument("--force", action="store_true", help="go past a STOP the user accepted")
    ap.add_argument("--undo", metavar="BACKUP", help="put back the folder this backup holds")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    home = os.path.expanduser("~")
    try:
        os.chdir(home)          # this process must not hold a folder it moves
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
