#!/usr/bin/env python3
"""Apply claudette's features to THIS machine — deterministically, from the registry.

`features_def.json` says what claudette can do; `prefs.local.json` says what this
machine has agreed to; this script is the only thing that turns the two into
config: the active `~/.claude/settings.json`, the plugin's `hooks/hooks.json`,
and the generated rules leaf `~/.claude/claudette-instructions.generated.md`
(imported by `~/.claude/CLAUDE.md`, which claudette otherwise never touches).

**Nothing is on until it has been answered for.** An absent feature key means off
and unanswered; `--update` lists everything unanswered with its description and
recommended setting and asks. That is also the install flow: clone, update,
restart.

It never pulls. `update` in the /claudette skill is the whole flow — pull, this,
report.

Modes:
  (none)             apply the current prefs
  --update           prompt for unanswered features, then apply
  --prefs            review/toggle every feature, then apply
  --accept-defaults  answer every unanswered feature with its recommendation
  --feat id=on|off   answer one feature (=null unanswers), then apply
  --param name=value set one parameter override (=null reverts), then apply
  -s / --status      show what would change; write nothing
  --features-json    the registry + this machine's state, for the skill to render
  --doctor           per-feature health, each problem with its fix
  --auth             macOS Keychain probe only; touches no config file

Conflict handling while applying: `-y` keeps the local value, `--take-source`
takes the registry's, otherwise you are asked.

Every mode that changes something mirrors its output to `logs/config_runs/`
(one file per run, ANSI stripped, newest 20 kept); the read-only modes above
write nothing. `pull.sh` opens that file first and passes it down, so the git
output and the apply it caused end up in one file.
"""
import argparse
import copy
import difflib
import importlib.util
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import prefs
import python_resolve
import redact
import user_copy

if platform.system() == "Windows":
    os.system("")  # Enable ANSI escape codes on Windows console

parser = argparse.ArgumentParser()
parser.add_argument("-s", "--status", action="store_true",
                    help="show settings/CLAUDE.md diff and dependency status only")
parser.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                    help="no prompts; keep the local value on conflict")
parser.add_argument("--take-source", action="store_true", dest="take_source",
                    help="no prompts; take the registry value on conflict")
parser.add_argument("--update", action="store_true",
                    help="prompt for features this machine has never answered for, then apply")
parser.add_argument("--prefs", action="store_true",
                    help="review and toggle every feature, then apply")
parser.add_argument("--accept-defaults", action="store_true", dest="accept_defaults",
                    help="answer every unanswered feature with its recommended setting")
parser.add_argument("--features-json", action="store_true", dest="features_json",
                    help="print the registry plus this machine's state as JSON")
parser.add_argument("--only", choices=("all", "needs-decision"), default="all",
                    help="with --features-json: 'needs-decision' keeps only the features "
                         "an update must present (unanswered, substrate present, not "
                         "already aligned) — what the skill renders, without paying for "
                         "the rest of the registry")
parser.add_argument("-f", "--feat", action="append", default=[], metavar="id=on|off",
                    help="answer a feature, then apply; repeatable "
                         "(e.g. --feat audible_failure=off; =null unanswers it). "
                         "How the skill writes the choices it collected conversationally.")
parser.add_argument("-p", "--param", action="append", default=[], metavar="name=value",
                    help="set a parameter override, then apply; repeatable "
                         "(e.g. --param audible_failure_volume=40; =null reverts to default)")
parser.add_argument("--set", action="append", default=[], help=argparse.SUPPRESS)
parser.add_argument("--suspend", action="append", default=[], metavar="zone",
                    help="disable a zone's effect (audio, claude_md, settings, "
                         "hooks, machinery, or 'all') — reversible, answers kept; "
                         "repeatable, then apply")
parser.add_argument("--resume", action="append", default=[], metavar="zone",
                    help="re-enable a suspended zone ('all' for every zone), "
                         "then apply; repeatable")
parser.add_argument("--uninstall", action="store_true",
                    help="revert everything claudette put on this machine "
                         "(settings from the undo records, hooks, CLAUDE.md block, "
                         "tones); keeps answers, prefs, logs and the clone — "
                         "deleting those is a separate question it prints, never runs")
parser.add_argument("--preview-tone", metavar="feature_id", dest="preview_tone",
                    help="synthesize and play one tone feature's sound with the "
                         "current parameters, without installing anything — how "
                         "audio is auditioned BEFORE its hooks exist")
parser.add_argument("--preview-speak", nargs="?", const="idle prompt", metavar="text",
                    dest="preview_speak",
                    help="speak a line with the current voice parameters, without "
                         "installing anything — the audible_speak_* counterpart of "
                         "--preview-tone (default text matches the real idle cue)")
parser.add_argument("--doctor", action="store_true",
                    help="per-feature health report with the exact command to fix each problem")
parser.add_argument("--update-plan", action="store_true", dest="update_plan",
                    help="small JSON saying whether an update needs any conversation at all "
                         "(read-only, repairs nothing) — the /claudette update fast path; "
                         "pull.sh appends it after the apply")
parser.add_argument("--features-table", action="store_true", dest="features_table",
                    help="one line per feature: state, id, name, params — the compact "
                         "/claudette prefs listing; decision fields on demand via --explain")
parser.add_argument("--explain", metavar="id[,id]",
                    help="print the four decision fields (what/benefit/cost/alternative) "
                         "for the named feature(s), read-only")
parser.add_argument("--auth", action="store_true",
                    help="authorize macOS Keychain for scoped usage limits ONLY (touches no config file)")
parser.add_argument("--md-dirt", action="store_true", dest="md_dirt",
                    help="print where the working CLAUDE.md differs from HEAD in its repo: "
                         "clean | managed (inside the claudette block only) | outside | "
                         "unknown — read-only, used by pull.sh before pulling the root repo")
args = parser.parse_args()
if args.take_source:
    args.yes = True

if os.environ.get("NO_COLOR"):
    RED = GREEN = CYAN = GRAY = BOLD = RESET = ""
else:
    RED, GREEN, CYAN = "\033[31m", "\033[32m", "\033[36m"
    GRAY, BOLD, RESET = "\033[90m", "\033[1m", "\033[0m"

# The report modes are what the run-log gate below trusts to be read-only, so a
# call that also carries a writing verb must die here rather than write config
# with no run log.
if ((args.update_plan or args.features_table or args.explain or args.md_dirt)
        and (args.feat or args.param or args.set or args.suspend or args.resume
             or args.update or args.prefs or args.accept_defaults or args.uninstall)):
    print(f"{RED}--update-plan/--features-table/--explain/--md-dirt are read-only reports{RESET} "
          "— make changes in a separate call (--feat/--param).")
    sys.exit(2)


# --------------------------------------------------------------------------
# Run log (logs/config_runs/<iso>_<pid>.log — CLAUDETTE_LOGS.md)
#
# Every run that CHANGES durable state mirrors its whole output to a file, so a
# caller that truncates (a pipe through `tail`, an agent reading the last N
# lines) cannot destroy the record. The log is opened by whichever entry point
# starts the run (pull.sh, for the git output) and handed down, never created
# here when a parent already has one.
#
# Read-only invocations stay silent: -s, --features-json, --doctor, --auth.
# --preview-tone/--preview-speak are excluded too, though a paired --param does
# write prefs: they are adjust-and-listen loops, and logging each audition
# would prune real applies out of the retained 20 for no audit value.
# --------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _Tee:
    """Mirror a stream into the run log, ANSI stripped and masked (redact.py),
    terminal unchanged.

    `isatty()` MUST pass through to the real stream: consent depends on it —
    --update/--prefs prompt only on a TTY, and a wrapper that reported False
    would silently turn an interactive review into accept-the-defaults.

    The log copy is masked a whole line at a time, since print() hands over a
    line in pieces and a credential split across two writes would match no
    pattern; flush() writes the unfinished rest."""

    def __init__(self, stream, fh):
        self._stream, self._fh = stream, fh
        self._pending = ""

    def _log(self, text):
        try:
            self._fh.write(redact.mask(_ANSI_RE.sub("", text)))
        except Exception:
            pass          # a full disk must not take the apply down with it

    def write(self, data):
        n = self._stream.write(data)
        self._pending += data
        if "\n" in self._pending:
            done, _, self._pending = self._pending.rpartition("\n")
            self._log(done + "\n")
        return n

    def flush(self):
        self._stream.flush()
        if self._pending:
            self._log(self._pending)
            self._pending = ""
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        return self._stream.isatty()

    def fileno(self):
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


RUN_LOG = None
if not (args.status or args.features_json or args.doctor or args.auth
        or args.preview_tone or args.preview_speak or args.update_plan
        or args.features_table or args.md_dirt
        or args.explain):
    try:
        RUN_LOG = paths.run_log()
        # Line buffered: the log stays complete even if the run is killed
        # mid-way, which is exactly when it is worth reading.
        _run_fh = open(RUN_LOG, "a", encoding="utf-8", errors="replace", buffering=1)
        _run_fh.write(redact.header("apply", sys.argv[1:] or ["(no args)"]) + "\n")
        sys.stdout = _Tee(sys.stdout, _run_fh)
        sys.stderr = _Tee(sys.stderr, _run_fh)
    except Exception:
        RUN_LOG = None    # never let logging block an apply
    if RUN_LOG:
        # Logs from before masking may hold a credential typed on a push's
        # command line; mask them in place (CLAUDETTE_DESIGN.md "Run logs are
        # masked, and pull failures quote git"). Cheap and idempotent, so it
        # needs no marker.
        try:
            redact.scrub_logs(paths.CONFIG_RUNS, skip=RUN_LOG)
        except Exception:
            pass

CLAUDE_DIR = pathlib.Path(paths.CLAUDE_DIR)
SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
# The generated rules leaf: whole-file claudette-owned, imported by CLAUDE.md,
# untracked (the root repo's `*` catch-all) because its content is
# f(tracked registry × per-machine prefs) — build output, never shared state.
LEAF_NAME = "claudette-instructions.generated.md"
LEAF = CLAUDE_DIR / LEAF_NAME
HOOKS_JSON = pathlib.Path(paths.ROOT) / "hooks" / "hooks.json"

# Identity of a claudette-managed settings entry, read out of its command string:
# a `claudette/[scripts/]<name>.py` path, or a `# claudette:<feature>` tag for a
# scriptless one-liner. `scripts/` is optional.
_SCRIPT_RE = re.compile(r"claudette/(?:scripts/)?([\w.]+\.py)")
_TAG_RE = re.compile(r"claudette:([\w.-]+)")
_PARAM_RE = re.compile(r"\{P:([\w.]+)\}")

REGISTRY_VERSION = 3  # newest features_def.json schema this script understands


def validate_registry():
    """What apply refuses to do (contract §13): rather than guessing, die loudly
    on a registry this script cannot be trusted to interpret."""
    problems = []
    version = prefs.defs().get("version") or 0
    if version > REGISTRY_VERSION:
        problems.append(f"registry version {version} is newer than this script "
                        f"(understands {REGISTRY_VERSION}) — pull claudette and retry")
    known_params = prefs.param_defaults()
    groups = prefs.groups()
    zones = prefs.zones()
    for fid, fdef in prefs.features().items():
        if fdef.get("default") not in (True, False, None):
            problems.append(f"{fid}: default must be true, false or null")
        if fdef.get("group") not in groups:
            problems.append(f"{fid}: group '{fdef.get('group')}' not in groups")
        if fdef.get("zone") not in zones:
            problems.append(f"{fid}: zone '{fdef.get('zone')}' not in zones — "
                            f"every feature must name its kill switch")
        if not (fdef.get("what") or fdef.get("description")):
            problems.append(f"{fid}: needs a `what` (what does it do?)")
        for pname in _PARAM_RE.findall(json.dumps(fdef)):
            if pname not in known_params:
                problems.append(f"{fid}: {{P:{pname}}} references a parameter nobody defines")
        if "user_copy" in fdef:
            problems.extend(user_copy.spec_problems(fid, fdef["user_copy"] or {}))
        if fdef.get("hooks_restart", True) not in (True, False):
            problems.append(f"{fid}: hooks_restart must be true or false")
        rb = fdef.get("recommend_by")
        if rb is not None:
            if not isinstance(rb, dict) or rb.get("fact") not in prefs._FACTS:
                problems.append(f"{fid}: recommend_by needs a known `fact` "
                                f"({', '.join(sorted(prefs._FACTS))})")
            else:
                for state, alt in rb.items():
                    if state == "fact":
                        continue
                    if not isinstance(alt, dict) or set(alt) - {"default", "recommend_note"} \
                            or alt.get("default", True) not in (True, False, None):
                        problems.append(f"{fid}: recommend_by.{state} takes only `default` "
                                        f"(true/false/null) and `recommend_note`")
    if problems:
        print(f"{RED}features_def.json refused:{RESET}\n  " + "\n  ".join(problems))
        sys.exit(2)


# --------------------------------------------------------------------------
# Registry -> config
# --------------------------------------------------------------------------
# Feature whose claude_md lines lead the generated rules leaf (see assemble()).
MD_FIRST = "claude_md_claudette_pointer"


def _expand(obj):
    """Substitute the layout and parameter placeholders: {PY} the interpreter
    (absolute path resolved for this machine), {S} scripts dir in $HOME form
    (bash command strings), {R} claudette root in ~ form (file_path rules and
    docs; {T} kept as a legacy alias), {P:name} a parameter. One registry,
    any layout, any machine."""
    s = paths.tilde(paths.SCRIPTS, "$HOME")
    t = paths.tilde(paths.ROOT, "~")
    if isinstance(obj, str):
        # {PY}: the interpreter word for hook/statusline commands — the
        # absolute path stored for this machine, or the PATH-lookup chain
        # while none is (python_resolve.render).
        out = (obj.replace("{PY}", python_resolve.render(prefs.param("python", "")))
               .replace("{S}", s).replace("{R}", t).replace("{T}", t))
        return _PARAM_RE.sub(lambda m: str(prefs.param(m.group(1), "")), out)
    if isinstance(obj, dict):
        out = {k: _expand(v) for k, v in obj.items()}
        if "settings_min" in out:
            # A FLOOR, folded into the ordinary payload here so that capture,
            # undo, drift and status all treat it like any other setting — only
            # the two places where "equal" is the wrong test know the difference
            # (_deep_merge takes the max, _has_payload accepts >=). Declaring it
            # separately in the registry is what marks those paths; see _OP_PATHS.
            out["settings_min"] = _numeric_leaves(out["settings_min"])
            out["settings"] = _merge_into(dict(out.get("settings") or {}),
                                          out["settings_min"])
        return out
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    return obj


def _numeric_leaves(payload):
    """A bound is numeric by definition, so coerce its leaves and drop the rest.

    Bounds are written as parameters ("{P:history_floor_days}") so a machine can
    raise or lower its own threshold through the ordinary prefs mechanism — no
    parallel "params_min" concept. But {P:…} renders to a STRING, and so does the
    --update prompt that stores an override, so without this the payload would
    write "365" into settings.json and every comparison below would refuse the
    pair. A leaf that is not a number after coercion is DROPPED, not written: a
    mistyped threshold must not become a garbage value in settings.json.
    """
    out = {}
    for k, v in payload.items():
        if isinstance(v, dict):
            out[k] = _numeric_leaves(v)
            continue
        n = v
        if isinstance(n, str):
            try:
                n = int(n.strip())
            except ValueError:
                try:
                    n = float(n.strip())
                except ValueError:
                    pass
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            print(f"  WARNING: bound '{k}' is not a number ({v!r}) — ignoring it")
            continue
        out[k] = n
    return out


# {dotted path: op}. Each op is a registry field plus one entry here, not a
# generic "settings_fn: {op, value}": each op owes four answers that cannot be
# factored out — what "satisfied" means to _has_payload, what undo restores,
# whether a difference is drift, and what --status diffs — and an unknown op
# string fails silently where an unknown registry FIELD does not.
_OPS = {"min": max}
_OP_PATHS = {}


def _op_paths():
    """Dotted paths declared as bounds by the REGISTRY — every feature, answered
    or not.

    A bound says "at least this much", so a larger local value is not a conflict
    to be resolved — it already satisfies the guarantee and must survive
    untouched. That is the whole semantic; everything else about the key behaves
    like a normal setting.

    Not gated on `effective()`: align_unanswered (I3) asks `_has_payload`
    whether an UNANSWERED feature's desired state already holds, so a bound must
    be visible there, or a machine already above the floor is asked to approve a
    change that does nothing. A bound path is a property of the registry, which
    cannot change mid-process (answers can), so the memo below is sound.
    Non-effective features stay inert regardless: assemble() never puts their
    payload in `src_settings`, so the merge never sees those paths.
    """
    if _OP_PATHS:
        return _OP_PATHS
    for _fid, fdef in prefs.features().items():
        for op in _OPS:
            payload = _expand(fdef).get("settings_%s" % op) or {}
            for path, _prior, _wrote in _payload_leaves({}, payload):
                _OP_PATHS[".".join(path)] = op
    return _OP_PATHS


