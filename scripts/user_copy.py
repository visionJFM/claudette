#!/usr/bin/env python3
"""User copies — files a feature creates once, then leaves to the user.

A feature's `user_copy` payload names a file in ~/.claude (`path`, a bare file
name) and the claudette file it starts from (`source`). The apply that finds
the feature on and the file absent writes the source there under a one-line
header naming the version it came from. From then on the file is the user's. No apply overwrites it; a newer shipped version is offered
by /claudette update, which asks (untouched copy) or merges with the user's
approval (edited copy) — CLAUDETTE_UPDATE.md "New version of a user copy".

Why a copy and not a generated file: the text is meant to be edited, and a
generated file is rewritten on every apply, so each new version would reach every
machine at its next update without a question.

Versions are the lines of CHANGELOG.md beside the source, newest first:
    - v<N> · <first 12 hex of the text's sha256> · <date> — <what changed>
That file is the one source of truth: a copy whose text hashes to a line is
untouched at that version (header or not), any other text is edited, and the
header only says which version an edited copy started from — the one thing its
text can no longer tell. No "custom" flag: it would repeat what the text shows
and could drift from it. A published text never changes under its number; the
shipped source must match the newest line (tests/test_user_copy.py), and the
base text of a merge is found in the clone's git history by that hash.

Hashes are taken over normalized text (BOM, CRLF and trailing blank lines
removed): git's autocrlf checks a copy out as CRLF on Windows, which must not
make an untouched copy look edited.

CLI, for the agent (via run.sh):
  status [--json]               every user copy: state, base, latest, pending changes
  base <feature> [--out FILE]   the shipped text the copy started from (git history)
  install <feature>             replace an untouched copy with the latest version
  write <feature> --from FILE   write FILE's text as the copy, based on the latest version
  decline <feature>             stop offering the latest version to this copy
  hash <file>                   the 12-hex hash a changelog line gives that text
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import prefs

PATH_RE = re.compile(r"^[\w.-]+\.md$")   # a bare name: nothing outside ~/.claude
HEADER_MARK = "<!-- claudette user copy of "
# The `sha256 <hex>` group is read and ignored: older headers carry one.
_HEADER_RE = re.compile(
    r"^<!-- claudette user copy of (?P<src>\S+): base (?:v(?P<base>\d+)|unknown)"
    r"(?:, sha256 (?P<sha>[0-9a-f]{64}))?(?:, declined v(?P<declined>\d+))?\.")
_CHANGELOG_RE = re.compile(r"^- v(\d+) · ([0-9a-f]{12}) · (\S+) — (.*)$")


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------
def norm(text):
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n").rstrip() + "\n"


def digest(text):
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()


def split(text):
    """(header dict or None, body). A first line that does not parse as the
    header is body: the user may have deleted or rewritten it."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    first, _, rest = text.partition("\n")
    m = _HEADER_RE.match(first) if first.startswith(HEADER_MARK) else None
    if not m:
        return None, text
    h = {"src": m["src"], "base": int(m["base"]) if m["base"] else None,
         "declined": int(m["declined"]) if m["declined"] else None}
    return h, rest


def header(spec, base, declined=None):
    return (f"{HEADER_MARK}{spec['source']}: base "
            + (f"v{base}" if base is not None else "unknown")
            + (f", declined v{declined}" if declined is not None else "")
            + ". Yours to edit: /claudette update asks before merging a newer version"
              " into an edited copy. -->\n")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def specs():
    """[(feature id, user_copy spec)] for every registry feature carrying one."""
    return [(fid, f["user_copy"]) for fid, f in prefs.features().items()
            if isinstance(f.get("user_copy"), dict)]


def spec_problems(fid, spec):
    """Registry checks for validate_registry(): the path must stay in ~/.claude."""
    out = []
    if not PATH_RE.match(str(spec.get("path", ""))):
        out.append(f"{fid}: user_copy.path must be a bare .md file name")
    if not spec.get("source"):
        out.append(f"{fid}: user_copy.source missing")
    return out


def dest(spec):
    return pathlib.Path(paths.CLAUDE_DIR) / spec["path"]


def source(spec):
    return pathlib.Path(paths.ROOT) / spec["source"]


