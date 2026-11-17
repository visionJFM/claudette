#!/usr/bin/env python3
"""Per-project parameter overrides for SCRIPT features.

    overrides.py resolve <param> --repo <root>
    overrides.py set <param> <json> --repo <root> --local|--shared --note <why>

A parameter resolves through four layers, most specific last:

    registry default  ->  machine (prefs.local.json)  ->  <repo>/.claudette.json
                                                      ->  <repo>/.claudette.local.json

`.claudette.json` is committed and shared by every clone; `.claudette.local.json`
is one machine's, kept out of git (`.git/info/exclude`). Only a parameter whose
registry `param_rules` entry says `"project": true` takes the project layers:
those are read by a script at the moment it runs, in the repo it runs for. A
CONFIG-WRITING feature cannot honour a project layer at all — its output is one
user-wide settings.json / CLAUDE.md, and a per-project exception would mean
writing into each project's own config files (CLAUDETTE_REGISTRY.md).

Project file shape (schema_version absent means 1):
    {"schema_version": 1,
     "params": {"leak_scan_classes": {"remove": ["host"]}},
     "notes":  {"leak_scan_classes": "why this repo needs it"}}

Rules, all printed as MSG lines when they refuse something — nothing is dropped
silently:
  - `merge: "set"` params change by `add` / `remove` only, never by replacing the
    list: a member the registry adds later then reaches every repo. A list given
    where a delta is expected is refused.
  - every project override needs a non-empty `notes` entry for its parameter.
  - `shared_may_remove` bounds what the SHARED file may remove; anything else is
    removable from the local file only (one commit must not disarm every clone).
  - a param whose default is a bool takes only true/false from any layer.
  - `add` and `remove` of the same member in one layer: add wins.
  - a `.claudette.local.json` that git tracks is not local any more: not applied.
  - unreadable / invalid / unknown-schema file: that file is not applied.
  - a `.claudette.json` git does not carry (ignored, or not committed yet) is still
    applied here, and says it reaches no other clone.
  - git unable to answer counts as the unsafe answer, and says so.
No quiet path: every refusal, and every file that does less than its name says,
prints a MSG. Only an ABSENT file is silent — and COVERAGE still says what ran.

Output, one per line (tab-separated fields, no tabs or newlines inside):
    VALUE <json>                          the resolved value
    FROM  <layer>\t<note>                 which layer set a scalar (absent: default)
    SKIP  <member>\t<layer>\t<note>       a set member some layer removed
    MSG   <text>                          something not applied, and why
Exit 0 always once arguments parse — a consumer that cannot use the output falls
back to the registry default and says so.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prefs  # noqa: E402

SCHEMA_VERSION = 1
SHARED, LOCAL = ".claudette.json", ".claudette.local.json"
KNOWN_KEYS = {"schema_version", "params", "notes", "features"}


def _one_line(s):
    return " ".join(str(s).split())


def _owner(name):
    for fid, fdef in prefs.features().items():
        if name in (fdef.get("params") or {}):
            return fid, fdef
    return None, None


def _git_says(repo, *args):
    """True / False from a git exit code, None when git could not answer."""
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    except Exception:
        return None
    return {0: True, 1: False}.get(p.returncode)


def _tracked(repo, fname):
    # ls-files --error-unmatch exits 1 for "not tracked"; anything else is no answer.
    return _git_says(repo, "ls-files", "--error-unmatch", "--", fname)


def _ignored(repo, fname):
    return _git_says(repo, "check-ignore", "-q", "--", fname)


def _load(repo, fname, msgs):
    """The file's parsed object, or None (absent, or refused with a MSG)."""
    path = os.path.join(repo, fname)
    if not os.path.exists(path):
        return None
    tracked = _tracked(repo, fname)
    if fname == LOCAL and tracked is not False:
        # Unknown counts as tracked: a local file every clone might carry is the
        # door the shared-removal bound exists to close.
        why = ("tracked by git, so every clone gets it" if tracked
               else "git could not say whether it is tracked")
        msgs.append(f"{fname}: {why} — not applied "
                    f"(put shared overrides in {SHARED}; keep this one untracked)")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        msgs.append(f"{fname}: unreadable or invalid JSON ({_one_line(e)}) — not applied")
        return None
    if not isinstance(data, dict):
        msgs.append(f"{fname}: top level is not an object — not applied")
        return None
    ver = data.get("schema_version", SCHEMA_VERSION)
    if ver != SCHEMA_VERSION or isinstance(ver, bool):
        msgs.append(f"{fname}: schema_version {ver!r} not understood by this claudette "
                    f"(knows {SCHEMA_VERSION}) — not applied")
        return None
    for k in sorted(set(data) - KNOWN_KEYS):
        msgs.append(f"{fname}: unknown key {k!r} ignored (known: {', '.join(sorted(KNOWN_KEYS))})")
    feats = data.get("features")
    if feats is not None and not isinstance(feats, dict):
        msgs.append(f"{fname}: features is not an object — not applied")
    for fid in sorted(feats if isinstance(feats, dict) else {}):
        msgs.append(f"{fname}: features.{fid} not applied — no feature supports "
                    f"per-project on/off yet (params only)")
    ignored = _ignored(repo, fname)
    if fname == LOCAL and ignored is not True:
        msgs.append(f"{fname}: " + ("not ignored by git" if ignored is False else
                    "git could not say whether it is ignored")
                    + " — add it to .git/info/exclude so it cannot be committed")
    if fname == SHARED and tracked is not True:
        # A "shared" file git never carries is one machine's file under a shared
        # name: applied here, silently absent on every other clone. An allowlist
        # .gitignore (`*` then `!…`) does exactly that to a new root file.
        if ignored:
            why = ("ignored by git, so no other clone gets it — un-ignore it "
                   f"(e.g. `!{SHARED}` in .gitignore) and commit it")
        elif tracked is None or ignored is None:
            why = "git could not say whether it is tracked — other clones may not get it"
        else:
            why = "not committed yet — other clones get it once it is"
        msgs.append(f"{fname}: applied on this machine only: {why}")
    return data