def _apply_op(kpath, local, source):
    """(take_it, value) for a bounded path holding two comparable numbers."""
    op = _op_paths().get(kpath)
    if not op:
        return False, None
    if isinstance(local, bool) or isinstance(source, bool):
        return False, None
    if not (isinstance(local, (int, float)) and isinstance(source, (int, float))):
        return False, None
    return True, _OPS[op](local, source)


def _merge_into(dst, src):
    """Plain recursive dict update, used to fold feature payloads together."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_into(dst[k], v)
        else:
            dst[k] = v
    return dst


def assemble():
    """(settings, hooks, claude_md_text, fingerprints) from the ENABLED features.

    In plugin mode the hooks go to the plugin's own hooks.json and are kept OUT
    of settings.json — Claude Code merges plugin hooks itself, and two copies of
    every hook would fire twice.

    MD_FIRST leads the generated leaf whatever its position in the registry: it
    is the line that says claudette exists and where the guide is, so a session
    reading top-down learns what generated the rest before it reads the rest.
    The sort is stable, so every other feature keeps registry order.
    """
    settings, hooks, allow, md, fps = {}, {}, [], [], set()
    for fid, fdef in sorted(prefs.features().items(), key=lambda kv: kv[0] != MD_FIRST):
        if not prefs.effective(fid):  # answered on AND zone not suspended (I8)
            continue
        f = _expand(fdef)
        if f.get("settings"):
            _merge_into(settings, f["settings"])
        for event, entries in (f.get("hooks") or {}).items():
            hooks.setdefault(event, []).extend(entries)
            for entry in entries:
                fps.add(_fingerprint(entry))
        allow.extend(f.get("permissions") or [])
        md_lines = f.get("claude_md") or []
        if md_lines and f.get("user_copy") and not user_copy.dest(f["user_copy"]).exists():
            # Its lines import the copy: without it they would load as literal
            # text. The apply seeds the copy before this runs, so only a failed
            # write (or -s before the first apply) lands here; doctor warns.
            md.append(f"<!-- from claudette feature: {fid} — left out, "
                      f"{f['user_copy']['path']} missing -->")
            md_lines = []
        if md_lines:
            # provenance comment: HTML comments are context-free (not injected
            # into the context window), so this costs display space only
            md.append(f"<!-- from claudette feature: {fid} -->")
            md.extend(md_lines)
    if allow:
        settings.setdefault("permissions", {}).setdefault("allow", []).extend(allow)
    if hooks and not paths.IS_PLUGIN:
        settings["hooks"] = hooks
    md_text = ("\n".join(md) + "\n") if md else ""
    return settings, hooks, md_text, fps


def _payload_leaves(dst, payload, prefix=()):
    """The leaf paths of a feature's settings payload, each with the prior
    value found in `dst` — the walk mirrors the merge's granularity: a
    command-bearing managed dict (statusLine) is ONE leaf, a plain dict (env)
    recurses so user-added siblings inside it are never claimed.
    Returns [(path_list, prior, wrote)]; prior is {"value": x} or
    {"absent": true} — the distinction an undo cannot live without."""
    out = []
    for key, val in payload.items():
        p = prefix + (key,)
        if isinstance(val, dict) and not ("command" in val and _fingerprint(val)):
            sub = dst.get(key) if isinstance(dst, dict) else None
            out.extend(_payload_leaves(sub if isinstance(sub, dict) else {}, val, p))
        else:
            if isinstance(dst, dict) and key in dst:
                out.append((list(p), {"value": copy.deepcopy(dst[key])}, copy.deepcopy(val)))
            else:
                out.append((list(p), {"absent": True}, copy.deepcopy(val)))
    return out


def write_hooks_json(hooks, dry=False):
    """Plugin mode only: the generated, machine-local hooks declaration.

    Generated rather than tracked so switching a feature off actually removes
    its hook, instead of every script having to check prefs at run time;
    untracked so that removal never shows up as repo churn.
    """
    text = json.dumps({"hooks": hooks}, indent=2, sort_keys=True) + "\n"
    if HOOKS_JSON.exists() and HOOKS_JSON.read_text(encoding="utf-8") == text:
        return False
    if not dry:
        HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
        HOOKS_JSON.write_text(text, encoding="utf-8")
    return True


def _restart_hooks(hooks):
    """The hook entries a restart is needed for, as a comparable string: all of
    them except those of features declaring `hooks_restart: false`. Such a
    hook only matters to processes started after the apply (a SessionStart
    guard for `claude -p` runs), so writing it asks nothing of the user."""
    live = set()
    for fdef in prefs.features().values():
        if fdef.get("hooks_restart", True) is False:
            for entries in (_expand(fdef).get("hooks") or {}).values():
                live.update(_fingerprint(e) for e in entries)
    kept = {}
    for event, entries in (hooks or {}).items():
        keep = [e for e in entries if _fingerprint(e) not in live]
        if keep:
            kept[event] = keep
    return json.dumps(kept, sort_keys=True)


def prune_managed_hooks(dst, keep):
    """Drop claudette-owned hook entries from the ACTIVE settings.json that the
    current feature set does not ask for. In plugin mode `keep` is empty: the
    plugin's hooks.json owns them all. Entries the user added themselves have no
    claudette fingerprint and are never touched."""
    removed = 0
    events = dst.get("hooks") or {}
    for event in list(events):
        kept = [e for e in events[event]
                if not _fingerprint(e) or _fingerprint(e) in keep]
        removed += len(events[event]) - len(kept)
        if kept:
            events[event] = kept
        else:
            del events[event]
    if not events:
        dst.pop("hooks", None)
    return removed


def _is_claudette_rule(rule):
    """Does this permission rule point into claudette's own files? Rules the
    user added for their own paths must survive a feature being switched off."""
    return "/claudette" in rule.replace("\\", "/")


def prune_allow(dst, wanted):
    """Remove claudette-owned permission rules that the current feature set does
    not ask for — including `~/.claude/claudette/` ones, which point at a folder
    that does not exist."""
    allow = ((dst.get("permissions") or {}).get("allow")) or []
    kept = [a for a in allow if a in wanted or not _is_claudette_rule(a)]
    if len(kept) != len(allow):
        dst["permissions"]["allow"] = kept
    return len(allow) - len(kept)


def _hp_path(prefix, key):
    return f"{prefix}.{key}" if prefix else key


def _has_payload(dst, payload, prefix=""):
    """Is this settings payload present in the active config? Used once, at
    migration, to work out which features a machine runs before it has prefs
    at all. LENIENT on command strings: a value that references the same
    claudette script counts as present whatever the path or arguments, so
    migration never switches things off behind the user. Exact equality is
    still required for everything unfingerprinted."""
    for key, val in payload.items():
        if key not in dst:
            return False
        if isinstance(val, dict) and isinstance(dst[key], dict):
            if not _has_payload(dst[key], val, _hp_path(prefix, key)):
                return False
        elif dst[key] != val and _apply_op(_hp_path(prefix, key), dst[key], val)[0]:
            if dst[key] < val:
                return False            # below the bound: the feature is NOT satisfied
        elif dst[key] != val:
            fp = _fingerprint(val) if isinstance(val, str) else frozenset()
            if not (fp and isinstance(dst[key], str) and _fingerprint(dst[key]) == fp):
                return False
    return True


# --------------------------------------------------------------------------
# Per-feature undo records (CLAUDETTE_DESIGN.md §First-install UX, I1)
#
# Capture before override, per feature, at apply time. The record holds the
# prior value of every settings key the feature touches; it is consumed on
# restore and re-captured on re-enable. No record means claudette never wrote
# the key on this machine — the value there is the USER'S, and a disabled or
# unanswered feature must leave it alone (never-asked is inert, I3). The
# rolling .backup exists to debug the last apply and is never the undo store.
#
# logs/feature_undo.json — single writer: this script.
#   {"<fid>": {"ts": ..., "entries": [{"path": ["tui"],
#              "prior": {"value": x} | {"absent": true}, "wrote": y}]}}
# `wrote` is what claudette last put at the path; restore only fires where the
# current value still equals it — a value the user changed since is theirs and
# stays (I2: act, warn, offer undo; divergence is the thing to report).
# --------------------------------------------------------------------------
UNDO_PATH = pathlib.Path(paths.log("feature_undo.json"))


def _load_undo():
    return _read_json_or(UNDO_PATH, {})


def _save_undo(records):
    UNDO_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(UNDO_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(UNDO_PATH))


def _capture_records(dst, records):
    """Before the merge: every enabled settings-bearing feature without a
    record gets one, capturing the prior value of each key it is about to
    touch. A feature that already has a record only gains entries for leaves
    newly in its payload. Returns True if changed."""
    dirty = False
    for fid, fdef in prefs.features().items():
        if not prefs.effective(fid):
            continue
        payload = _expand(fdef).get("settings")
        if not payload:
            continue
        leaves = _payload_leaves(dst, payload)
        rec = records.get(fid)
        if rec is None:
            records[fid] = {"ts": int(time.time()),
                            "entries": [{"path": p, "prior": pr, "wrote": w}
                                        for p, pr, w in leaves]}
            dirty = True
        else:
            have = {tuple(e.get("path") or ()) for e in rec.get("entries", [])}
            for p, pr, w in leaves:
                if tuple(p) not in have:
                    rec.setdefault("entries", []).append(
                        {"path": p, "prior": pr, "wrote": w})
                    dirty = True
    return dirty


def _refresh_wrote(merged, records):
    """After the merge: where the active value now equals the payload's, that
    IS claudette's write — keep `wrote` current so a later restore recognizes
    it across parameter changes. A value the resolver left diverged keeps its
    old `wrote`, so restore correctly refuses it. Returns True if changed."""
    dirty = False
    for fid, rec in records.items():
        fdef = prefs.features().get(fid)
        if not fdef or not prefs.effective(fid):
            continue
        payload = _expand(fdef).get("settings") or {}
        for e in rec.get("entries", []):
            desired, cur, ok = payload, merged, True
            for k in e.get("path") or ():
                if not (isinstance(desired, dict) and k in desired
                        and isinstance(cur, dict) and k in cur):
                    ok = False
                    break
                desired, cur = desired[k], cur[k]
            if ok and cur == desired and e.get("wrote") != desired:
                e["wrote"] = copy.deepcopy(desired)
                dirty = True
    return dirty


def _restore_feature(dst, rec):
    """Consume one feature's record: at each path where the current value is
    still what claudette wrote (exact, or same-script fingerprint for a
    command-bearing dict from another layout), put the prior value back —
    or delete the key if the prior state was absent. Empty dicts left along
    the path go too. Returns (restored, kept_divergent)."""
    restored, kept = 0, 0
    for e in rec.get("entries", []):
        parts = e.get("path") or []
        if not parts:
            continue
        node = dst
        for k in parts[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if not isinstance(node, dict) or parts[-1] not in node:
            continue  # already gone — nothing claudette needs to do
        cur, wrote = node[parts[-1]], e.get("wrote")
        fp = _fingerprint(wrote) if isinstance(wrote, (str, dict)) else frozenset()
        same = cur == wrote or (bool(fp) and _fingerprint(cur) == fp)
        if not same:
            kept += 1
            continue
        prior = e.get("prior") or {}
        if prior.get("absent"):
            del node[parts[-1]]
        else:
            node[parts[-1]] = copy.deepcopy(prior.get("value"))
        restored += 1
        for depth in range(len(parts) - 1, 0, -1):
            parent = dst
            for k in parts[:depth - 1]:
                parent = parent.get(k) if isinstance(parent, dict) else None
            if (isinstance(parent, dict)
                    and isinstance(parent.get(parts[depth - 1]), dict)
                    and not parent[parts[depth - 1]]):
                del parent[parts[depth - 1]]
    return restored, kept


# --------------------------------------------------------------------------
# Settings merge (fingerprint-based; see CLAUDETTE_DEV.md "Settings merge convention")
# --------------------------------------------------------------------------
def _fingerprint(obj):
    """Frozenset identifying a claudette-managed entry, read from its command
    strings two ways:
      - a `claudette/[scripts/]<name>.py` path  -> member "<name>.py"
      - a `# claudette:<feature>` tag           -> member "tag:<feature>"
    Non-empty means the entry is claudette-managed. Tags exist so a bare shell
    one-liner — which has no script to reference — can still carry identity and
    therefore sync; the "tag:" prefix keeps them from colliding with script
    names. Identity lives in the command payload itself, so it survives Claude
    Code rewriting the active settings.json (which drops '//' comment keys)."""
    names = set()

    def walk(v):
        if isinstance(v, str):
            names.update(_SCRIPT_RE.findall(v))
            names.update("tag:" + t for t in _TAG_RE.findall(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return frozenset(names)


def _strip_comments(obj):
    """Drop legacy '//' comment keys (recursively) for comparison and output."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if k != "//"}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _print_rerender(path, local):
    """Default `rerender` callback of _deep_merge: one line, old value included
    so the run log keeps what was replaced."""
    old = local.get("command") if isinstance(local, dict) else local
    print(f"  Re-rendered claudette-owned '{path}' (was: {old})")


def _deep_merge(dst, src, resolve, path="", rerender=_print_rerender):
    """Merge assembled claudette config into active settings using content
    fingerprints.
    - Array dict items referencing claudette scripts: identity = set of scripts
      referenced (per array, i.e. per hook event). Same identity -> the source
      item, always (see below); no match -> appended. Extra same-identity
      duplicates in dst are removed (managed entries are singletons per event).
    - Array string items: appended if missing.
    - Dict values: claudette-managed (non-empty fingerprint) command-bearing
      dicts (statusLine) -> the source, always; plain dicts recurse.
    - Scalars: equal -> keep; differ -> resolve() decides (user value vs
      registry — ownership is undecidable for a plain `tui` or `env.LANG`).

    A fingerprint means "this entry is claudette's": its content is assembled
    output, never a user setting ("edit the registry, never the assembled
    output"), and everything tunable in it is a param. So a differing
    fingerprinted entry is a stale rendering, not a conflict — it is replaced
    and reported through `rerender(path, local)`, never put to resolve() (which
    would keep the stale command under --yes, and --take-source would also
    override real user values).

    resolve(path, local, source) returns the value to keep. Comparisons ignore
    legacy '//' keys.
    """
    for key, val in src.items():
        if key == "//":
            continue
        kpath = f"{path}.{key}" if path else key
        if key not in dst:
            dst[key] = _strip_comments(val)
        elif isinstance(val, list) and isinstance(dst[key], list):
            for item in val:
                fp = _fingerprint(item) if isinstance(item, dict) else None
                if fp:
                    canon = _strip_comments(item)
                    ipath = f"{kpath}[{'+'.join(sorted(fp))}]"
                    matches = [i for i, d in enumerate(dst[key])
                               if isinstance(d, dict) and _fingerprint(d) == fp]
                    if not matches:
                        dst[key].append(canon)
                    else:
                        local = _strip_comments(dst[key][matches[0]])
                        if local != canon:
                            rerender(ipath, local)
                        dst[key][matches[0]] = canon
                        for i in reversed(matches[1:]):
                            del dst[key][i]
                elif isinstance(item, str):
                    if item not in dst[key]:
                        dst[key].append(item)
                else:
                    print(f"  WARNING: unrecognized array item in source key '{kpath}' — skipping: {item}")
        elif isinstance(val, dict) and isinstance(dst[key], dict):
            # Wholesale compare only command-bearing managed objects (e.g.
            # statusLine); containers like `hooks` also fingerprint non-empty
            # but must recurse.
            if "command" in val and _fingerprint(val):
                canon = _strip_comments(val)
                local = _strip_comments(dst[key])
                if local != canon:
                    rerender(kpath, local)
                dst[key] = canon
            else:
                _deep_merge(dst[key], val, resolve, kpath, rerender)
        elif dst[key] != val:
            # A floor never reaches resolve(): "keep local" would silently
            # abandon the guarantee on exactly the machines that need it (the
            # scalar branch keeps the local value by default), and "take source"
            # would LOWER a machine set higher on purpose. max() is both answers
            # at once, and it is why a floor is never reported as drift.
            took, merged = _apply_op(kpath, dst[key], val)
            dst[key] = merged if took else resolve(kpath, dst[key], val)
    return dst


def _record_resolver(conflicts):
    """Keep local values; record (path, local, source) for reporting."""
    def resolve(path, local, source):
        conflicts.append((path, local, source))
        return local
    return resolve


def _prompt_resolver(yes, take_source):
    """Ask per conflict; --yes keeps local, --take-source takes the registry."""
    def resolve(path, local, source):
        if take_source:
            print(f"  Conflict at '{path}': taking source (--take-source)")
            return source
        if yes:
            print(f"  Conflict at '{path}': keeping local (--yes)")
            return local
        print(f"\n  Conflict at '{path}' (local differs from the claudette registry):")
        _print_diff(json.dumps(local, indent=2).splitlines(),
                    json.dumps(source, indent=2).splitlines(),
                    f"local/{path}", f"claudette/{path}")
        return source if _ask("  Keep (l)ocal or take (s)ource? ").lower().startswith("s") else local
    return resolve