def versions(spec):
    """[(n, hash12, date, note)] from CHANGELOG.md beside the source, newest first."""
    try:
        lines = (source(spec).parent / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = _CHANGELOG_RE.match(line.strip())
        if m:
            out.append((int(m[1]), m[2], m[3], m[4]))
    return sorted(out, reverse=True)


def shipped(spec):
    """(version, text, sha) of the source as shipped; version None when the
    changelog has no line for this text (a release without its changelog line)."""
    text = source(spec).read_text(encoding="utf-8")
    sha = digest(text)
    n = next((v for v, h, _, _ in versions(spec) if sha.startswith(h)), None)
    return n, norm(text), sha


def tilde(p):
    return paths.tilde(str(p), "~")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def state(fid, spec):
    """Everything the apply, the doctor and the update plan need, as a dict.

    state: missing | untouched | custom. `base` is the shipped version the copy
    started from (None when unknown); `pending` is true when a later version
    ships and the user has not declined it."""
    p = dest(spec)
    out = {"feature": fid, "path": tilde(p), "source": tilde(source(spec)),
           "state": "missing", "base": None,
           "latest": None, "pending": False, "declined": None, "changes": []}
    try:
        latest = shipped(spec)[0]
        out["latest"] = latest
    except OSError as e:
        out["error"] = f"shipped text unreadable: {e}"
        latest = None
    if not p.exists():
        return out
    try:
        h, body = split(p.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        out.update(state="unreadable", error=str(e))
        return out
    by_text = {h12: v for v, h12, _, _ in versions(spec)}.get(digest(body)[:12])
    if by_text is not None:
        # The text IS a shipped version, whatever the header says (or lost).
        out.update(state="untouched", base=by_text)
    else:
        out.update(state="custom", base=h["base"] if h else None)
    out["declined"] = h["declined"] if h else None
    if latest is not None and (out["base"] or 0) < latest and out["declined"] != latest:
        out["pending"] = True
        out["changes"] = [f"v{v} ({d}): {note}" for v, _, d, note in versions(spec)
                          if (out["base"] or 0) < v <= latest]
    return out


def states(effective_only=True):
    return [state(fid, spec) for fid, spec in specs()
            if not effective_only or prefs.effective(fid)]


def write_copy(spec, body, declined=None):
    latest = shipped(spec)[0]
    p = dest(spec)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(header(spec, latest, declined) + norm(body))
    return latest


# --------------------------------------------------------------------------
# Called by features_apply.py
# --------------------------------------------------------------------------
def seed(say=print):
    """Create the copy of every effective feature that has none. Never touches
    an existing file — custom, untouched or of an older version: the update
    flow offers newer text, with a question."""
    for fid, spec in specs():
        if not prefs.effective(fid):
            continue
        p = dest(spec)
        if p.exists():
            st = state(fid, spec)
            if st["state"] == "untouched" and not st["pending"]:
                continue   # the steady state: nothing to say on every apply
            say(f"{spec['path']}: kept ({st['state']}"
                + (f", based on v{st['base']}" if st["base"] else "") + ")"
                + (f" — v{st['latest']} available, /claudette update offers it"
                   if st["pending"] else "") + ".")
            continue
        try:
            n = write_copy(spec, shipped(spec)[1])
        except OSError as e:
            say(f"{spec['path']}: NOT created ({e}) — its import is left out until it exists.")
            continue
        say(f"{spec['path']}: created from v{n} — yours to edit from now on."
            + (f" {_UNSYNCED.format(name=spec['path'])}." if root_ignored(spec) else ""))


def doctor_rows():
    """(feature, ok|warn|off, detail, remedy) per user copy. An edited copy is
    the intended use, so it is `ok`; only a missing copy under an effective
    feature is a fault (its import is dropped, so the feature does nothing)."""
    rows = []
    fix = paths.cmd("features_apply.py", "-y")
    for fid, spec in specs():
        st = state(fid, spec)
        label = f"user copy {spec['path']}"
        if st.get("error") and st["state"] != "missing":
            rows.append((label, "warn", st["error"][:100], ""))
        elif not prefs.effective(fid):
            if st["state"] != "missing":
                rows.append((label, "off", f"{fid} is off — kept on disk, not imported", ""))
        elif st["state"] == "missing":
            rows.append((label, "warn", f"missing — {fid} is on but imports nothing", fix))
        else:
            detail = st["state"] + (f", based on v{st['base']}" if st["base"] else ", base unknown")
            if st["pending"]:
                detail += f"; v{st['latest']} available — /claudette update offers it"
            elif st["declined"]:
                detail += f"; v{st['declined']} declined"
            if root_ignored(spec):
                detail += "; " + _UNSYNCED.format(name=spec["path"])
            rows.append((label, "ok", detail, ""))
    return rows


_UNSYNCED = ("Your ~/.claude repo ignores it, so edits stay on this machine: "
             "add `!{name}` to that repo's .gitignore to share them")


def root_ignored(spec):
    """True when the user's own ~/.claude git repo ignores the copy — an
    allowlist .gitignore ignores every new file — so edits to it never reach
    their other machines, and nothing else would say so. Not a fault (one
    machine needs no sync), hence a note rather than a warn. None when not
    asked: only with my_root_repo_sync on, the user's consent for claudette to run
    git in that repo, and only a read-only check-ignore."""
    if not prefs.enabled("my_root_repo_sync"):
        return None
    if not (pathlib.Path(paths.CLAUDE_DIR) / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", paths.CLAUDE_DIR, "check-ignore", "-q", "--",
                            spec["path"]], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _base_text(spec, version):
    """The shipped text of `version`, found by its changelog hash: the current
    source, else the clone's git history. None when neither has it (a version
    the changelog lacks, a shallow clone, no git)."""
    sha = {v: h12 for v, h12, _, _ in versions(spec)}.get(version)
    if not sha:
        return None
    try:
        if digest(source(spec).read_text(encoding="utf-8")).startswith(sha):
            return norm(source(spec).read_text(encoding="utf-8"))
    except OSError:
        pass
    try:
        log = subprocess.run(["git", "-C", paths.ROOT, "log", "--format=%H", "-n", "300",
                              "--", spec["source"]], capture_output=True, timeout=30)
        for commit in log.stdout.decode().split():
            blob = subprocess.run(["git", "-C", paths.ROOT, "show",
                                   f"{commit}:{spec['source']}"],
                                  capture_output=True, timeout=30)
            if blob.returncode == 0:
                text = blob.stdout.decode("utf-8", errors="replace")
                if digest(text).startswith(sha):
                    return norm(text)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _spec_of(fid):
    found = dict(specs()).get(fid)
    if not found:
        sys.exit(f"no feature with a user copy: {fid} "
                 f"(known: {', '.join(f for f, _ in specs()) or 'none'})")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="include features that are off")
    b = sub.add_parser("base")
    b.add_argument("feature")
    b.add_argument("--out")
    for verb in ("install", "decline"):
        sub.add_parser(verb).add_argument("feature")
    sub.add_parser("hash").add_argument("file")
    w = sub.add_parser("write")
    w.add_argument("feature")
    w.add_argument("--from", dest="src", required=True)
    a = ap.parse_args(argv)

    if a.verb == "status":
        sts = states(effective_only=not a.all)
        if a.json:
            print(json.dumps(sts, indent=2))
        for st in ([] if a.json else sts):
            print(f"{st['path']}: {st['state']}"
                  + (f", based on v{st['base']}" if st["base"] else "")
                  + (f"; v{st['latest']} available" if st["pending"] else "")
                  + "".join(f"\n  {c}" for c in st["changes"]))
        return 0

    if a.verb == "hash":
        print(digest(split(pathlib.Path(a.file).read_text(encoding="utf-8"))[1])[:12])
        return 0

    spec = _spec_of(a.feature)
    st = state(a.feature, spec)
    if a.verb == "base":
        text = _base_text(spec, st["base"]) if st["base"] else None
        if text is None:
            print("base text unavailable (no base version, or not in this clone's history) — "
                  "compare the copy with the latest version directly, and say so", file=sys.stderr)
            return 3
        if a.out:
            pathlib.Path(a.out).write_text(text, encoding="utf-8")
            print(f"base v{st['base']} written to {a.out}")
        else:
            sys.stdout.write(text)
        return 0
    if a.verb == "install":
        if st["state"] == "custom":
            print(f"{st['path']} is edited — merge, then `write {a.feature} --from <file>`",
                  file=sys.stderr)
            return 3
        n = write_copy(spec, shipped(spec)[1])
        print(f"{st['path']}: now v{n}.")
        return 0
    if a.verb == "write":
        try:
            body = pathlib.Path(a.src).read_text(encoding="utf-8")
        except OSError as e:
            sys.exit(f"cannot read {a.src}: {e}")
        n = write_copy(spec, body)
        now = state(a.feature, spec)
        print(f"{st['path']}: written, based on v{n} ({now['state']}).")
        return 0
    if a.verb == "decline":
        if not st["pending"]:
            print(f"{st['path']}: nothing pending to decline.")
            return 0
        p = dest(spec)
        _, body = split(p.read_text(encoding="utf-8"))
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(header(spec, st["base"], st["latest"]) + body)
        print(f"{st['path']}: v{st['latest']} declined — offered again only by a later version.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