def _apply_set(value, delta, layer, rules, msgs, skips, note=""):
    """One layer's {add, remove} applied to `value` (a list, order kept)."""
    if not isinstance(delta, dict) or set(delta) - {"add", "remove"}:
        msgs.append(f"{layer}: expected {{\"add\": [...], \"remove\": [...]}} — not applied "
                    f"(a whole list would silently drop members added later)")
        return value
    members = rules.get("members")
    ops = {}
    for op in ("add", "remove"):
        got = delta.get(op) or []
        if not isinstance(got, list) or not all(isinstance(m, str) for m in got):
            msgs.append(f"{layer}: {op} must be a list of names — not applied")
            return value
        ok = []
        for m in got:
            if members is not None and m not in members:
                msgs.append(f"{layer}: unknown member {m!r} in {op}, ignored "
                            f"(known: {', '.join(members)})")
            else:
                ok.append(m)
        ops[op] = ok
    both = [m for m in ops["remove"] if m in ops["add"]]
    for m in both:
        msgs.append(f"{layer}: {m!r} is in both add and remove — add wins")
    if layer == SHARED and "shared_may_remove" in rules:
        allowed = rules["shared_may_remove"]
        for m in [m for m in ops["remove"] if m not in allowed and m not in both]:
            msgs.append(f"{layer}: removing {m!r} is refused in the shared file — "
                        f"only {LOCAL} may remove it; still runs")
        ops["remove"] = [m for m in ops["remove"] if m in allowed or m in both]
    out = list(value)
    for m in ops["remove"]:
        if m in both:
            continue
        if m in out:
            out.remove(m)
        skips[m] = (layer, note)
    for m in ops["add"]:
        if m not in out:
            out.append(m)
        skips.pop(m, None)
    return out