def _ask(prompt, default=""):
    try:
        return input(prompt)
    except EOFError:
        print(f" (no interactive terminal — using '{default or 'skip'}')")
        return default


def _vtag(v):
    return " (venv)" if v.get("venv") else " (conda)" if v.get("conda") else ""


def _vver(v):
    return ".".join(map(str, v.get("version") or ()))


def ensure_python():
    """Resolve the interpreter the hooks and status line will run, once per
    machine (CLAUDETTE_DESIGN.md, "Absolute interpreter path").
    A stored path that still validates is kept untouched. Otherwise every
    candidate is probed from a non-login bash; one winner is stored silently,
    several distinct plain installs are put to the user (recommendation first;
    off a TTY the recommendation is taken and the alternatives named so the
    agent can raise them). Nothing valid: the `python` param is cleared, the
    hooks render the PATH-lookup chain, and the doctor warns until it is
    fixed. Returns the stored path or ""."""
    raw = prefs.param("python", "")
    stored = python_resolve.norm(raw)
    if stored:
        v = python_resolve.validate(stored)
        if v["ok"]:
            if stored != str(raw):
                prefs.write(params={"python": stored})  # normalized form
            return stored
        print(f"{RED}Stored Python for hooks is unusable: {stored} — {v['reason']}; "
              f"re-resolving.{RESET}")
    best, valid, rejected = python_resolve.resolve()
    if not best:
        print(f"{RED}No usable Python >= 3.9 found for the hooks and status line.{RESET}")
        for r in rejected:
            print(f"  rejected {r['path']}: {r['reason']}")
        print("  Hook commands will look Python up on PATH at run time; the status line "
              "stays empty until this is fixed. Install Python 3 (Windows: tick \"Add to "
              "PATH\"), or set " + paths.cmd("features_apply.py", "--param",
                                                "python=<full path>"))
        if stored:
            prefs.write(remove_params=["python"])
        return ""
    plain = [v for v in valid if not (v["venv"] or v["conda"])]
    choices = plain if len(plain) > 1 else valid
    pick = best["path"]
    if len(choices) > 1:
        print("Several Python installs could run claudette's hooks and status line:")
        for i, v in enumerate(choices, 1):
            print(f"  {i}. {v['path']}  {_vver(v)}{_vtag(v)}"
                  + ("  <- recommended" if i == 1 else ""))
        ans = _ask("Which one? (number, Enter = 1): ", "1").strip() or "1"
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            pick = choices[int(ans) - 1]["path"]
    prefs.write(params={"python": pick})
    v = next(x for x in valid if x["path"] == pick)
    print(f"Python for hooks and status line: {pick}  {_vver(v)}{_vtag(v)}")
    others = [x["path"] for x in valid if x["path"] != pick]
    if others:
        print("  also usable: " + ", ".join(others) + "  — change with "
              + paths.cmd("features_apply.py", "--param", "python=<path>"))
    if v["venv"] or v["conda"]:
        print(f"  {RED}an environment interpreter: the hooks die with it if it is "
              f"removed or moved.{RESET}")
    return pick


