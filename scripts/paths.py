#!/usr/bin/env python3
"""Where claudette's own files live — the ONLY place that knows the layout.

Two layouts, one file, no edit between them:
    ~/.claude/claudette/<script>.py               flat
    ~/.claude/skills/claudette/scripts/<s>.py     plugin (repo root = plugin root)

ROOT owns the data (`logs/`, `features_def.json`, `prefs.local.json`); SCRIPTS
owns the code. No other claudette file may hardcode either.

`cmd()` and `tilde()` exist for the same reason in the other direction: paths
that end up inside *text* (hook command strings, prompts injected into a
session, doctor remedies) must be $HOME-relative bash, not this process's
absolute Windows path.
"""
import os

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS) if os.path.basename(SCRIPTS) == "scripts" else SCRIPTS
# Plugin mode: hooks are owned by the plugin's own hooks/hooks.json, not by the
# active settings.json. Detected from the manifest, never from the path.
PLUGIN_JSON = os.path.join(ROOT, ".claude-plugin", "plugin.json")
IS_PLUGIN = os.path.isfile(PLUGIN_JSON)


def version():
    """claudette's version, from the plugin manifest; None when it cannot be read."""
    try:
        import json
        with open(PLUGIN_JSON, encoding="utf-8") as fh:
            v = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
LOGS = os.path.join(ROOT, "logs")


def log(*parts):
    """Path inside logs/ (the folder is created by whoever writes)."""
    return os.path.join(LOGS, *parts)


# Log names follow <family>_<member>, where the member says what the data IS,
# not the mechanism that produces it (CLAUDETTE_LOGS.md).
USAGE_LAST = log("claude_usage_last.json")
USAGE_SNAPSHOTS = log("claude_usage_snapshots")
USAGE_SESSIONS = log("sessions")
LIMITS_LAST = log("claude_limits_last.json")
LIMITS_REQUEST = log("claude_limits_request.json")
# auto_done's compaction gate: what it decided, and whether Claude Code was seen
# compacting an idle session on this machine (CLAUDETTE_LOGS.md).
COMPACT_DONE_DECISIONS = log("compact_done_decisions.jsonl")
IDLE_COMPACTION_SEEN = log("idle_compaction_seen.json")
# auto_done's idle timer: per-session state (the newest timer's token, the
# session-ended mark, the off switch handed across /clear) and what each timer did.
IDLE_TIMER_DIR = log("idle_timer")
IDLE_TIMER_DECISIONS = log("idle_timer_decisions.jsonl")

# The only log of claudette ACTING rather than observing: one file per run that
# changed durable state — git traffic on the repos, config written to this
# machine, or both. Every other log here is a reading (usage, limits, upstream
# staleness); the hooks run constantly and never land here.
CONFIG_RUNS = log("config_runs")
RUN_LOG_KEEP = 20        # newest N kept; pruned on creation
RUN_LOG_ENV = "CLAUDETTE_RUN_LOG"


def new_run_log(keep=RUN_LOG_KEEP):
    """Create an empty run log and prune the folder to the newest `keep`.

    Name is `<YYYYMMDD-HHMMSS>_<pid>.log` — sortable, so pruning is a plain
    name sort, and readable in an `ls`, which is the whole point of a log a
    human opens. (The machine-read logs use epoch ms; this one does not.)
    Local imports keep the module's cost where it was — `paths` is imported by
    every status-line render."""
    import glob
    import time
    os.makedirs(CONFIG_RUNS, exist_ok=True)
    path = os.path.join(CONFIG_RUNS,
                        "%s_%d.log" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    open(path, "a", encoding="utf-8").close()
    if keep:
        for old in sorted(glob.glob(os.path.join(CONFIG_RUNS, "*.log")))[:-keep]:
            try:
                os.unlink(old)
            except OSError:
                pass
    return path


def run_log():
    """This run's log: the one a parent already opened (pull.sh, log_run.sh —
    handed down through the environment so a pull and the apply it triggered
    stay ONE file), else a new one."""
    return os.environ.get(RUN_LOG_ENV) or new_run_log()


def newest_run_log():
    """Path of the most recent run log, or None. Readers glob; there is no
    `_last` copy — nothing reads these on a hot path, so a fixed path would buy
    a duplicate file and a second write to keep consistent."""
    import glob
    found = sorted(glob.glob(os.path.join(CONFIG_RUNS, "*.log")))
    return found[-1] if found else None


def tilde(path, home_var="$HOME"):
    """Absolute path as forward-slashed bash with $HOME (or ~) for the home dir.

    Windows paths must survive being pasted into a hook command or a prompt;
    bash is the universal substrate on every platform (CLAUDETTE_DEV.md
    "Windows vs Mac"), so the text form is always POSIX-ish.
    """
    p = os.path.abspath(path).replace("\\", "/")
    h = HOME.replace("\\", "/")
    if p.lower().startswith(h.lower()):
        return home_var + p[len(h):]
    return p


def cmd(script, *args):
    """Canonical invocation string for a claudette script in docs, fix: hints
    and instructions to Claude — the run.sh dispatcher form, whose single
    literal prefix is what the claude_allow_claudette_scripts grant matches
    (CLAUDETTE_DESIGN.md "Permission / allow-rule principles"). Hook commands
    and the statusline never come through run.sh: they carry the absolute
    interpreter path resolved for this machine ({PY} in the registry)."""
    parts = ['bash "%s" %s' % (tilde(os.path.join(SCRIPTS, "run.sh")), script)]
    parts.extend(str(a) for a in args)
    return " ".join(parts)