def _scalar_ok(value, default, layer, name, rules, msgs):
    """May this layer's scalar replace the value? Refusals print."""
    if isinstance(default, bool) and not isinstance(value, bool):
        # "true", 1 and "no" are all truthy: a string where a switch is
        # expected must not quietly switch it on.
        msgs.append(f"{layer}: {name} must be true or false, got {json.dumps(value)} — not applied")
        return False
    return True


def resolve(name, repo):
    """(value, skips {member: (layer, note)}, msgs [str])."""
    return resolve_full(name, repo)[:3]


def resolve_full(name, repo):
    """(value, skips, msgs, source): source is (layer, note) of the layer that
    set a SCALAR's value, None when the registry default stands. A switch that
    is off in one repo has to be able to say which file turned it off."""
    msgs, skips, source = [], {}, None
    fid, fdef = _owner(name)
    if fdef is None:
        default = (prefs.defs().get("params") or {}).get(name)
        rules = {}
    else:
        default = fdef["params"][name]
        rules = (fdef.get("param_rules") or {}).get(name) or {}
    is_set = rules.get("merge") == "set"
    value = list(default) if is_set else default

    machine = prefs.param_overrides()
    if name in machine:
        if is_set:
            value = _apply_set(value, machine[name], "machine prefs", rules, msgs, skips,
                               "prefs.local.json")
        elif _scalar_ok(machine[name], default, "machine prefs", name, rules, msgs):
            value, source = machine[name], ("machine prefs", "prefs.local.json")

    if repo:
        for fname in (SHARED, LOCAL):
            data = _load(repo, fname, msgs)
            if data is None:
                continue
            params = data.get("params") or {}
            notes = data.get("notes") or {}
            if not isinstance(params, dict) or not isinstance(notes, dict):
                msgs.append(f"{fname}: params and notes must be objects — not applied")
                continue
            for p in sorted(params):
                if p == name:
                    continue
                if _owner(p)[1] is None and p not in (prefs.defs().get("params") or {}):
                    msgs.append(f"{fname}: unknown parameter {p!r} ignored")
            for n in sorted(notes):
                if n not in params:
                    msgs.append(f"{fname}: notes.{n} has no params entry — nothing to explain")
            if name not in params:
                continue
            if not rules.get("project"):
                msgs.append(f"{fname}: {name} is machine-level only — not applied")
                continue
            note = notes.get(name)
            if not isinstance(note, str) or not note.strip():
                msgs.append(f"{fname}: {name} has no notes entry saying why — not applied")
                continue
            if is_set:
                value = _apply_set(value, params[name], fname, rules, msgs, skips,
                                   _one_line(note))
            elif _scalar_ok(params[name], default, fname, name, rules, msgs):
                value, source = params[name], (fname, _one_line(note))
    return value, skips, msgs, source