def _read_json_or(path, default=None):
    """Read a JSON file, returning `default` ({} if unset) on any error —
    missing file, truncated write, bad encoding. Used by --doctor, which must
    never crash on a half-written log."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _print_diff(old_lines, new_lines, fromfile, tofile):
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile, lineterm=""):
        if line.startswith("---"):   print(RED + line + RESET)
        elif line.startswith("+++"): print(GREEN + line + RESET)
        elif line.startswith("@@"):  print(CYAN + line + RESET)
        elif line.startswith("-"):   print(RED + line + RESET)
        elif line.startswith("+"):   print(GREEN + line + RESET)
        else:                        print(line)


# --------------------------------------------------------------------------
# CLAUDE.md hub + generated rules leaf
#
# Claudette owns NO span of CLAUDE.md: the feature lines live in LEAF
# (whole-file overwrite, untracked) and the hub carries one static import
# stanza, authored once. The begin/end markers below exist only to
# (a) migrate a file with an embedded block — it becomes the stanza — and
# (b) let --md-dirt classify such dirt during that migration.
# --------------------------------------------------------------------------
LEAF_HEADER = ("<!-- Generated by claudette (features_apply.py) from features_def.json filtered\n"
               "     by prefs.local.json — per-machine, untracked, overwritten on every apply.\n"
               "     Never edit; change behavior with /claudette prefs. Imported by\n"
               "     ~/.claude/CLAUDE.md; kept when empty — a missing import target would sit\n"
               "     in context as literal text. HTML comments cost no tokens. -->\n")
HUB_STANZA = ("<!-- Rules claudette assembled from this machine's answered features — the\n"
              "     imported file is generated: manage it with /claudette prefs, never edit. -->\n"
              "@" + LEAF_NAME + "\n")
_HUB_IMPORT_RE = re.compile(r"^[ \t]*@" + re.escape(LEAF_NAME) + r"[ \t]*$", re.M)
MARK_BEGIN = ("<!-- claudette:begin — assembled from features_def.json by features_apply.py; "
              "edits inside this block are overwritten; these HTML comments are automatically "
              "ignored by Claude and do not consume tokens. -->")
MARK_END = "<!-- claudette:end -->"
_MARK_BEGIN_RE = re.compile(r"^[ \t]*<!--\s*claudette:begin\b.*?-->[ \t]*$", re.M)
_MARK_END_RE = re.compile(r"^[ \t]*<!--\s*claudette:end\b.*?-->[ \t]*$", re.M)


def _find_block(text):
    """(start, end) span of the managed block INCLUDING marker lines, or None.
    Raises ValueError on malformed markers (never guess ownership)."""
    begins = list(_MARK_BEGIN_RE.finditer(text))
    ends = list(_MARK_END_RE.finditer(text))
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1:
        raise ValueError(f"expected exactly one claudette:begin/claudette:end marker pair "
                         f"(found {len(begins)} begin, {len(ends)} end)")
    if ends[0].start() < begins[0].end():
        raise ValueError("claudette:end marker appears before claudette:begin")
    return begins[0].start(), ends[0].end()


def _md_dirt_scope():
    """Where the working CLAUDE.md differs from HEAD: clean|managed|outside|unknown.

    `managed` means the ONLY difference is claudette-owned — the embedded
    block, or the import stanza that replaces it (so a locally-migrated file
    still pulls over a HEAD that has the block). Such dirt is derived: the apply right
    after the pull regenerates it, so pull.sh restores the file from HEAD first,
    because git refuses to merge over it. `outside` is real user content and is
    left alone: the pull must fail loudly rather than discard it.

    Line endings are normalized away — every machine writes CLAUDE.md with its own
    newline convention, and a CRLF/LF-only difference is not dirt anyone means.
    """
    stanza = {l.strip() for l in HUB_STANZA.splitlines() if l.strip()}

    def _norm(text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            span = _find_block(text)
        except ValueError:
            return None, text          # malformed markers: no trustworthy split
        block = ""
        if span:
            block = text[span[0]:span[1]]
            text = text[:span[0]] + text[span[1]:]
        # Non-empty lines only: removing a span vs removing lines leaves
        # different blank-line runs around the seam, and blank lines are not
        # content anyone authored.
        rest = [l for l in text.split("\n") if l.strip() and l.strip() not in stanza]
        return block, rest

    repo = CLAUDE_MD.parent
    try:
        head = subprocess.run(["git", "-C", str(repo), "show", "HEAD:CLAUDE.md"],
                              capture_output=True, timeout=15)
        if head.returncode != 0:
            return "unknown"           # not a repo, no HEAD, or file untracked
        head_text = head.stdout.decode("utf-8", errors="replace")
        work_text = CLAUDE_MD.read_text(encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    h_block, h_rest = _norm(head_text)
    w_block, w_rest = _norm(work_text)
    if h_block is None or w_block is None:
        return "unknown"
    if h_rest != w_rest:
        return "outside"
    return "clean" if h_block == w_block else "managed"


_IMPORT_RE = re.compile(r"(?:(?<=^)|(?<=\s))@([^\s`'\"]+\.(?:md|markdown|txt))\b")


def _md_missing_imports(path):
    """[(lineno, raw, resolved)] for @-imports in CLAUDE.md whose target is absent.

    A missing import does not vanish: Claude Code leaves the literal `@path` in
    place, so it loads into every session as prose and the line silently stops
    meaning what it says. Nothing errors anywhere — which is why --doctor is
    where this belongs.

    Deliberately conservative, because a false nag is worse than a miss:
    - only `.md`/`.markdown`/`.txt` targets. A wider net flags npm scopes
      (`@scope/pkg`) and decorators, which Claude Code would fail to import too
      but which nobody wants to be told about every update;
    - `@` must start a line or follow whitespace, so emails never match;
    - code fences and inline spans are blanked out first (line numbers kept);
    - the claudette-managed block is skipped: it is generated, and never imports;
    - the top-level file ONLY. Claude Code follows imports ~5 levels deep; this
      does not, so the row must not be read as full coverage.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        span = _find_block(text)
    except ValueError:
        span = None  # malformed markers are reported elsewhere; scan it all
    if span:
        text = text[:span[0]] + "\n" * text[span[0]:span[1]].count("\n") + text[span[1]:]
    text = re.sub(r"```.*?```", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in _IMPORT_RE.finditer(line):
            raw = m.group(1)
            p = pathlib.Path(os.path.expanduser(raw))
            if not p.is_absolute():
                p = path.parent / p
            if not p.exists():
                out.append((n, raw, p))
    return out


def _user_line_count(text):
    stanza = {l.strip() for l in HUB_STANZA.splitlines() if l.strip()}
    return len([l for l in text.splitlines() if l.strip() and l.strip() not in stanza])


def _merge_hub(dst_text, want):
    """Ensure CLAUDE.md imports the generated leaf. Returns (merged, n_user|None).

    A claudette:begin/end block is replaced by the import stanza in place —
    the one-time migration, done even when `want` is false so the dead block
    never lingers. Otherwise: stanza already present → no change ever
    (the steady state — CLAUDE.md moves only by user hand);
    `want` false (no md-bearing feature) → a stanza-less file is not touched;
    empty/absent file → minimal template; anything else → stanza appended,
    everything above kept. Malformed markers raise — never guess ownership."""
    d = _find_block(dst_text)  # ValueError propagates
    if d:
        merged = dst_text[:d[0]] + HUB_STANZA.rstrip("\n") + dst_text[d[1]:]
        return merged, _user_line_count(merged)
    if _HUB_IMPORT_RE.search(dst_text) or not want:
        return dst_text, (_user_line_count(dst_text) if dst_text else None)
    if not dst_text.strip():
        return "# CLAUDETTE\n" + HUB_STANZA, None
    merged = dst_text.rstrip("\n") + "\n\n# CLAUDETTE\n" + HUB_STANZA
    return merged, _user_line_count(dst_text)


def _leaf_text(md_body):
    return LEAF_HEADER + ("\n" + md_body if md_body else "")


def _write_leaf(md_body):
    """Write the generated leaf; True if it changed. Wholly claudette-owned, so
    no prompt and no backup — like hooks.json, it is regenerated, never merged.
    Written EMPTY (header only) rather than deleted: a dangling @import stays
    in context as literal text."""
    text = _leaf_text(md_body)
    old = LEAF.read_text(encoding="utf-8", errors="replace") if LEAF.exists() else None
    if old == text:
        return False
    LEAF.write_text(text, encoding="utf-8")
    return True


def _report_user_rules(n_user):
    if n_user is not None:
        print(f"  User content in CLAUDE.md: {n_user} non-empty line(s) — untouched by sync.")


def _backup(path):
    if path.exists():
        backup = path.with_suffix(path.suffix + ".backup")
        shutil.copy2(path, backup)
        print(f"  Backup: {backup.name}")


# --------------------------------------------------------------------------
# Feature review (the numbered lists behind /claudette update and /claudette prefs)
# --------------------------------------------------------------------------
def _wrap(text, width, indent):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return ("\n" + " " * indent).join(lines)


def _parse_selection(text, n):
    """'2,5-7 9' -> {2,5,6,7,9}, silently ignoring anything out of range."""
    out = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            out.update(i for i in range(min(lo, hi), max(lo, hi) + 1) if 1 <= i <= n)
        elif part.isdigit() and 1 <= int(part) <= n:
            out.add(int(part))
    return out


def review(ids, state, title, note, touched=None):
    """Numbered list of `ids` with their proposed state; returns the state the
    user settled on, or None if they backed out.

    `state` maps id -> proposed bool. For an update that is the registry's
    recommendation for features nobody has answered for yet; for `--prefs` it is
    what the machine currently has. Same renderer either way, because the only
    real difference is which ids are in the list.

    `touched`, if given, is a set this function fills with the ids the user
    explicitly flipped — how finalize_state() knows a `default: null` feature
    was actually chosen rather than carried along.
    """
    state = dict(state)
    touched = touched if touched is not None else set()
    feats = prefs.features()
    grp_names = prefs.groups()
    while True:
        print(f"\n{BOLD}--- {title} ---{RESET}")
        print(note + "\n")
        # Grouped for display, in the registry's `groups` order — deliberately
        # independent of feature order, which belongs to the CLAUDE.md block.
        by_group = {}
        for fid in ids:
            by_group.setdefault((feats.get(fid) or {}).get("group") or "claudette", []).append(fid)
        seq = [(g, by_group[g]) for g in grp_names if g in by_group]
        seq += [(g, v) for g, v in by_group.items() if g not in grp_names]
        order = []
        for grp, members in seq:
            print(f"{GRAY}{grp_names.get(grp, grp)}{RESET}")
            for fid in members:
                fdef = feats.get(fid) or {}
                order.append(fid)
                i = len(order)
                on = state.get(fid, False)
                mark = f"{GREEN}on {RESET}" if on else f"{GRAY}off{RESET}"
                rec_val = fdef.get("default")
                if rec_val is None:
                    rec = "" if prefs.answered(fid) or fid in touched else \
                        f"  {RED}(no recommendation — choose explicitly or it stays unanswered){RESET}"
                else:
                    rec = "" if bool(rec_val) == on else \
                        f"  {CYAN}(recommended: {'on' if rec_val else 'off'}){RESET}"
                label = f" {GRAY}— {fdef['name']}{RESET}" if fdef.get("name") else ""
                params = "".join(
                    f"\n      {GRAY}{k} = {json.dumps(prefs.param(k))}{RESET}"
                    for k in (fdef.get("params") or {}))
                print(f"  {i:>2}. [{mark}] {BOLD}{fid}{RESET}{label}{rec}")
                # The four decision fields (contract §16.1): what it does, what
                # you gain, what it costs in side effects, and the next best
                # option without it. `description` is the pre-v3 fallback.
                for fkey, flabel in (("what", "What"), ("benefit", "Benefit"),
                                     ("cost", "Cost"), ("alternative", "Alternative")):
                    text = fdef.get(fkey) or (fdef.get("description") if fkey == "what" else "")
                    if text:
                        print(f"      {GRAY}{flabel}: {_wrap(text, 82, 6 + len(flabel) + 2)}{RESET}")
                if fdef.get("recommend_note"):
                    print(f"      {GRAY}Recommendation: "
                          f"{_wrap(fdef['recommend_note'], 82, 6 + 16)}{RESET}")
                if params:
                    print(params.lstrip("\n"))
        print("\nEnter numbers to flip (e.g. 3,7-9), 'r' to reset to recommended,")
        # EOF defaults to CANCEL, never accept (I5): with no terminal, 'Enter
        # to accept' would silently consent to the whole proposal. Note
        # isatty() cannot be the guard here — Windows reports the NUL device
        # as a TTY — so the consent prompt itself must refuse to default.
        answer = _ask("'q' to cancel, or Enter to accept: ", "q").strip().lower()
        if answer in ("q", "quit", "cancel"):
            return None
        if not answer:
            break
        if answer in ("r", "reset"):
            state = {fid: bool((feats.get(fid) or {}).get("default")) for fid in ids}
            touched.clear()  # a reset is not an explicit choice for null-rec features
            continue
        for i in _parse_selection(answer, len(order)):
            fid = order[i - 1]
            state[fid] = not state.get(fid, False)
            touched.add(fid)

    # Dependencies are checked against the WHOLE proposed set, not one at a
    # time, so turning two things on together is not reported as a conflict.
    for fid, on in list(state.items()):
        if not on:
            continue
        missing = prefs.missing_requirements(fid, state)
        if missing:
            print(f"\n{RED}{fid} needs {', '.join(missing)}, which would be off.{RESET}")
            if _ask(f"  Turn {'it' if len(missing) == 1 else 'them'} on too? (y/n): ", "y") \
                    .strip().lower().startswith("y"):
                for req in missing:
                    state[req] = True
            else:
                state[fid] = False
                print(f"  Leaving {fid} off.")
    return state


def finalize_state(state, touched):
    """Split a reviewed state into what may be written and what must stay
    unanswered: a `default: null` feature that was neither already answered nor
    explicitly flipped in this review has NOT been chosen — writing it would
    turn "carried along in the list" into consent."""
    writable, skipped = {}, []
    for fid, on in state.items():
        if prefs.recommendation(fid) is None and not prefs.answered(fid) and fid not in touched:
            skipped.append(fid)
        else:
            writable[fid] = on
    return writable, skipped


def save_state(state):
    """Persist a proposed feature state, and drop answers for features the
    registry no longer has."""
    gone = prefs.orphans()
    if gone:
        print(f"  Dropping {len(gone)} pref(s) for features that no longer exist: "
              + ", ".join(gone))
    prefs.write(features={fid: bool(on) for fid, on in state.items()},
                remove_features=gone)


# --------------------------------------------------------------------------
# One-time migration from an older layout
# --------------------------------------------------------------------------
_LOG_RENAMES = [
    ("last_status.json", "claude_usage_last.json"),
    ("status", "claude_usage_snapshots"),
    ("last_limits.json", "claude_limits_last.json"),
    ("last_limits_refresh.json", "claude_limits_request.json"),
]

# Registry v3 param renames — features carry their own via `renamed_from`,
# parameters need this one-time map.
_PARAM_RENAMES = [
    ("audio_turn_end_notes", "audible_turn_end_notes"),
    ("audio_turn_end_ms", "audible_turn_end_total_ms"),
    ("audio_turn_end_volume", "audible_turn_end_volume"),
    ("audio_permission_notes", "audible_turn_permission_notes"),
    ("audio_permission_ms", "audible_turn_permission_total_ms"),
    ("audio_permission_volume", "audible_turn_permission_volume"),
    ("audio_failure_notes", "audible_failure_notes"),
    ("audio_failure_ms", "audible_failure_total_ms"),
    ("audio_failure_volume", "audible_failure_volume"),
    ("audio_speak_voice", "audible_speak_voice"),
    ("audio_speak_rate", "audible_speak_rate"),
    ("audio_speak_volume", "audible_speak_volume"),
]
# Retired features/params whose prefs would otherwise linger as orphans.
# A feature REPLACED by a different intervention has no renamed_from, so re-consent
# is intended: claude_md_no_coauthor -> claude_no_coauthor -> claude_no_attribution
# (settings.json, not CLAUDE.md; the last also suppresses the session URL, a second
# trailer the older answers never covered), and pretool_push_guard ->
# claude_md_push_leak_scan (a CLAUDE.md rule, not a PreToolUse hook;
# CLAUDETTE_DESIGN.md "The gate went back to an instruction").
# claude_auto_updates: its whole payload was autoUpdatesChannel, which Claude Code
# already resolves to "latest" when unset. No _RETIRED_FEATURE_SETTINGS entry:
# reconcile() reverts the key from each machine's own undo record.
_RETIRED_FEATURES = ["audio_session_end", "claude_md_no_coauthor",
                     "claude_no_coauthor", "claude_auto_updates",
                     "pretool_push_guard"]
_RETIRED_PARAMS = ["audio_session_end_notes", "audio_session_end_ms",
                   "audio_session_end_volume", "claude_auto_updates_channel"]

# Settings a retired feature wrote that no live registry entry owns any more.
# Cleaned via a synthesized undo record at the migration that drops the retired
# ANSWER — only a machine that had the feature on ever gets one, so a fresh
# machine where the user set the same value by hand is never touched (I1/I3).
# A feature retired after undo records existed needs no entry here: its real
# record survives it and reconcile() restores from that.
_RETIRED_FEATURE_SETTINGS = {
    # claude_no_coauthor's payload; Claude Code deprecated the key in favour of
    # `attribution`, which claude_no_attribution writes.
    "claude_no_coauthor": {"includeCoAuthoredBy": False},
}


def migrate():
    """Idempotent moves from an older claudette layout. Safe on every apply:
    each step is a no-op once done and none of them overwrite a newer file."""
    done = []
    old_root = CLAUDE_DIR / "claudette"
    old_logs = old_root / "logs"
    new_logs = pathlib.Path(paths.LOGS)

    # 1. logs/ follows the code. Throttle registry, calibration and usage
    #    snapshots are real history — moved, not regenerated.
    if old_logs.is_dir() and old_logs.resolve() != new_logs.resolve():
        new_logs.mkdir(parents=True, exist_ok=True)
        for item in old_logs.iterdir():
            target = new_logs / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
            done.append(f"logs/{item.name} -> {paths.tilde(str(target), '~')}")

    # 2. Log names (<family>_<member>). A destination that already exists means
    #    both names were in use — merge directories, keep the newer file.
    for old, new in _LOG_RENAMES:
        src, dst = new_logs / old, new_logs / new
        if not src.exists():
            continue
        if not dst.exists():
            shutil.move(str(src), str(dst))
            done.append(f"logs/{old} -> logs/{new}")
        elif src.is_dir() and dst.is_dir():
            for item in src.iterdir():
                if not (dst / item.name).exists():
                    shutil.move(str(item), str(dst / item.name))
            if not any(src.iterdir()):
                src.rmdir()
            done.append(f"logs/{old}/ merged into logs/{new}/")

    # 3. prefs.local.json is untracked, so the migration pull leaves it behind
    #    in the old folder where nothing would ever look for it.
    old_prefs = old_root / os.path.basename(prefs.LOCAL_PATH)
    if old_prefs.exists() and not pathlib.Path(prefs.LOCAL_PATH).exists():
        shutil.move(str(old_prefs), prefs.LOCAL_PATH)
        done.append(f"{old_prefs.name} -> {paths.tilde(prefs.LOCAL_PATH, '~')}")

    # 3b. Flat prefs -> sections: `cft_<id>` keys become bare ids
    #     under "features", everything else moves under "params". prefs.write()
    #     folds legacy flat keys on every pass, so an empty patch is the rewrite.
    if any(k not in ("features", "params") for k in prefs.local()):
        prefs.write()
        done.append("prefs.local.json: flat keys -> features/params sections")

    # 4. hook_settings.json values are per-machine, so they become prefs. That
    #    file is tracked, so the migration pull deletes it before this can run;
    #    the README says to copy it aside first, and that copy is read first.
    for cand in (CLAUDE_DIR / "hook_settings.premigration.json",
                 old_root / "hook_settings.json",
                 pathlib.Path(paths.ROOT) / "hook_settings.json"):
        if cand.exists():
            try:
                vals = json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                vals = {}
            carry = {k: v for k, v in vals.items()
                     if k in ("owner", "audio_device") and v and k not in prefs.param_overrides()}
            if carry:
                prefs.write(params=carry)
                done.append(f"{cand.name} -> {os.path.basename(prefs.LOCAL_PATH)}")
            break

    # 4b. Renames: a feature's stored answer follows it across an id change
    #     (`renamed_from`, may fan out — the speak split carries one old answer
    #     to three new features), and this pass's parameter renames likewise.
    feats, pars = prefs.feature_answers(), prefs.param_overrides()
    fcarry, fdrop, pcarry, pdrop = {}, set(), {}, set()
    # Fan-in too: when several old features merge into one (auto_done from
    # auto_done_scheduler and compact_done), an old answer of yes wins - the
    # user wanted what the merged feature does in one of its forms.
    for fid in prefs.features():
        olds = [old for old in prefs.renamed_from(fid) if old in feats]
        if not olds:
            continue
        if fid not in feats:
            answers = [feats[old] for old in olds]
            fcarry[fid] = (True if True in answers
                           else False if False in answers else answers[-1])
        fdrop.update(olds)
    for old, new in _PARAM_RENAMES:
        if old in pars and new not in pars:
            pcarry[new] = pars[old]
            pdrop.add(old)
    fdrop.update(k for k in _RETIRED_FEATURES if k in feats)
    pdrop.update(k for k in _RETIRED_PARAMS if k in pars)
    # A retired feature that was ON leaves a synthesized undo record so
    # reconcile() removes its orphaned settings — evidence it was claudette's
    # write; a machine that never ran it (or answered no) gets nothing.
    retired_on = [k for k in _RETIRED_FEATURE_SETTINGS if feats.get(k) is True]
    if fcarry or fdrop or pcarry or pdrop:
        prefs.write(features=fcarry, params=pcarry,
                    remove_features=fdrop, remove_params=pdrop)
        done.append(f"renames: {len(fcarry) + len(pcarry)} answer(s)/param(s) carried, "
                    f"{len(fdrop) + len(pdrop)} old key(s) dropped")
    undo = _load_undo()
    undo_made = []
    for fid in retired_on:
        if fid not in undo:
            undo[fid] = {"ts": int(time.time()), "migrated": True,
                         "entries": [{"path": p, "prior": pr, "wrote": w}
                                     for p, pr, w in
                                     _payload_leaves({}, _expand(_RETIRED_FEATURE_SETTINGS[fid]))]}
            undo_made.append(fid)

    # 4c. Features switched on before undo records existed have no capture;
    #     their prior is treated as ABSENT — disabling removes rather than
    #     restores, since the alternative leaves an uninstall permanently
    #     polluted. A record can be seeded by hand where the true prior value
    #     is known.
    active = _read_json_or(SETTINGS) if SETTINGS.exists() else {}
    for fid, fdef in prefs.features().items():
        if not prefs.enabled(fid) or fid in undo:
            continue
        payload = _expand(fdef).get("settings")
        # Only a payload demonstrably IN EFFECT predates records; a feature
        # answered but not yet applied gets a real capture at apply time.
        if not payload or not _has_payload(active, payload):
            continue
        undo[fid] = {"ts": int(time.time()), "migrated": True,
                     "entries": [{"path": p, "prior": pr, "wrote": w}
                                 for p, pr, w in _payload_leaves({}, payload)]}
        undo_made.append(fid)
    if undo_made:
        _save_undo(undo)
        done.append("undo records synthesized (prior treated as absent) for: "
                    + ", ".join(undo_made))

    # 5. An existing installation was, by definition, running everything. Answer
    #    the whole registry at its recommendation ONCE so an upgrade does not
    #    silently switch the machine off — the two security-posture features
    #    recommend off, so an upgrade never loosens anything on its own.
    if not prefs.feature_answers():
        active = _read_json_or(SETTINGS) if SETTINGS.exists() else {}
        if _fingerprint(active):
            state = {}
            allow = ((active.get("permissions") or {}).get("allow")) or []
            for fid, fdef in prefs.features().items():
                # Preserve what the machine demonstrably already had: a settings
                # payload (or permission rule) that is present and unchanged
                # means the user was running that feature, whatever the registry
                # recommends. Evidence-based answers are the only ones a
                # `default: null` feature can receive here.
                f = _expand(fdef)
                if f.get("settings"):
                    state[fid] = _has_payload(active, f["settings"])
                elif f.get("permissions"):
                    state[fid] = any(r in allow for r in f["permissions"])
                else:
                    state[fid] = prefs.recommendation(fid) is True
            save_state(state)
            done.append("existing install detected -> features answered from what this machine already ran")

    if done:
        print(f"{BOLD}--- Migration ---{RESET}")
        for line in done:
            print("  " + line)
    return done


# --------------------------------------------------------------------------
# Dependencies — zero REQUIRED third-party modules (contract §10).
# `sounddevice` is the one OPTIONAL module: it only buys audio_device routing,
# and only matters when that parameter is set. Warned once, HERE — the hooks
# degrade to the default output device silently.
# --------------------------------------------------------------------------
pip_cmd = f"{pathlib.Path(sys.executable).name} -m pip install sounddevice"


def optional_dep_warnings():
    out = []
    if prefs.param("audio_device", "") and importlib.util.find_spec("sounddevice") is None:
        out.append("audio_device is set but 'sounddevice' is not installed — tones and speech "
                   "play on the DEFAULT output device. Fix: " + pip_cmd)
    return out


# --------------------------------------------------------------------------
# Apply-time tone synthesis (contract §9): generated/<feature>.wav per enabled
# tone feature, hash-gated on (notes, total_ms, volume, synth version) via
# generated/tones.json — so apply stays cheap, a parameter change regenerates
# exactly one file, and NOTHING synthesises at hook time. The tunables live in
# the artifact, not the hook command line, so changing a tone does not rewrite
# hooks.json and needs no restart.
# --------------------------------------------------------------------------
GENERATED = pathlib.Path(paths.ROOT) / "generated"
TONES_MANIFEST = GENERATED / "tones.json"


def tone_features():
    """Feature ids whose params follow the <fid>_notes convention — the tone
    features, without a hardcoded list."""
    return [fid for fid, fdef in prefs.features().items()
            if (fid + "_notes") in (fdef.get("params") or {})]


def generate_tones(quiet=False):
    import hashlib
    import tone_synth
    manifest = _read_json_or(TONES_MANIFEST, {})
    made, removed = [], []
    for fid in tone_features():
        wav = GENERATED / (fid + ".wav")
        if not prefs.effective(fid):
            continue
        # audio_lead_ms is global, not per feature — the delay it compensates
        # for belongs to the headphones, not to the cue. It still has to be IN
        # the hash, or changing it leaves every existing wav matching and the
        # new value never reaches a single file.
        inputs = {"notes": str(prefs.param(fid + "_notes")),
                  "ms": int(prefs.param(fid + "_total_ms", 200)),
                  "vol": int(prefs.param(fid + "_volume", 50)),
                  "lead": int(prefs.param("audio_lead_ms", 500)),
                  "synth": tone_synth.SYNTH_VERSION}
        digest = hashlib.sha1(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:12]
        if manifest.get(fid) == digest and wav.exists():
            continue
        GENERATED.mkdir(parents=True, exist_ok=True)
        tone_synth.synth_wav(str(wav), inputs["notes"], inputs["ms"], inputs["vol"],
                             inputs["lead"])
        manifest[fid] = digest
        made.append(fid)
    # Un-apply: a disabled, suspended or retired tone feature loses its artifact too.
    for fid in [f for f in manifest if f not in prefs.features() or not prefs.effective(f)]:
        (GENERATED / (fid + ".wav")).unlink(missing_ok=True)
        del manifest[fid]
        removed.append(fid)
    if made or removed:
        GENERATED.mkdir(parents=True, exist_ok=True)
        TONES_MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
        if not quiet:
            print(f"\n{BOLD}--- generated/ tones ---{RESET}")
            for fid in made:
                print(f"  synthesised {fid}.wav")
            for fid in removed:
                print(f"  removed {fid}.wav (feature off)")


# --------------------------------------------------------------------------
# Keychain (macOS scoped usage limits)
# --------------------------------------------------------------------------
def keychain_authorize(force=False):
    """macOS Keychain authorization for scoped usage limits (Fable % etc).

    claude_limits.py needs Claude Code's OAuth token. On macOS it lives in the
    Keychain, and reading it can raise a system prompt — unacceptable from a
    detached background refresh. So background access is gated on
    logs/keychain_ok.json and the prompt is only ever triggered HERE, with the
    intent stated first. Re-probes (marker present) run silently with a short
    timeout to detect ACL resets (Claude Code rewrites the keychain item on
    token refresh, which occasionally drops 'Always Allow').

    Returns True if scoped limits are enabled afterwards, else False.
    """
    if platform.system() != "Darwin":
        return True  # non-macOS reads .credentials.json directly
    kc_ok = pathlib.Path(paths.log("keychain_ok.json"))
    if (CLAUDE_DIR / ".credentials.json").exists():
        print("\nKeychain: not needed (.credentials.json present) — scoped usage limits enabled.")
        return True
    first = not kc_ok.exists()
    if first and not force:
        print("\n--- Keychain authorization (optional) ---")
        print("To show model-scoped usage limits (e.g. Fable weekly %) in the status")
        print("line, Claudette reads Claude Code's OAuth token from the macOS Keychain.")
        print("macOS may show a security prompt: click 'Always Allow' so future")
        print("background refreshes stay silent. Declining just disables the feature.")
        # Default 'y' with no TTY: the macOS dialog is the real consent gate, and
        # declining it there is equivalent to answering 'n' here.
        if not _ask("Probe Keychain now? (y/n): ", "y").strip().lower().startswith("y"):
            print("Skipped — run this anytime to authorize:")
            print("  " + paths.cmd("features_apply.py", "--auth"))
            return False
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=120 if first else 5)
        ok = r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        ok = False
    if ok:
        kc_ok.parent.mkdir(parents=True, exist_ok=True)
        kc_ok.write_text(json.dumps({"ts": int(time.time()), "by": "features_apply.py probe"}))
        print("Keychain access OK — scoped usage limits enabled (logs/keychain_ok.json).")
        return True
    if not first:
        kc_ok.unlink(missing_ok=True)
        print("WARNING: Keychain access lost (item ACL may have reset).")
        print("Scoped usage limits disabled. Re-authorize with:")
    else:
        print("Keychain access not granted — scoped usage limits stay disabled.")
        print("Retry anytime with:")
    print("  " + paths.cmd("features_apply.py", "--auth"))
    return False


def install_stages():
    """{fid: stage} splitting the install by restart requirement (I7):
    'core' = hook-bearing features (but not `hooks_restart: false`, whose hooks
    only reach processes started later) plus their requires closure — the set one
    restart covers, incoherent if half-applied; 'extra' = live-pickup goodies
    a later update can add without cost; 'individual' = default:null security
    posture, presented one at a time, never inside a pack — a pack answer
    must never double as consent to loosen the machine."""
    feats = prefs.features()
    core = {fid for fid, fdef in feats.items()
            if fdef.get("hooks") and fdef.get("hooks_restart", True)}
    grew = True
    while grew:
        grew = False
        for fid in list(core):
            for req in (feats.get(fid) or {}).get("requires") or []:
                if req not in core:
                    core.add(req)
                    grew = True
    return {fid: ("individual" if fdef.get("default") is None
                  else "core" if fid in core else "extra")
            for fid, fdef in feats.items()}


def _active_settings_allow():
    """The active settings.json plus its permission allow-list — the pair every
    alignment test reads."""
    active = _read_json_or(SETTINGS) if SETTINGS.exists() else {}
    allow = ((active.get("permissions") or {}).get("allow")) or []
    return active, allow


def _is_aligned(fid, fdef, active, allow):
    """I3 as a predicate: unanswered, but the desired state already holds — the
    next apply answers it on silently, so no flow may offer it as a decision.
    Shared by --features-json and --update-plan."""
    if prefs.answered(fid):
        return False
    f = _expand(fdef)
    if f.get("settings"):
        return _has_payload(active, f["settings"])
    if f.get("permissions"):
        return all(r in allow for r in f["permissions"])
    return False


# --------------------------------------------------------------------------
# Modes that exit early
# --------------------------------------------------------------------------
# A `recommend_by` recommendation follows what was observed on this machine
# (prefs.features); bring the observation up to date before anything lists
# features. TTL'd, so cheap when fresh; a failure leaves the registry defaults.
try:
    import idle_compaction
    idle_compaction.refresh()
except Exception:
    pass
validate_registry()

if args.auth:
    sys.exit(0 if keychain_authorize(args.yes) else 1)

def honor_preview_params():
    """`--param x=v --preview-*` is the adjust-and-listen loop; the main
    --param processing lives past the preview early exits, so honor it here."""
    for item in args.param:
        k, _, v = item.partition("=")
        k, v = k.strip(), v.strip()
        if k in prefs.param_defaults() and v and v.lower() != "null":
            try:
                val = json.loads(v)
            except ValueError:
                val = v
            prefs.write(params={k: val})
            print(f"  {k} = {v}")

if args.preview_tone:
    # A tone is auditionable before any hook is installed. Synthesizes to a
    # temp file with the CURRENT params (adjust with --param <fid>_volume=NN
    # and preview again), plays it, touches no config.
    import tempfile
    import tone_synth
    fid = args.preview_tone
    if fid not in tone_features():
        print(f"{RED}{fid} is not a tone feature.{RESET} Tone features: "
              + ", ".join(tone_features()))
        sys.exit(2)
    honor_preview_params()
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    tone_synth.synth_wav(tmp.name, str(prefs.param(fid + "_notes")),
                         int(prefs.param(fid + "_total_ms", 200)),
                         int(prefs.param(fid + "_volume", 50)),
                         int(prefs.param("audio_lead_ms", 500)))
    import hook_play_wav
    ok = hook_play_wav.play_wav(tmp.name)
    os.unlink(tmp.name)
    # The lead is printed because it is otherwise indistinguishable from a
    # failure: half a second of nothing, then the cue.
    print(f"Played {fid} (notes={prefs.param(fid + '_notes')}, "
          f"volume={prefs.param(fid + '_volume', 50)}, "
          f"lead={prefs.param('audio_lead_ms', 500)}ms)." if ok
          else f"{RED}Playback failed — check the OS audio device.{RESET}")
    print("Adjust: " + paths.cmd("features_apply.py", "--param",
                                 f"{fid}_volume=<0-100>", "--preview-tone", fid))
    sys.exit(0 if ok else 1)

if args.preview_speak:
    # The audible_speak_* counterpart of --preview-tone, and the way to A/B
    # audio_lead_ms by ear for speech. Calls the same
    # speak() the hook runs — one code path — but errors print here instead of
    # dropping the degraded marker for --doctor. The hook's gates (class
    # routing, has_real_prompt) belong to the pipeline, not the voice, and are
    # deliberately not exercised.
    if "audio" in prefs.suspended_zones():
        # A preview that silently no-ops is failure-shaped success — refuse loudly.
        print(f"{RED}Audio zone is suspended — nothing would be spoken.{RESET} "
              "Resume: " + paths.cmd("features_apply.py", "--resume", "audio"))
        sys.exit(2)
    honor_preview_params()
    import hook_speak_notification as speak_mod
    voice = int(prefs.param("audible_speak_voice", 1))
    rate = int(prefs.param("audible_speak_rate", 180))
    volume = int(prefs.param("audible_speak_volume", 50))
    lead = int(prefs.param("audio_lead_ms", 500))
    err = speak_mod.speak(args.preview_speak, voice, rate, volume)
    # Print only the params this platform actually uses: off Windows, voice and
    # volume are SAPI concepts — listing them would imply tuning them changes
    # anything. The lead is printed for the same reason as --preview-tone's.
    system = platform.system()
    if system == "Darwin":
        effective = f"rate={rate} wpm, lead={lead}ms; voice/volume are Windows-only"
    elif system == "Windows":
        effective = f"voice={voice}, rate={rate} wpm, volume={volume}, lead={lead}ms"
    else:
        effective = f"rate={rate} wpm, volume={volume}; no lead primitive on Linux"
    if err:
        print(f"{RED}Speech failed: {err}{RESET}")
    else:
        print(f'Spoke "{args.preview_speak}" ({effective}).')
    # Speech params ride the hook command line ({P:audible_speak_*} argv), so a
    # prefs change is inaudible in real notifications until hooks re-apply —
    # unlike tones, whose wav regenerates. Say so when the preview and the
    # installed hook disagree, or the preview lies about what 3pm sounds like.
    m = None
    for rows in (_read_json_or(HOOKS_JSON).get("hooks") or {}).values():
        for row in rows:
            for h in row.get("hooks", []):
                m = m or re.search(r'hook_speak_notification\.py" (\d+) (\d+) (\d+)',
                                   h.get("command", ""))
    if m is None:
        print("No speak hook installed yet (audible_speak_* off, or not applied) — "
              "previewed straight from prefs.")
    elif tuple(int(x) for x in m.groups()) != (voice, rate, volume):
        print(f"{RED}Installed hook still says voice={m.group(1)}, rate={m.group(2)}, "
              f"volume={m.group(3)}{RESET} — run an apply (then restart) to make "
              "notifications match this preview.")
    print("Adjust: " + paths.cmd("features_apply.py", "--param",
                                 "audible_speak_rate=<wpm>", "--preview-speak"))
    sys.exit(0 if not err else 1)

if args.features_json:
    # For the /claudette skill to render conversationally. Everything the
    # numbered list shows, as data. `aligned` marks unanswered features whose
    # desired state already holds — the next apply answers them on with no
    # prompt (I3), so the skill must not offer them as decisions either.
    # `install_stage` drives the staged install (I7): core = hooks + their
    # requires closure (one restart covers them); extra = live-pickup goodies;
    # individual = security posture, one at a time, never bundled in a pack.
    _active, _allow = _active_settings_allow()
    _stages = install_stages()
    _rows = [{
        "id": fid,
        "group": f.get("group", "claudette"),
        "zone": f.get("zone", ""),
        "name": f.get("name", ""),
        "what": f.get("what") or f.get("description", ""),
        "benefit": f.get("benefit", ""),
        "cost": f.get("cost", ""),
        "alternative": f.get("alternative", ""),
        # Optional prose for the recommendation slot, for a feature whose worth
        # depends on facts only the user has (chart_autoupdate: more than one
        # machine?). Relayed, never evaluated — claudette does not guess.
        "recommend_note": f.get("recommend_note", ""),
        "recommended": f.get("default"),
        "requires": f.get("requires") or [],
        "params": sorted((f.get("params") or {}).keys()),
        "state": "on" if prefs.enabled(fid)
                 else "off" if prefs.answered(fid)
                 else "unanswered" if prefs.available(fid)
                 else "unavailable",
        "available": prefs.available(fid),
        "substrate": f.get("substrate", ""),
        "aligned": _is_aligned(fid, f, _active, _allow),
        "install_stage": _stages[fid],
        "arms_enforcement": bool(f.get("arms_enforcement")),
        "deferred": fid in prefs.deferred_features(),
    } for fid, f in prefs.features().items()]
    # Enforcement-arming features (auto permission mode) sink to the end so
    # every flow that presents rows in order asks the grants first: arming the
    # classifier before its exemptions exist denies the flow's own next script
    # call. Stable — everything else keeps registry order.
    _rows.sort(key=lambda r: r["arms_enforcement"])
    if args.only == "needs-decision":
        # What an update must present, nothing else: unanswered (deferred are a
        # subset — offered again per CLAUDETTE_UPDATE.md), substrate present
        # (I4), not already aligned (I3).
        _rows = [r for r in _rows if r["state"] == "unanswered" and not r["aligned"]]
    print(json.dumps({
        "root": paths.tilde(paths.ROOT, "~"),
        "layout": "plugin" if paths.IS_PLUGIN else "flat",
        "groups": prefs.groups(),
        "zones": prefs.zones(),
        "suspended_zones": prefs.suspended_zones(),
        "params": {k: prefs.param(k) for k in prefs.param_defaults()},
        "ask_params": prefs.defs().get("ask_params") or [],
        "only": args.only,
        "features": _rows,
        "unanswered": prefs.unanswered(),
        "orphans": prefs.orphans(),
        "param_orphans": prefs.param_orphans(),
    }, indent=2))
    sys.exit(0)

# Values GCM reads as "show me no UI". It accepts the boolean and the legacy
# tri-state spelling for the same setting, so a check for one of them alone
# would report a correctly-configured machine as unconfigured.
_CRED_OFF = {"false", "never", "0"}


def _credential_config():
    """Every `credential.*` key from git's SYSTEM then GLOBAL scope, in that
    precedence order. Two subprocess calls, no network, no helper spawned.

    The scopes are named explicitly: a bare `git config --get` ALSO reads the
    repo config of whatever directory the doctor happens to run in, so the same
    machine would answer differently per cwd — unacceptable for a row that
    speaks about the machine. Returns None when git cannot be asked at all; no
    row is better than a guess about a helper that may not even be installed."""
    out = {"helpers": [], "interactive": "", "scope": "", "scoped_off": []}
    for scope in ("--system", "--global"):
        try:
            r = subprocess.run(["git", "config", scope, "--get-regexp", r"^credential\."],
                               capture_output=True, text=True, timeout=5,
                               encoding="utf-8", errors="replace")
        except Exception:
            return None  # git absent, unreadable, or slower than this row is worth
        if r.returncode not in (0, 1):  # 1 is "nothing matched", which is normal
            continue
        for line in r.stdout.splitlines():
            key, _, val = line.partition(" ")
            key, val = key.strip().lower(), val.strip()
            if key == "credential.helper":
                # An EMPTY credential.helper RESETS the list git has built so
                # far. A machine that deliberately clears an inherited system
                # helper must not still read as "the manager runs here".
                out["helpers"] = out["helpers"] + [val] if val else []
            elif key == "credential.interactive":
                out["interactive"], out["scope"] = val, scope.lstrip("-")
            elif key.startswith("credential.") and key.endswith(".interactive"):
                if val.lower() in _CRED_OFF:
                    # The middle is a URL and carries dots of its own, so slice
                    # the known ends off rather than splitting on ".".
                    out["scoped_off"].append(key[len("credential."):-len(".interactive")])
    return out