def _exclude_local(repo):
    """Make sure .git/info/exclude names LOCAL. git-path finds the COMMON
    dir's file, which is where a linked worktree's excludes live. Returns an
    error string, or None.

    The path is read as UTF-8 (non-ASCII repo path)."""
    try:
        p = subprocess.run(["git", "-C", repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "info/exclude"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    except Exception as e:
        return f"git failed ({_one_line(e)})"
    path = p.stdout.strip()
    if p.returncode != 0 or not path:
        return "git could not locate info/exclude"
    try:
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
    except Exception as e:
        return f"cannot read {path} ({_one_line(e)})"
    wanted = "/" + LOCAL
    if any(line.strip() in (wanted, LOCAL) for line in text.splitlines()):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(wanted + "\n")
    return None


def set_override(name, value, repo, local, note):
    """Write one param override into the repo's shared or local file.
    Returns (ok, [lines to print]). Refuses rather than overwrite anything it
    cannot read back whole."""
    fname = LOCAL if local else SHARED
    fid, fdef = _owner(name)
    if fdef is None:
        return False, [f"unknown parameter {name!r}"]
    rules = (fdef.get("param_rules") or {}).get(name) or {}
    if not rules.get("project"):
        return False, [f"{name} is machine-level only — set it with /claudette prefs"]
    if not isinstance(note, str) or not note.strip():
        return False, [f"a note saying why is required for every override"]
    default = fdef["params"][name]
    if rules.get("merge") == "set":
        if not isinstance(value, dict) or set(value) - {"add", "remove"}:
            return False, ['expected {"add": [...], "remove": [...]}']
    elif isinstance(default, bool) and not isinstance(value, bool):
        return False, [f"{name} must be true or false"]
    if local and _tracked(repo, LOCAL) is not False:
        return False, [f"{LOCAL} is tracked by git (or git could not say) — not written"]
    path = os.path.join(repo, fname)
    data = {"schema_version": SCHEMA_VERSION}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            return False, [f"{fname} is not valid JSON ({_one_line(e)}) — fix it by hand, not overwritten"]
        if not isinstance(data, dict) or data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            return False, [f"{fname} is not a schema_version {SCHEMA_VERSION} object — not overwritten"]
        for k in ("params", "notes"):
            if k in data and not isinstance(data[k], dict):
                return False, [f"{fname}: {k} is not an object — not overwritten"]
    data.setdefault("params", {})[name] = value
    data.setdefault("notes", {})[name] = _one_line(note)
    out = []
    if local:
        err = _exclude_local(repo)
        if err:
            out.append(f"MSG {LOCAL} written but not excluded from git: {err}")
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    out.insert(0, f"WROTE {path}")
    return True, out


USAGE = ("usage: overrides.py resolve <param> [--repo <root>]\n"
         "       overrides.py set <param> <json> --repo <root> --local|--shared --note <why>")


def main(argv):
    if argv[:1] == ["set"]:
        args = argv[1:]
        opts = {}
        pos = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--repo", "--note") and i + 1 < len(args):
                opts[a], i = args[i + 1], i + 2
            elif a in ("--local", "--shared"):
                opts["scope"] = a if opts.get("scope", a) == a else "both"  # both = refused
                i += 1
            else:
                pos.append(a)
                i += 1
        if len(pos) != 2 or "--repo" not in opts or opts.get("scope") not in ("--local", "--shared"):
            print(USAGE, file=sys.stderr)
            return 2
        try:
            value = json.loads(pos[1])
        except Exception:
            print(f"value is not JSON: {pos[1]!r} (e.g. true, false, "
                  '\'{"remove": ["host"]}\')', file=sys.stderr)
            return 2
        ok, lines = set_override(pos[0], value, opts["--repo"], opts["scope"] == "--local",
                                 opts.get("--note", ""))
        for line in lines:
            print(line if ok or line.startswith("MSG ") else "REFUSED " + line)
        if not ok:
            return 1
        # What the repo resolves to now, refusals included: a shared value this
        # machine does not accept must say so the moment it is written.
        return main(["resolve", pos[0], "--repo", opts["--repo"]])
    if len(argv) < 2 or argv[0] != "resolve":
        print(USAGE, file=sys.stderr)
        return 2
    name, repo = argv[1], None
    rest = argv[2:]
    if rest[:1] == ["--repo"] and len(rest) >= 2:
        repo = rest[1]
    elif rest:
        print(USAGE, file=sys.stderr)
        return 2
    value, skips, msgs, source = resolve_full(name, repo)
    print("VALUE " + json.dumps(value))
    if source:
        print(f"FROM {source[0]}\t{source[1]}")
    for m, (layer, note) in skips.items():
        print(f"SKIP {m}\t{layer}\t{note}")
    for m in msgs:
        print("MSG " + _one_line(m))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