def doctor_rows(repair=True):
    """Per-feature health as data — every problem row names its fix.
    `repair=True` is the --doctor contract: the mixer row and the
    staleness-cache sweep are the two places claudette repairs rather than
    reports. --update-plan passes False — a report generated on every update
    must not mutate anything, not even a mixer slider the user may have
    lowered on purpose."""
    rows = []  # (feature, ok|warn|off, detail, remedy or "")

    # Python floor is 3.9 — Apple's Command Line Tools pin it (I6); checked
    # here and at first run, never per render.
    via = os.environ.get("CLAUDETTE_VIA", "")
    rows.append(("python (this run)", "ok" if sys.version_info >= (3, 9) else "warn",
                 f"{platform.python_version()} at {python_resolve.norm(sys.executable)}"
                 + (", via run.sh" if via == "run.sh" else ", launched outside run.sh"),
                 "" if sys.version_info >= (3, 9)
                 else "install Python >= 3.9 (macOS: xcode-select --install)"))
    # The interpreter the hooks and status line ACTUALLY run — validated from a
    # non-login bash, the context the agent's shell does not share. The row
    # above says nothing about it: this process may have been launched any way.
    stored = python_resolve.norm(prefs.param("python", ""))
    if stored:
        v = python_resolve.validate(stored)
        rows.append(("python (hooks)", "ok" if v["ok"] else "warn",
                     f"{stored}  {_vver(v)}{_vtag(v)}" if v["ok"]
                     else f"{stored} — {v['reason']}",
                     "" if v["ok"] else paths.cmd("features_apply.py", "-y")
                     + "  (re-resolves; or --param python=<full path>)"))
    else:
        rows.append(("python (hooks)", "warn",
                     "not resolved — hook commands look Python up on PATH at run time, "
                     "the lookup that fails on a fresh Windows install",
                     paths.cmd("features_apply.py", "-y") + "  (resolves and stores it)"))

    opt = optional_dep_warnings()
    rows.append(("dependencies", "ok" if not opt else "warn",
                 "none required" if not opt else opt[0],
                 "" if not opt else pip_cmd))

    # Tone artifacts: an enabled tone feature with no generated wav is silently
    # mute — exactly the class of failure --doctor exists to catch.
    stale_tones = [fid for fid in tone_features()
                   if prefs.enabled(fid) and not (GENERATED / (fid + ".wav")).exists()]
    if any(prefs.enabled(f) for f in tone_features()):
        rows.append(("generated tones", "ok" if not stale_tones else "warn",
                     "all present" if not stale_tones else f"missing: {', '.join(stale_tones)}",
                     "" if not stale_tones else paths.cmd("features_apply.py", "-y")))

    # Runtime degradation markers dropped by the hooks (audio, speech): a hook
    # must fail silently in the moment, but not invisibly forever.
    for marker in sorted(pathlib.Path(paths.LOGS).glob("*_degraded.json")):
        info = _read_json_or(marker)
        rows.append((marker.stem.replace("_", " "), "warn",
                     str(info.get("reason", "unknown"))[:80],
                     "clears on next success, or: rm " + paths.tilde(str(marker), "~")))

    if not prefs.enabled("statusline"):
        rows.append(("statusline", "off", "switched off for this machine",
                     paths.cmd("features_apply.py", "--prefs")))
    else:
        st = _read_json_or(paths.USAGE_LAST)
        age = int(time.time() - (st.get("ts") or 0)) if st else None
        rows.append(("statusline", "ok" if age is not None and age < 86400 else "warn",
                     f"last render {age}s ago" if age is not None
                     else "never rendered (no claude_usage_last.json)",
                     "" if age is not None and age < 86400 else "check statusLine in settings.json"))

    # Run logs: never a warn — an absent folder just means nothing has changed
    # config on this machine yet, which is not a fault. The row exists to make
    # the newest transcript findable without knowing the naming scheme.
    newest = paths.newest_run_log() if os.path.isdir(paths.CONFIG_RUNS) else None
    kept = len(list(pathlib.Path(paths.CONFIG_RUNS).glob("*.log"))) if newest else 0
    rows.append(("config runs", "ok",
                 f"{kept} kept, newest {paths.tilde(newest, '~')}" if newest
                 else "none yet (written by the next apply)", ""))

    if paths.IS_PLUGIN:
        n_hooks = sum(len(v) for v in (_read_json_or(HOOKS_JSON).get("hooks") or {}).values())
        want_hooks = any(prefs.effective(fid) and fdef.get("hooks")
                         for fid, fdef in prefs.features().items())
        if not want_hooks:
            rows.append(("plugin hooks", "off", "no hook feature on", ""))
        else:
            rows.append(("plugin hooks", "ok" if n_hooks else "warn",
                         f"{n_hooks} entr{'y' if n_hooks == 1 else 'ies'} in hooks/hooks.json"
                         if n_hooks else "hooks/hooks.json missing or empty",
                         "" if n_hooks else paths.cmd("features_apply.py", "-y")))

    # Hook audio: the per-app Windows mixer slider is shared by EVERY python.exe
    # on the machine, and a zeroed one silences all hook sound with no error
    # anywhere (see winaudio.py). This row is the only place in the doctor that
    # repairs rather than reports — deliberate: the remedy is one COM call, and
    # printing "go find the mixer" wastes the trip. The sound hooks also heal
    # it before every playback; the row says how often they had to.
    if not any(prefs.enabled(f) for f in prefs.features() if f.startswith("audible_")):
        rows.append(("hook audio (mixer)", "off", "no audio feature enabled", ""))
    else:
        try:
            import winaudio
            if not winaudio.IS_WINDOWS:
                audio = None
            elif repair:
                audio = winaudio.repair()
            else:
                st = winaudio.read()
                audio = None if st is None else {
                    "changed": False, "before": st, "after": st,
                    "silenced": st["muted"] or st["volume"] < winaudio.MIN_AUDIBLE}
        except Exception:
            winaudio, audio = None, None
        if winaudio is not None and not winaudio.IS_WINDOWS:
            rows.append(("hook audio (mixer)", "off", "not applicable on this OS", ""))
        elif audio is None:
            rows.append(("hook audio (mixer)", "warn", "could not read this app's session volume",
                         "check the OS sound devices; hooks may still work"))
        elif audio.get("silenced"):
            b = audio["before"]
            was = "muted" if b["muted"] else f"at {b['volume'] * 100:.0f}%"
            rows.append(("hook audio (mixer)", "warn",
                         f"python.exe is {was} in the Windows mixer — all hook audio is silent",
                         "/claudette doctor repairs it"))
        elif audio["changed"]:
            b = audio["before"]
            was = "muted" if b["muted"] else f"{b['volume'] * 100:.0f}%"
            rows.append(("hook audio (mixer)", "warn",
                         f"python.exe was {was} in the Windows mixer — RAISED to 100%", ""))
        else:
            healed = _read_json_or(paths.log("audio_mixer_healed.json"))
            note = ""
            if isinstance(healed, dict) and healed.get("count"):
                fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t or 0))
                note = (f"; hooks self-healed it {healed['count']}x since "
                        f"{fmt(healed.get('first_ts'))}, last {fmt(healed.get('ts'))}")
            rows.append(("hook audio (mixer)", "ok",
                         f"python.exe at {audio['after']['volume'] * 100:.0f}%, not muted{note}", ""))

    if platform.system() == "Darwin" and not (CLAUDE_DIR / ".credentials.json").exists():
        kc = os.path.exists(paths.log("keychain_ok.json"))
    else:
        kc = True
    lim = _read_json_or(paths.LIMITS_LAST)
    lim_age = int(time.time() - (lim.get("ts") or 0)) if lim.get("ts") else None
    if not prefs.enabled("statusline"):
        rows.append(("scoped usage limits", "off", "statusline off — not in use", ""))
    elif os.path.exists(paths.log("claude_limits_off")):
        rows.append(("scoped usage limits", "off", "kill switch present",
                     "rm " + paths.log("claude_limits_off")))
    elif not kc:
        rows.append(("scoped usage limits", "warn", "macOS Keychain not authorized",
                     paths.cmd("features_apply.py", "--auth")))
    elif not lim:
        # never refreshed is a fresh machine, not a failure — the first
        # statusline render fetches
        rows.append(("scoped usage limits", "ok", "no refresh yet (statusline will fetch)", ""))
    elif not lim.get("ok"):
        rows.append(("scoped usage limits", "warn",
                     f"last refresh failed: {str(lim.get('error'))[:80]}",
                     paths.cmd("features_apply.py", "--auth")))
    else:
        names = ", ".join(f"{s.get('name')} {s.get('pct')}%" for s in (lim.get("scoped") or [])) \
                or "none for this account"
        rows.append(("scoped usage limits", "ok", f"{names} (cache {lim_age}s old)", ""))

    up = pathlib.Path(paths.log("git_upstreams"))
    if (up / "off").exists():
        rows.append(("git upstream staleness", "off", "kill switch present", f"rm {up / 'off'}"))
    else:
        # A refused credential is the one upstream failure that must be filed as
        # a warn: it does not heal, and a checkout that cannot reach its remote
        # cannot learn it is behind — so every OTHER report (statusline, update
        # plan, done board) goes quiet exactly when it matters. Read from the
        # cache files, never by probing: --doctor is read-only and offline-safe.
        files = sorted(up.glob("*.json")) if up.exists() else []
        n, blocked, pruned, foreign = 0, [], 0, 0
        _scope = {os.path.normcase(os.path.normpath(d))
                  for d in (paths.ROOT, paths.CLAUDE_DIR)}
        for p in files:
            data = _read_json_or(p)
            if not isinstance(data, dict):
                continue
            # Liveness before anything else: the cache is written only when a
            # repo is asked about, so a deleted repo's file survives forever —
            # and its last recorded truth would be warned about below. This
            # sweep is the only reader that visits every file, which makes it
            # the natural eviction point (repair=True only; entries in one
            # file share a root, so any entry's `repo` speaks for the file).
            repo_path = next((e.get("repo") for e in data.values()
                              if isinstance(e, dict) and e.get("repo")), None)
            if repo_path and not os.path.exists(os.path.join(repo_path, ".git")):
                # .git may be a FILE (worktree/submodule) — exists, not isdir
                if repair:
                    try:
                        p.unlink()
                        pruned += 1
                    except OSError:
                        pass
                continue
            n += 1
            for br, e in data.items():
                if not isinstance(e, dict) or e.get("error") != "auth-failed":
                    continue
                succ = e.get("last_success_at")
                since = (f"last answered {time.strftime('%Y-%m-%d', time.localtime(succ))} "
                         f"({(int(time.time()) - int(succ)) // 86400}d stale)"
                         if succ else "never answered on this machine")
                repo = e.get("repo") or p.stem
                # git's first line names the cause (a credential helper's own
                # error comes first): long enough to read it whole.
                why = redact.mask(((e.get("last_error") or "").splitlines() or [""])[0][:160])
                # The cache spans every repo any session's statusline ever
                # rendered in. A claudette doctor warn about an unrelated
                # private repo the user merely visited is honest but noise:
                # warn only for the repos claudette itself syncs — elsewhere
                # the statusline already shows ⚠auth in-repo, where the user
                # actually works on it.
                if os.path.normcase(os.path.normpath(repo)) in _scope:
                    blocked.append((repo, br, since, why))
                else:
                    foreign += 1
        for repo, br, since, why in blocked:
            rows.append(("git upstream staleness", "warn",
                         f"{paths.tilde(repo, '~')} ({br}): git could not authenticate to the "
                         f"remote — staleness unknown, not 0; {since}"
                         + (f"; git said: {why}" if why else ""),
                         f'git -C "{repo}" ls-remote origin '
                         "# once what git said is fixed (may ask for a credential), "
                         "then /claudette update"))
        if repair:
            # _store() writes <file>.json.tmp<pid> then os.replace()s it; a
            # process killed in between (render timeout) strands the tmp with
            # no other collector. An hour of age rules out a write in flight.
            cutoff = time.time() - 3600
            for t in (up.glob("*.json.tmp*") if up.exists() else []):
                try:
                    if t.stat().st_mtime < cutoff:
                        t.unlink()
                        pruned += 1
                except OSError:
                    pass
        if not blocked:
            rows.append(("git upstream staleness", "ok", f"{n} repo(s) cached"
                         + (f", {pruned} dead file(s) pruned" if pruned else "")
                         + (f"; {foreign} other repo(s) with a refused credential "
                            "(statusline flags them in-repo)" if foreign else ""), ""))

    # DETECTION ONLY, and deliberately never a warn. A credential helper left
    # waiting on a dialog nobody will answer is what strands one for days;
    # credential.interactive=false closes that class by failing fast instead.
    # It also removes the re-auth dialog from the user's OWN pushes and from
    # anything else sharing this git config, and whether that trade is right
    # depends on the machine — so this row states what is true and stops. A
    # warn would set the exit code, land in --update-plan's warn list, and nag
    # every machine into one answer. See CLAUDETTE_DESIGN.md "Credential
    # prompts: detect, never decide".
    cred = _credential_config()
    if cred is not None and any("manager" in h.lower() for h in cred["helpers"]):
        if cred["interactive"].lower() in _CRED_OFF:
            detail = (f"off machine-wide (credential.interactive={cred['interactive']} in "
                      f"{cred['scope']} config) — no prompt can strand a helper; a push "
                      "needing re-auth fails with an auth error instead of asking "
                      "(git -c credential.interactive=true <cmd> to re-auth once)")
        elif cred["scoped_off"]:
            detail = ("on, off for " + ", ".join(cred["scoped_off"])
                      + " — every OTHER host can still strand a helper on an "
                        "interrupted or timed-out remote call")
        else:
            # "unset" and an explicit true/always/auto both leave prompts on,
            # but they are not the same news: the second says someone already
            # decided this, and a row that calls it "unset" invites undoing a
            # deliberate choice.
            how = (f"credential.interactive={cred['interactive']} in {cred['scope']} config"
                   if cred["interactive"] else "credential.interactive unset")
            detail = (f"on ({how}) — an interrupted or timed-out remote call can leave a "
                      "credential helper waiting on a dialog for days; turning prompts off "
                      "is a per-machine call (design notes, not published: "
                      "CLAUDETTE_DESIGN.md \"Credential prompts\")")
        env = os.environ.get("GCM_INTERACTIVE", "").strip()
        if env:
            detail += (f"; GCM_INTERACTIVE={env} in THIS process's environment overrides "
                       "the above, and says nothing about the user's own shell")
        rows.append(("git credential prompts", "ok", detail, ""))

    # Auto-fetch rows come from the recorded decisions only, like the staleness
    # cache above. They run with the feature off too: that is when "repos opted
    # in, kept" tells the user what switching it back on restores.
    try:
        import git_autofetch
        rows.extend(git_autofetch.doctor_rows(prefs.effective("git_auto_fetch")))
    except Exception as e:
        rows.append(("git auto-fetch", "warn", f"state unreadable: {str(e)[:80]}", ""))

    # The idle done report (auto_done) runs on a 1-hour prompt cache only in its
    # default mode, and idle compaction needs one too - so say what the newest
    # session actually got from the API, then what was seen of idle compaction.
    try:
        import cache_ttl
        import idle_compaction
        ttl, _ = cache_ttl.newest_session_ttl(paths.HOME)
        rows.extend(cache_ttl.doctor_rows(ttl, prefs.effective("auto_done"),
                                          prefs.effective("prompt_cache_1h")))
        rows.extend(idle_compaction.doctor_rows(prefs.effective("auto_done")))
        import hook_idle_timer
        rows.extend(hook_idle_timer.doctor_rows(prefs.effective("auto_done")))
    except Exception as e:
        rows.append(("prompt cache", "warn", f"check failed: {str(e)[:80]}", ""))

    # .note stores by the client recording the blocked prompt — undocumented, and
    # a client upgrade can stop it. Here because this report also runs on every
    # update (doctor_warns). No row until a note exists.
    try:
        import dot_note
        rows.extend(dot_note.doctor_rows())
    except Exception as e:
        rows.append((".note recording", "warn", f"check failed: {str(e)[:80]}", ""))

    # The claudette clone's own .claudette.json is committed to the SHARED
    # claudette repo: an override there reaches every other clone.
    shared = os.path.join(paths.ROOT, ".claudette.json")
    if os.path.exists(shared):
        data = _read_json_or(pathlib.Path(shared))
        params = sorted((data.get("params") or {}) if isinstance(data, dict) else {})
        if params:
            try:
                tracked = subprocess.run(
                    ["git", "-C", paths.ROOT, "ls-files", "--error-unmatch", "--", ".claudette.json"],
                    capture_output=True, timeout=30).returncode == 0
            except Exception:
                tracked = True  # unknown counts as the unsafe answer
            reach = ("is committed to the shared claudette repo, so every other clone gets it"
                     if tracked else
                     "is not committed yet — once it is, every other clone gets it")
            rows.append(("project overrides", "warn",
                         f"{paths.tilde(shared, '~')} sets {', '.join(params)} — this file {reach}",
                         "move it to .claudette.local.json in the same folder"))

    try:
        rows.extend(user_copy.doctor_rows())
    except Exception as e:
        rows.append(("user copies", "warn", f"check failed: {str(e)[:80]}", ""))

    if prefs.enabled("pretool_throttle"):
        thr = _read_json_or(paths.log("throttle_control.json"))
        rows.append(("token throttle", "off" if thr.get("off") else "ok",
                     "bypassed by /claudette:throttle off" if thr.get("off") else "active",
                     "/claudette:throttle on" if thr.get("off") else ""))

    susp = prefs.suspended_zones()
    if susp:
        rows.append(("suspended zones", "off",
                     ", ".join(susp) + " — effects off, answers kept",
                     paths.cmd("features_apply.py", "--resume", "<zone>", "-y")))

    # A CLAUDE.md import pointing at nothing. The generated leaf is claudette's
    # own — an apply recreates it. Any other target is the user's file, so that
    # row REPORTS and stops: the remedy is a decision about the user's own file
    # (see CLAUDETTE_UPDATE.md step 7), never a write.
    for n, raw, p in _md_missing_imports(CLAUDE_MD):
        if p.name == LEAF_NAME:
            rows.append(("CLAUDE.md imports", "warn",
                         f"line {n} imports @{raw} — the generated leaf is missing, so the "
                         f"literal text loads into every session",
                         paths.cmd("features_apply.py", "-y")))
            continue
        rows.append(("CLAUDE.md imports", "warn",
                     f"line {n} imports @{raw} — {paths.tilde(str(p), '~')} does not exist, "
                     f"so the literal text loads into every session instead of a file",
                     f"decide: create {paths.tilde(str(p), '~')} for this machine's own "
                     f"instructions (worth having even empty), or drop the @ line"))

    # Drift: an approved feature whose state no longer holds. Report only —
    # the value there is the user's now (I2); doctor never re-asserts.
    active_now = _read_json_or(SETTINGS) if SETTINGS.exists() else {}
    allow_now = ((active_now.get("permissions") or {}).get("allow")) or []
    drifted = []
    for fid, fdef in prefs.features().items():
        if not prefs.effective(fid):
            continue
        f = _expand(fdef)
        if f.get("settings") and not _has_payload(active_now, f["settings"]):
            drifted.append(fid)
        elif f.get("permissions") and not all(r in allow_now for r in f["permissions"]):
            drifted.append(fid)
    if drifted:
        rows.append(("settings drift", "warn",
                     "approved but not in effect: " + ", ".join(drifted),
                     "yours if you changed it; re-assert: "
                     + paths.cmd("features_apply.py", "--take-source")
                     + "  stop ensuring: --feat <id>=off"))

    on = [f for f in prefs.features() if prefs.enabled(f)]
    new = prefs.unanswered()
    deferred = [f for f in new if f in prefs.deferred_features()]
    pending = [f for f in new if f not in deferred]
    rows.append(("features", "warn" if pending else "ok",
                 f"{len(on)}/{len(prefs.features())} on"
                 + (f"; {len(pending)} never answered for" if pending else "")
                 + (f"; {len(deferred)} deferred" if deferred else ""),
                 paths.cmd("features_apply.py", "--update") if pending else ""))

    # Prefs hygiene: keys the registry no longer knows. A feature orphan is not a
    # stray answer — it is a registry entry deleted without being retired, so the
    # removal never reaches any OTHER machine. Name that, rather than offering a
    # --param command that cannot drop a feature key (--feat <id>=null refuses it
    # too: no such feature). A stale param override merely sits there doing
    # nothing, which is worth its own, separate warning.
    fstray = list(prefs.orphans())
    pstray = list(prefs.param_orphans())
    if fstray:
        rows.append(("prefs hygiene", "warn",
                     "feature(s) removed from the registry without being retired: "
                     + ", ".join(fstray),
                     "add the id to _RETIRED_FEATURES in scripts/features_apply.py "
                     "(its params to _RETIRED_PARAMS), then re-run --update"))
    if pstray:
        rows.append(("prefs hygiene", "warn",
                     "unknown param override(s): " + ", ".join(pstray),
                     paths.cmd("features_apply.py", "--param", "<name>=null") + " to drop"))
    return rows


if args.features_table:
    # The compact /claudette prefs listing: one line per feature, relayed to the
    # user verbatim. The four decision fields are the CONSENT apparatus and stay
    # mandatory in update, where a yes is being asked for; prefs reviews
    # decisions already made, so here they load on demand (--explain).
    _feats = prefs.features()
    _deferred = prefs.deferred_features()

    def _short(v):
        # A menu line must stay a line: long values (the statusline segment
        # list) collapse to their size; --explain / --features-json carry the
        # full value for whoever asked.
        s = str(v)
        if len(s) <= 24:
            return s
        return f"[{len(v)} items]" if isinstance(v, list) else s[:23] + "…"

    # Global params FIRST. They belong to no feature, so they get no row below
    # (audio_device can route every sound on the machine and never appear).
    # Before the list and not after because the numbered rows ARE the menu:
    # anything printed under them lands between the menu and its invite, and a
    # reader who has scrolled that far is looking for a number to type.
    _globals = prefs.defs().get("params") or {}
    if _globals:
        _ov = prefs.param_overrides()
        print(f"{BOLD}--- Claudette global parameters ({len(_globals)}) ---{RESET}")
        print("  Not owned by any one feature — they apply wherever they are read.")
        for _k in _globals:                      # registry order: grouped by concern
            _v = prefs.param(_k)
            print(("  %-22s %-24s%s" % (_k, _short(_v) if _v != "" else "(unset)",
                                        "*" if _k in _ov else "")).rstrip())
        if any(_k in _ov for _k in _globals):    # no legend for an absent mark
            print("  (* set on this machine; the rest are registry defaults)")
        print("  Type a parameter name to change it; `<name>=null` restores its default.\n")

    print(f"{BOLD}--- Claudette features ({len(_feats)}) ---{RESET}")
    _group = None
    for _n, (fid, fdef) in enumerate(_feats.items(), 1):
        if fdef.get("group", "claudette") != _group:
            _group = fdef.get("group", "claudette")
            print(f"  {BOLD}{_group}{RESET}")
        state = ("on" if prefs.enabled(fid)
                 else "off" if prefs.answered(fid)
                 else "deferred" if fid in _deferred
                 else "unanswered" if prefs.available(fid)
                 else "unavailable")
        rec = fdef.get("default")
        mark = "*" if (rec is not None and prefs.answered(fid)
                       and prefs.enabled(fid) is not rec) else " "
        params = ", ".join(f"{k}={_short(prefs.param(k))}"
                           for k in sorted(fdef.get("params") or {}))
        print(f"  {_n:3} {state:11}{mark} {fid:36} {fdef.get('name', '')}"
              + (f"  [{params}]" if params else ""))
    print("  (* differs from the recommendation)")
    print("  Type a number or id for details and tweaking options.")
    sys.exit(0)

if args.explain:
    _feats = prefs.features()
    ids = [i.strip() for i in args.explain.split(",") if i.strip()]
    bad = [i for i in ids if i not in _feats]
    if bad:
        print(f"{RED}No such feature: {', '.join(bad)}{RESET} — ids are listed by --features-table")
        sys.exit(2)
    for fid in ids:
        fdef = _feats[fid]
        state = ("on" if prefs.enabled(fid) else "off" if prefs.answered(fid)
                 else "unanswered" if prefs.available(fid) else "unavailable")
        rec = fdef.get("default")
        print(f"\n{BOLD}{fid}{RESET} — {fdef.get('name', '')}  ({state}; "
              + ("recommended on" if rec is True else "recommended off" if rec is False
                 else "no recommendation — must be answered explicitly") + ")")
        for key, label in (("what", "What"), ("benefit", "Benefit"),
                           ("cost", "Cost"), ("alternative", "Alternative")):
            if fdef.get(key):
                print(f"  {label}: {_wrap(fdef[key], 86, 2 + len(label) + 2)}")
        if fdef.get("recommend_note"):
            print(f"  Recommendation: {_wrap(fdef['recommend_note'], 86, 2 + 16)}")
    sys.exit(0)

if args.md_dirt:
    print(_md_dirt_scope())
    sys.exit(0)

if args.doctor:
    rows = doctor_rows(repair=True)
    print(f"{BOLD}--- Claudette doctor ---{RESET}")
    print(f"  version: {paths.version() or '? (no readable .claude-plugin/plugin.json)'}")
    print(f"  layout: {'plugin' if paths.IS_PLUGIN else 'flat'} at {paths.tilde(paths.ROOT, '~')}")
    mark = {"ok": f"{GREEN}ok  {RESET}", "warn": f"{RED}warn{RESET}", "off": f"{CYAN}off {RESET}"}
    for feat, state, detail, remedy in rows:
        print(f"  {mark[state]} {feat}: {detail}")
        if remedy:
            print(f"       fix: {remedy}")
    print("\nConfig sync status: add -s for the settings/CLAUDE.md diff.")
    sys.exit(0 if all(r[1] != "warn" for r in rows) else 1)


def _update_held():
    """The hold update_screen.py recorded, while the install still sits at the commit it
    screened from; None otherwise, or when it cannot be read (the plan must not fail)."""
    try:
        import update_screen
        hold = update_screen.current_hold()
    except Exception:
        return None
    if not hold:
        return None
    return {"to": hold.get("to"), "findings": hold.get("findings") or []}


if args.update_plan:
    # The /claudette update fast path: one small JSON that says whether an
    # update needs any conversation at all. Read-only: answers nothing, writes
    # nothing (run-log gate), repairs nothing (doctor_rows(repair=False)).
    #
    # pull.sh runs this AFTER the apply and snapshots two facts from BEFORE
    # the pull into the environment, because the apply itself may create
    # prefs.local.json on a virgin machine (align_unanswered) — a post-hoc
    # first-contact test would lie.
    _active, _allow = _active_settings_allow()
    _stages = install_stages()
    _feats = prefs.features()
    _deferred = prefs.deferred_features()
    # Deferred stay OUT of needs_decision: one deferred security feature must
    # not force the full choreography on every later update. They are reported
    # in their own field so the fast path can still name them in one line —
    # "offered again" (CLAUDETTE_UPDATE.md) survives as that mention.
    _need = [fid for fid in prefs.unanswered()
             if fid not in _deferred
             and not _is_aligned(fid, _feats[fid], _active, _allow)]
    _last = _read_json_or(pathlib.Path(paths.log("last_apply.json")))
    _pre = os.environ.get("CLAUDETTE_PRE_PULL_PREFS")
    _reg = os.environ.get("CLAUDETTE_REGISTRY_CHANGED")
    # pull.sh checks each `git pull` rc and classifies the failure (it is the
    # only place the stderr exists). It rides in the environment for the same
    # reason the pre-pull snapshots do — and it must be IN the plan, because the
    # plan is the block that survives a caller keeping only the tail: a pull
    # that could not authenticate otherwise renders as "nothing to decide".
    # Repos come newline-separated (a home folder may hold a space); a pull.sh
    # from before 1.2.5, still running while this file was updated under it,
    # separates them with spaces. `git_said` is git's own last lines per repo,
    # so the report names the cause in git's words instead of guessing one.
    _pull_fail = os.environ.get("CLAUDETTE_PULL_FAILED")
    _said = {}
    for _ln in os.environ.get("CLAUDETTE_PULL_SAID", "").splitlines():
        _repo, _tab, _text = _ln.partition("\t")
        if _tab:
            _said.setdefault(_repo, []).append(redact.mask(_text))
    if _pull_fail:
        _fail_repos = _pull_fail.split(":", 1)[1]
        _fail_repos = ([r for r in _fail_repos.splitlines() if r.strip()]
                       if "\n" in _fail_repos.strip() or _fail_repos.strip() in _said
                       else _fail_repos.split())
    print(json.dumps({
        "pull_failed": ({"reason": _pull_fail.split(":", 1)[0],
                         "repos": _fail_repos,
                         "git_said": {r: _said[r] for r in _fail_repos if r in _said}}
                        if _pull_fail else None),
        "first_contact": (_pre == "0") if _pre in ("0", "1")
                         else not os.path.exists(prefs.LOCAL_PATH),
        "needs_decision": [{"id": fid, "name": _feats[fid].get("name", ""),
                            "stage": _stages[fid]} for fid in _need],
        "deferred": _deferred,
        "unavailable": [fid for fid in _feats if not prefs.available(fid)],
        "on": sum(1 for f in _feats if prefs.enabled(f)),
        "off": sum(1 for f in _feats if prefs.answered(f) and not prefs.enabled(f)),
        "ask_params_pending": [p for p in (prefs.defs().get("ask_params") or [])
                               if p not in prefs.param_overrides()],
        # A newer shipped text for a user copy (the turn style): a question for
        # the user, so it routes the update to CLAUDETTE_UPDATE.md like an
        # unanswered feature does.
        "copy_updates": [s for s in user_copy.states() if s["pending"]],
        # Interpreter the hooks render with (absolute path) — null means the
        # PATH-lookup fallback is in force; doctor_warns then says why.
        "python": python_resolve.norm(prefs.param("python", "")) or None,
        "registry_changed": {"0": False, "1": True}.get(_reg),
        # An update the screen held on an install (update_screen.py): the commit it
        # stopped at and what it found. Not a failure: pull_failed stays null.
        "update_held": _update_held(),
        # claudette's version before the pull (pull.sh's snapshot) and now; the
        # report names a change and points to CHANGELOG.md.
        "version": {"before": os.environ.get("CLAUDETTE_VERSION_BEFORE") or None,
                    "now": paths.version()},
        "restart_needed": bool(_last.get("hooks_changed")) if _last else None,
        "last_apply_ts": _last.get("ts") if _last else None,
        "doctor_warns": [{"feature": f, "detail": d, "fix": r}
                         for f, s, d, r in doctor_rows(repair=False) if s == "warn"],
        # Run-log pointer INSIDE the plan: the plan is the last block of a
        # pull.sh run, and the last lines are what survive a caller that
        # truncates (CLAUDETTE_DESIGN.md "Run log for state-changing runs").
        "run_log": _last.get("run_log") if _last else None,
    }, indent=2))
    sys.exit(0)


def align_unanswered():
    """I3 — an unanswered feature whose desired state ALREADY holds needs no
    approval: there is nothing to do. Answer it on from the evidence, the same
    rule migrate() applies to a whole pre-prefs machine. The capture pass then
    records prior = the value already there, so switching it off later keeps
    the user's value. Runs BEFORE --feat processing, so an explicit `=null`
    in the same call still ends unanswered."""
    active = _read_json_or(SETTINGS) if SETTINGS.exists() else {}
    allow = ((active.get("permissions") or {}).get("allow")) or []
    aligned = []
    for fid in prefs.unanswered():
        f = _expand(prefs.features()[fid])
        if f.get("settings"):
            ok = _has_payload(active, f["settings"])
        elif f.get("permissions"):
            ok = all(r in allow for r in f["permissions"])
        else:
            continue  # hooks/claude_md payloads are claudette-generated — they
            # cannot pre-exist on a machine claudette never wrote to
        if ok and not prefs.missing_requirements(fid):
            aligned.append(fid)
    if aligned:
        prefs.write(features={fid: True for fid in aligned})
        print(f"{BOLD}--- Aligned ---{RESET}")
        print(f"  Already in place on this machine (nothing to do), "
              f"answered on: {', '.join(aligned)}")
    return aligned


# --------------------------------------------------------------------------
# Feature answers (before assembling anything)
# --------------------------------------------------------------------------
if not args.status:
    migrate()
    align_unanswered()

if args.uninstall:
    # Uninstall IS suspend-everything: settings restore from the undo records,
    # hooks.json regenerates empty, the CLAUDE.md block drops, tones go. The
    # answers survive — `--resume all` is the reinstall.
    print(f"{BOLD}--- Uninstall ---{RESET}")
    print("Reverting everything claudette put on this machine. Answers, prefs,")
    print("logs and the clone stay; deleting those is a separate step, printed")
    print("at the end and never run by this script.")
    if not args.yes and not _ask("Proceed? (y/n): ", "y").strip().lower().startswith("y"):
        print("Cancelled — nothing changed.")
        sys.exit(0)
    args.suspend = list(prefs.zones())
    args.yes = True

if args.suspend or args.resume:
    known_zones = prefs.zones()
    if "all" in args.suspend:
        args.suspend = list(known_zones)
    if "all" in args.resume:
        args.resume = list(known_zones)
    bad = [z for z in args.suspend + args.resume if z not in known_zones]
    if bad:
        print(f"{RED}No such zone: {', '.join(bad)}{RESET} "
              f"(zones: {', '.join(known_zones)})")
        sys.exit(2)
    susp = set(prefs.suspended_zones())
    susp |= set(args.suspend)
    susp -= set(args.resume)
    prefs.write(suspended=susp)
    if susp:
        print(f"Suspended zones (answers untouched): {', '.join(sorted(susp))}")
    else:
        print("No zones suspended.")

if args.set:
    # The kind is stated by the verb, never inferred from the key.
    print(f"{RED}--set was split: use --feat <id>=on|off|null for features, "
          f"--param <name>=value|null for parameters.{RESET}")
    sys.exit(2)

if args.feat or args.param:
    feat_updates, feat_removals, feat_defers = {}, set(), set()
    param_updates, param_removals, bad = {}, set(), []
    known_params = prefs.param_defaults()
    for item in args.feat:
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        if key not in prefs.features():
            hint = " — a parameter; use --param" if key in known_params else ""
            bad.append(f"--feat {item} (no such feature{hint})")
        elif raw.lower() in ("null", ""):
            # `null` is a CLI verb, never a stored value: the key is deleted and
            # the feature returns to unanswered (re-asked on the next update).
            feat_removals.add(key)
        elif raw.lower() in ("defer", "later"):
            # An acknowledgment, not an answer: silences the doctor's
            # never-answered warning; update still offers the feature.
            if prefs.answered(key):
                bad.append(f"--feat {item} (already answered — =null first to defer)")
            else:
                feat_defers.add(key)
        elif raw.lower() not in ("on", "off", "true", "false", "yes", "no", "1", "0"):
            bad.append(f"--feat {item} (expected on/off/defer/null)")
        elif (raw.lower() in ("on", "true", "yes", "1")
              and not prefs.available(key)):
            # I4 — a yes here could not have an effect; name what is missing.
            sub = prefs.features()[key].get("substrate")
            bad.append(f"--feat {item} (substrate missing: {sub} does not exist "
                       f"on this machine)")
        else:
            feat_updates[key] = raw.lower() in ("on", "true", "yes", "1")
    for item in args.param:
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        if raw.lower() in ("null", ""):
            # Same verb for a parameter: delete the override, fall back to the
            # registry default. Also legal for an override the registry no
            # longer knows — it is the only way to clean an orphan out.
            if key in known_params or key in prefs.param_overrides():
                param_removals.add(key)
            else:
                bad.append(f"--param {item} (no such parameter or override)")
        elif key not in known_params:
            hint = " — a feature; use --feat" if key in prefs.features() else ""
            bad.append(f"--param {item} (no such parameter{hint})")
        else:
            try:
                param_updates[key] = json.loads(raw)
            except ValueError:
                param_updates[key] = raw
    if bad:
        print(f"{RED}Not applied:{RESET}\n  " + "\n  ".join(bad))
        sys.exit(2)
    # Dependencies are checked against the resulting state, not the patch alone.
    proposed = {}
    for fid in prefs.features():
        if fid in feat_updates:
            proposed[fid] = feat_updates[fid]
        elif fid in feat_removals:
            proposed[fid] = False
        else:
            proposed[fid] = prefs.enabled(fid)
    blocked = {fid: prefs.missing_requirements(fid, proposed)
               for fid, on in proposed.items() if on and prefs.missing_requirements(fid, proposed)}
    if blocked:
        for fid, missing in blocked.items():
            print(f"{RED}{fid} needs {', '.join(missing)}, which would be off.{RESET}")
        print("Add them to the same call (--feat <id>=on), or turn the dependent feature off.")
        sys.exit(2)
    deferred = None
    if feat_defers or feat_removals:
        # =defer adds; =null clears (re-ask now); answering clears in write().
        deferred = (set(prefs.deferred_features()) | feat_defers) - feat_removals
    prefs.write(features=feat_updates, params=param_updates,
                remove_features=feat_removals, remove_params=param_removals,
                deferred=deferred)
    changed = {**feat_updates, **param_updates}
    reverted = sorted(feat_removals | param_removals)
    print("Set: " + ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(changed.items()))
          + ("" if not reverted else "  Reverted: " + ", ".join(reverted))
          + ("" if not feat_defers else "  Deferred: " + ", ".join(sorted(feat_defers))))

if args.accept_defaults:
    new = prefs.unanswered()
    answerable = [fid for fid in new if prefs.recommendation(fid) is not None]
    must = [fid for fid in new if prefs.recommendation(fid) is None]
    if answerable:
        save_state({fid: prefs.recommended(fid) for fid in answerable})
        print(f"Answered {len(answerable)} feature(s) at their recommended setting.")
    if must:
        # A `default: null` feature carries no recommendation to accept — it can
        # only be answered by an explicit choice.
        print(f"{RED}{len(must)} feature(s) have no recommendation and stay unanswered:{RESET} "
              + ", ".join(must))
        print("  Answer each explicitly: " + paths.cmd("features_apply.py", "--feat", "<id>=on|off"))
elif args.prefs:
    if not sys.stdin.isatty():
        # I5 — consent must not degrade with execution context: a review with
        # no terminal would accept its own proposal on EOF. Answers off-TTY
        # must be explicit.
        print("No interactive terminal — the review needs one. Answer explicitly:")
        print("  " + paths.cmd("features_apply.py", "--feat", "<id>=on|off"))
        sys.exit(0)
    ids = list(prefs.features())
    touched = set()
    state = review(ids, {fid: prefs.enabled(fid) for fid in ids},
                   "Claudette features on this machine",
                   "Everything claudette can do. Off means the config is not applied at all.",
                   touched=touched)
    if state is None:
        print("Cancelled — nothing written.")
        sys.exit(0)
    writable, skipped = finalize_state(state, touched)
    save_state(writable)
    if skipped:
        print(f"{RED}Still unanswered (no recommendation — flip them explicitly):{RESET} "
              + ", ".join(skipped))
elif args.update:
    new = prefs.unanswered()
    if not new:
        print("No new features — everything in the registry has been answered for.")
    elif not sys.stdin.isatty():
        # I5 — off-TTY, 'Enter to accept' would be a batch of silent yeses.
        # Nothing is answered here; the paths that answer are explicit.
        print(f"{len(new)} feature(s) unanswered: {', '.join(new)}")
        print("No interactive terminal — nothing answered. Answer explicitly:")
        print("  " + paths.cmd("features_apply.py", "--accept-defaults")
              + "   (recommendations only)")
        print("  " + paths.cmd("features_apply.py", "--feat", "<id>=on|off|defer"))
    else:
        # Staged approval (I7): core first — hooks and their requires closure,
        # the set one restart covers — then live-pickup extras, then security
        # posture one item at a time, never inside a pack. Each pack is written
        # as it is confirmed, so a later cancel keeps earlier progress.
        stages = install_stages()
        packs = [("Core — hooks and what they require (one restart covers these)",
                  [f for f in new if stages[f] == "core"]),
                 ("Extras — picked up live, no restart needed",
                  [f for f in new if stages[f] == "extra"])]
        packs = [(t, ids) for t, ids in packs if ids]
        # Grants before armers, same reason as the --features-json sort: an
        # enforcement-arming answer must be the last thing this flow writes.
        singles = sorted((f for f in new if stages[f] == "individual"),
                         key=lambda f: bool(prefs.features()[f].get("arms_enforcement")))
        total = len(packs) + len(singles)
        n = 0
        cancelled = False
        for title, ids in packs:
            n += 1
            touched = set()
            state = review(ids, {fid: prefs.recommended(fid) for fid in ids},
                           f"[{n}/{total}] {title}",
                           "Nothing here is on yet; Enter accepts the state shown.",
                           touched=touched)
            if state is None:
                cancelled = True
                break
            writable, _skipped = finalize_state(state, touched)
            save_state(writable)
        for fid in [] if cancelled else singles:
            n += 1
            fdef = prefs.features()[fid]
            label = f" — {fdef['name']}" if fdef.get("name") else ""
            print(f"\n{BOLD}[{n}/{total}] Security posture — decided individually{RESET}")
            print(f"  {BOLD}{fid}{RESET}{label}")
            for fkey, flabel in (("what", "What"), ("benefit", "Benefit"),
                                 ("cost", "Cost"), ("alternative", "Alternative")):
                if fdef.get(fkey):
                    print(f"      {GRAY}{flabel}: "
                          f"{_wrap(fdef[fkey], 82, 6 + len(flabel) + 2)}{RESET}")
            if fdef.get("recommend_note"):
                print(f"      {GRAY}Recommendation: "
                      f"{_wrap(fdef['recommend_note'], 82, 6 + 16)}{RESET}")
            ans = _ask("  on / off / Enter = later: ", "later").strip().lower()
            if ans in ("on", "y", "yes"):
                save_state({fid: True})
            elif ans in ("off", "n", "no"):
                save_state({fid: False})
            else:
                print(f"  Left unanswered — asked again next update; "
                      f"--feat {fid}=defer acknowledges it for the doctor.")
        if cancelled:
            print("Stopped — packs already confirmed are kept; the rest stay unanswered.")
            print("Answer explicitly anytime:")
            print("  " + paths.cmd("features_apply.py", "--accept-defaults")
                  + "   (recommendations only)")
            print("  " + paths.cmd("features_apply.py", "--feat", "<id>=on|off|defer"))
    # Parameters flagged `ask_params` deserve attention while still at their
    # default — the owner token being the canonical case.
    for pname in (prefs.defs().get("ask_params") or []):
        if pname not in prefs.param_overrides():
            cur = prefs.param_defaults().get(pname)
            val = _ask(f"Parameter '{pname}' is still the default ({cur!r}) — "
                       f"value for this machine (Enter to keep): ", "").strip()
            if val:
                prefs.write(params={pname: val})
                print(f"  {pname} = {val}")

# The interpreter the hook commands render with — resolved (or re-validated)
# before assemble(), so the very first apply already writes absolute paths.
# Not in the read-only modes (--status writes nothing, by contract) and not
# when uninstalling: both render whatever is stored, or the chain.
if not (args.uninstall or args.status):
    ensure_python()

# User copies before assemble(): a feature whose copy is missing leaves its
# import out of the leaf, so the copy must exist first. Created once, never
# overwritten (user_copy.py); not in the read-only or uninstall paths. Reported
# under the CLAUDE.md section, where the import they back is written.
_seeded = []
if not (args.uninstall or args.status):
    user_copy.seed(_seeded.append)

src_settings, src_hooks, src_md, src_fps = assemble()
wanted_allow = set(((src_settings.get("permissions") or {}).get("allow")) or [])


def reconcile(dst, records, consume=True):
    """Everything that has to be REMOVED from the active settings, in one pass:
    hooks the current feature set does not ask for (all of them in plugin mode),
    permission rules likewise, and — via the undo records — the settings of
    every feature that is no longer on. A feature with no record was never
    applied by claudette and is left alone, however its values compare
    (never-asked is inert, I1/I3). `consume` drops each record as it is
    restored; pass False for a dry run. Returns (count, records_changed)."""
    n = prune_managed_hooks(dst, set() if paths.IS_PLUGIN else src_fps)
    n += prune_allow(dst, wanted_allow)
    changed = False
    for fid in list(records):
        if fid in prefs.features() and prefs.effective(fid):
            continue
        restored, kept = _restore_feature(dst, records[fid])
        n += restored
        if kept:
            print(f"  {fid}: kept {kept} setting(s) changed since claudette "
                  f"wrote them — they are yours now.")
        if consume:
            del records[fid]
            changed = True
    return n, changed


if args.status:
    print(f"{BOLD}--- Dependencies ---{RESET}")
    _opt = optional_dep_warnings()
    if _opt:
        for w in _opt:
            print(f"  {w}")
    else:
        print("None required.")

    new = prefs.unanswered()
    if new:
        print(f"\n{RED}{len(new)} feature(s) never answered for:{RESET} {', '.join(new)}")
        print("  " + paths.cmd("features_apply.py", "--update"))

    print(f"\n{BOLD}--- Settings diff ({'plugin' if paths.IS_PLUGIN else 'flat'} layout) ---{RESET}")
    if not SETTINGS.exists():
        print("settings.json: not yet created (will be created on apply)")
    else:
        dst_data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        conflicts, stale = [], []
        merged = _deep_merge(json.loads(SETTINGS.read_text(encoding="utf-8")),
                             src_settings, _record_resolver(conflicts),
                             rerender=lambda p, _local: stale.append(p))
        reconcile(merged, _load_undo(), consume=False)
        if merged == dst_data and not conflicts:
            print("settings.json: up to date")
        else:
            if stale:
                print("settings.json: claudette-owned entries with a stale rendering, "
                      "re-rendered by the next apply (no conflict): " + ", ".join(stale))
            if merged != dst_data:
                print("settings.json: pending changes (apply will update)")
                _print_diff(SETTINGS.read_text(encoding="utf-8").splitlines(),
                            json.dumps(merged, indent=2).splitlines(),
                            "settings.json", "settings.json (merged)")
            for cpath, local, source in conflicts:
                print(f"  {CYAN}Drift (local differs from registry):{RESET} {cpath}")
                _print_diff(json.dumps(source, indent=2).splitlines(),
                            json.dumps(local, indent=2).splitlines(),
                            f"claudette/{cpath}", f"local/{cpath}")

    if paths.IS_PLUGIN:
        print(f"\n{BOLD}--- Plugin hooks ---{RESET}")
        print("hooks/hooks.json: " + ("pending changes" if write_hooks_json(src_hooks, dry=True)
                                      else "up to date"))

    print(f"\n{BOLD}--- CLAUDE.md diff ---{RESET}")
    dst_text = CLAUDE_MD.read_text(encoding="utf-8", errors="replace") if CLAUDE_MD.exists() else ""
    leaf_now = LEAF.read_text(encoding="utf-8", errors="replace") if LEAF.exists() else ""
    leaf_new = _leaf_text(src_md)
    if leaf_new == leaf_now:
        print(f"{LEAF_NAME}: up to date")
    else:
        print(f"{LEAF_NAME}: pending changes (apply will rewrite)")
        _print_diff(leaf_now.splitlines(), leaf_new.splitlines(),
                    LEAF_NAME, f"{LEAF_NAME} (generated)")
    try:
        merged_md, n_user = _merge_hub(dst_text, bool(src_md))
    except ValueError as e:
        print(f"{RED}CLAUDE.md: {e} — fix the markers manually.{RESET}")
    else:
        if merged_md == dst_text:
            print("CLAUDE.md: up to date")
        else:
            print("CLAUDE.md: import stanza missing (apply will add it)")
            _print_diff(dst_text.splitlines(), merged_md.splitlines(),
                        "CLAUDE.md", "CLAUDE.md (merged)")
        _report_user_rules(n_user)
    sys.exit(0)


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
print(f"\nPython: {python_resolve.norm(sys.executable)}"
      + ("" if os.environ.get("CLAUDETTE_VIA") == "run.sh" else "  (launched outside run.sh)"))
print(f"Claudette: {paths.tilde(paths.ROOT, '~')} ({'plugin' if paths.IS_PLUGIN else 'flat'} layout)")
on_ids = [f for f in prefs.features() if prefs.enabled(f)]
print(f"Features on: {len(on_ids)}/{len(prefs.features())}")

# Warn-once-at-apply discipline (contract §10): the hooks never nag.
for _w in optional_dep_warnings():
    print(f"\n{RED}{_w}{RESET}")

generate_tones()

print(f"\n{BOLD}--- settings.json ---{RESET}")
dst_settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
before = json.dumps(dst_settings, indent=2)
undo = _load_undo()
undo_dirty = _capture_records(dst_settings, undo)
merged = _deep_merge(dst_settings, src_settings, _prompt_resolver(args.yes, args.take_source))
undo_dirty |= _refresh_wrote(merged, undo)
# Captures describe the file as it IS on disk, so they may be saved even if
# the user declines the apply below; consumed records may NOT — the restore
# they describe would never have been written.
undo_captured = copy.deepcopy(undo)
n_removed, undo_consumed = reconcile(merged, undo)
if n_removed:
    print(f"  Removed {n_removed} entr{'y' if n_removed == 1 else 'ies'} claudette no longer owns.")
after = json.dumps(merged, indent=2)
if after == before:
    print("Up to date.")
    if undo_dirty or undo_consumed:
        _save_undo(undo)
else:
    _print_diff(before.splitlines(), after.splitlines(), "settings.json", "settings.json (merged)")
    if args.yes or _ask("\nApply to settings.json? (y/n): ", "y").lower().startswith("y"):
        _backup(SETTINGS)
        SETTINGS.write_text(after, encoding="utf-8")
        print("Applied.")
        if undo_dirty or undo_consumed:
            _save_undo(undo)
    else:
        print("Skipped settings.json.")
        if undo_dirty:
            _save_undo(undo_captured)

hooks_changed = False
if paths.IS_PLUGIN:
    print(f"\n{BOLD}--- hooks/hooks.json ---{RESET}")
    _old_hooks = (_read_json_or(HOOKS_JSON) or {}).get("hooks") or {}
    _written = write_hooks_json(src_hooks)
    # Only a change a running session would miss asks for a restart: the
    # entries of `hooks_restart: false` features are left out of the comparison.
    hooks_changed = _written and _restart_hooks(_old_hooks) != _restart_hooks(src_hooks)
    print("Written." if _written else "Up to date.")

print(f"\n{BOLD}--- CLAUDE.md ---{RESET}")
# Leaf first, so the hub's import never dangles — a missing target sits in
# context as literal text.
for _line in _seeded:
    print(_line)
print(f"{LEAF_NAME}: " + ("written." if _write_leaf(src_md) else "up to date."))
dst_text = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else ""
try:
    merged_md, n_user = _merge_hub(dst_text, bool(src_md))
except ValueError as e:
    print(f"{RED}CLAUDE.md: {e} — fix the markers manually; skipping.{RESET}")
else:
    if merged_md == dst_text:
        print("CLAUDE.md: up to date (only the leaf changes hands).")
        _report_user_rules(n_user)
    else:
        _report_user_rules(n_user)
        _print_diff(dst_text.splitlines(), merged_md.splitlines(),
                    "CLAUDE.md", "CLAUDE.md (merged)")
        if args.yes or _ask("\nApply merge to CLAUDE.md? (y/n): ", "y").lower().startswith("y"):
            _backup(CLAUDE_MD)
            CLAUDE_MD.write_text(merged_md, encoding="utf-8")
            print("Applied (CLAUDE.md now imports the generated leaf; user content preserved).")
        else:
            print("Skipped CLAUDE.md.")

keychain_authorize(args.yes)

# The last apply's outcome, for --update-plan: a read-only mode cannot recompute
# hooks_changed without re-running assemble(). Single writer: this apply path.
# restart_needed in the plan therefore means "as of the last apply" — accurate
# in the pull.sh flow, where the plan prints right after the apply that set it.
try:
    _last_apply = pathlib.Path(paths.log("last_apply.json"))
    _last_apply.parent.mkdir(parents=True, exist_ok=True)
    _last_apply.write_text(json.dumps({
        "ts": int(time.time()),
        "hooks_changed": hooks_changed,
        "argv": sys.argv[1:],
        "run_log": paths.tilde(RUN_LOG, "~") if RUN_LOG else None,
    }, indent=2), encoding="utf-8")
except OSError:
    pass  # a record, never a reason for an apply to fail

# Hooks and skills are read once, at session start. Everything else — the status
# line, permissions, env, the CLAUDE.md leaf — is picked up live, so only say
# this when it is actually true.
if hooks_changed:
    print(f"\n{BOLD}Restart Claude Code{RESET} for the hook changes to take effect "
          "(hooks are read at session start):")
    print("  type `quit` (or /exit), then `claude --continue` — the conversation survives.")

if args.update:
    # Post-install epilogue: what state the machine is in and how to change or
    # undo it.
    feats = prefs.features()
    n_on = sum(1 for f in feats if prefs.enabled(f))
    n_off = sum(1 for f in feats if prefs.answered(f) and not prefs.enabled(f))
    still = prefs.unanswered()
    print(f"\n{BOLD}--- State after update ---{RESET}")
    print(f"  {n_on} feature(s) on, {n_off} answered off"
          + (f", {len(still)} still unanswered" if still else "") + ".")
    print("  Change anything: /claudette prefs.  Every apply keeps a .backup of")
    print("  settings.json and CLAUDE.md; undo records let a feature switched off")
    print("  restore what was there before. Full revert (answers kept):")
    print("  /claudette uninstall, or " + paths.cmd("features_apply.py", "--uninstall"))

if args.uninstall:
    root = paths.tilde(paths.ROOT, "~")
    print(f"\n{BOLD}Uninstalled.{RESET} Settings restored from the undo records, hooks removed,")
    print("generated rules leaf emptied (CLAUDE.md itself untouched). Answers kept, so reinstalling is:")
    print("  " + paths.cmd("features_apply.py", "--resume", "all", "-y"))
    print("\nStill on disk (deleting them is irreversible — do it yourself if you")
    print("mean it; prefs.local.json and logs/ hold every answer and record):")
    print(f"  {root}    (the clone, including prefs.local.json and logs/)")
    for _st in user_copy.states(effective_only=False):
        if _st["state"] != "missing":
            print(f"  {_st['path']}    (your copy for {_st['feature']} — no longer imported)")
    print("If claudette is registered as a Claude Code plugin, remove it there too (/plugin).")
elif prefs.unanswered():
    new = prefs.unanswered()
    print(f"\n{RED}{len(new)} feature(s) are off because nobody has decided about them yet:{RESET} "
          + ", ".join(new))
    print("  /claudette update   (or: " + paths.cmd("features_apply.py", "--update") + ")")

# Last line, so it survives a caller that keeps only the tail.
if RUN_LOG:
    print(f"{GRAY}Run log: {paths.tilde(RUN_LOG, '~')}{RESET}")
