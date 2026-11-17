#!/usr/bin/env python3
"""Claudette auto_done compaction hooks - the done report runs before Claude Code
compacts an idle session, and survives that compaction.

PART OF auto_done: the `gate` half, beside the cron watchdog (hook_watchdog.py).
auto_done.py decides whether this session may be held back: mode
(`auto_done_watcher`), the session's prompt-cache TTL, the `.autodone` switches.
Only the BLOCK depends on that decision. `keep` and `restore` carry a report that
already exists through the compaction, cost nothing, and run whenever the feature
is on. The decision log, the wake marker and the restore marker keep their
compact_done names, so markers already written in transcripts and logs still match.

WHY. Claude Code compacts a long session on its own while it sits idle (about 54 min
after the last request, 1h prompt cache, 200k tokens or more; CLAUDETTE_DESIGN.md,
"compact_done: the done report before an idle compaction"). Whatever the summary leaves out is gone from the agent's working
context. The done report is the one place that says what this session never saved,
and it is cheapest and most complete on the full, still-cached conversation - so it
should run right before that compaction, in exactly the sessions that get one, and
nowhere else. The idle watchdog (hook_watchdog.py) reaches the same moment by arming
a cron in every session; this reaches it from the compaction itself.

THREE MODES, ONE SCRIPT. One hooks.json entry per event, so one fingerprint each:
  gate     PreCompact, matcher `auto`, synchronous. Decides block / keep / pass.
           block: exit 2, Claude Code skips the compaction ("continuing uncompacted").
           keep:  exit 0 with the last done report on stdout, which Claude Code
                  appends to the compaction instructions, so the summary carries it.
           pass:  exit 0, nothing printed; the compaction runs as if we were absent.
  wake     PreCompact, matcher `auto`, `asyncRewake`. Runs in the background; exit 2
           wakes the model with its stderr. It wakes only when the gate blocked,
           read from the gate's decision log line - two processes deciding alone
           could disagree, and a wake without a block would run the report on an
           already compacted context.
  restore  SessionStart, matcher `compact`. After a compaction the gate let through
           with a report, re-injects that report verbatim (stdout reaches the
           model), with the transcript path for the details. The keep instructions
           ask the summarizer; this does not depend on it obeying.

ONE BLOCK PER PROMPT, AND STATE LIVES IN THE TRANSCRIPT. The wake lands in the
transcript as a user row carrying WAKE_MARKER (origin task-notification). After it,
the next idle compaction finds either a report since the user's last prompt (keep)
or the wake alone (pass: the turn ran and produced none). So a session is held back
at most once per prompt the user typed, and a failure anywhere degrades to the
compaction Claude Code would have run anyway. No ledger is needed for that, the
same choice hook_watchdog.py made.

THE GATE NEVER BLOCKS A SESSION AT WORK. PreCompact `auto` is also the compaction
that fires because the context is full, mid-turn; blocking that one would stop the
session. Two independent guards, either one sufficient:
  - idle: no user or assistant row in the last IDLE_MIN minutes, by row timestamp
    (never file mtime: Claude Code appends bookkeeping rows to an idle transcript);
  - size: the context under CTX_MAX % of the window, from the status line's
    per-session snapshot. Idle compaction starts at 200k tokens; a full-context one
    fires near the window's end.
And it passes for a subagent, scheduled work (a self-pacing loop or a cron the user
created; the done report in the middle of it is noise), a 5h window at its throttle
cap, a missing transcript, or the feature switched off.

REPORT ONLY. The woken turn may not commit, push or write anything but the usage
chart, exactly the watchdog's contract: the user is away.

CLI (tests and manual checks also pass these; the hook registry does not):
    hook_compact_done.py gate|wake|restore [--idle-min N] [--any-trigger] [--wake-file F]
--any-trigger lets a manual /compact stand in for the idle one; --wake-file replaces
the wake's instructions (after the marker) with the file's text.
"""
import calendar
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

FEATURE = "auto_done"
WAKE_MARKER = "[claudette compact-done]"
RESTORE_MARKER = "[claudette compact-done: report kept]"
WATCHDOG_MARKER = "[claudette auto-done watchdog]"
# The idle timer's wake (hook_idle_timer.py TIMER_MARKER), repeated so this hook does not
# import that module: a timer wake since the last prompt counts as this gate's own.
TIMER_MARKER = "[claudette idle-timer]"
IDLE_MIN = 45            # below this the compaction is the session at work
CTX_MAX = 80              # % of the window: at or above, a full-context compaction
RESTORE_WINDOW_S = 15 * 60  # a `keep` older than this was another compaction
WAKE_WAIT_S = 8           # how long the wake waits for the gate's decision line
RECURRING_MAX_S = 7 * 86400  # Claude Code expires recurring crons after 7 days
REPORT_MAX = 6000         # chars of report carried into instructions / context
LOG_CAP_BYTES = 256 * 1024
LOG_KEEP = 400

# A done report: its fixed title at a line start, inside the report's diff fence.
# A conversation ABOUT the report quotes the title too, so the fence is required.
FENCE_RE = re.compile(r"```diff\n.*?^DONE PERSISTENCE STATUS\b.*?(?:^```|\Z)", re.S | re.M)
SYS_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S | re.I)
# User rows that are the client talking, not the user: slash-command records,
# their output, background-task notifications.
NOT_PROMPT = ("<command-name>", "<command-message>", "<local-command", "<task-notification>")
NOT_PROMPT_KINDS = ("task-notification",)
SCHED_TOOLS = ("CronCreate", "ScheduleWakeup")


def _epoch(ts):
    """Epoch seconds from a transcript ISO-8601 UTC timestamp; None if unparseable."""
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_prompt(row, content, txt):
    """A message that starts new work - the only thing that starts a new 'since'.
    Typed prompts carry origin kind `human` (none in older clients and headless
    runs); another session's message is `peer`, new work too. Only background
    notifications are excluded, by name: a kind not seen yet counts, since the
    cost of that miss is one more report, where the other way keeps a stale one."""
    if row.get("isMeta") or row.get("isCompactSummary") or row.get("isSidechain"):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict) and origin.get("kind") in NOT_PROMPT_KINDS:
        return False
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                         for b in content):
        return False
    s = SYS_REMINDER.sub(" ", txt).strip()
    if not s or s.startswith(NOT_PROMPT) or WATCHDOG_MARKER in s:
        return False
    return True


def scan(path):
    """What the transcript says, in file order (the order rows were appended):
    last_activity  newest user/assistant timestamp (max, not last: rows can be
                   written out of order)
    prompt_ln      line of the user's last typed prompt (-1: none)
    wake_ln        line of the last row carrying WAKE_MARKER
    timer_ln       line of the last row carrying TIMER_MARKER (the idle timer's wake)
    report_ln, report   the last done report an assistant wrote
    sched          [(line, epoch, tool, input)] scheduling calls not the watchdog's
    None when the file cannot be read."""
    st = {"last_activity": None, "prompt_ln": -1, "wake_ln": -1, "timer_ln": -1,
          "report_ln": -1, "report": None, "sched": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                if ('"type":"user"' not in line and '"type":"assistant"' not in line
                        and '"type": "user"' not in line and '"type": "assistant"' not in line
                        and "queue-operation" not in line):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get("type")
                # A prompt is queued (`enqueue`) the moment it is submitted, before
                # its user row exists: a compaction forced on that submit must see
                # the user back, even when the status line's size reading is gone.
                queued = kind == "queue-operation" and row.get("operation") == "enqueue"
                if kind not in ("user", "assistant") and not queued:
                    continue
                ts = _epoch(row.get("timestamp"))
                if ts is not None and (st["last_activity"] is None or ts > st["last_activity"]):
                    st["last_activity"] = ts
                if queued:
                    continue
                msg = row.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if kind == "assistant":
                    if row.get("isSidechain") or not isinstance(content, list):
                        continue
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and FENCE_RE.search(b.get("text") or ""):
                            st["report_ln"], st["report"] = ln, b["text"]
                        elif b.get("type") == "tool_use" and b.get("name") in SCHED_TOOLS:
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            if WATCHDOG_MARKER not in json.dumps(inp):
                                st["sched"].append((ln, ts, b.get("name"), inp))
                    continue
                if row.get("isSidechain"):
                    continue
                txt = _text(content)
                if WAKE_MARKER in txt:
                    st["wake_ln"] = ln
                elif TIMER_MARKER in txt:
                    st["timer_ln"] = ln
                elif _is_prompt(row, content, txt):
                    st["prompt_ln"] = ln
    except (IOError, OSError):
        return None
    return st


def scheduled(st, now):
    """Is the user's own scheduled work running in this session? A self-pacing loop
    schedules its next wake every turn, so one since the last prompt means a loop;
    a recurring cron lives up to 7 days wherever it was created. Deleted jobs are
    not tracked: a false positive only means no report, the safe side."""
    for ln, ts, tool, inp in st["sched"]:
        if tool == "ScheduleWakeup":
            if not inp.get("stop") and ln > st["prompt_ln"]:
                return True
        elif inp.get("recurring", True) is not False:
            if ts is None or now - ts < RECURRING_MAX_S:
                return True
        elif ln > st["prompt_ln"]:
            return True
    return False


def ctx_pct(session_id):
    """The context fill the status line last rendered for this session, or None."""
    try:
        with open(os.path.join(paths.USAGE_SESSIONS, session_id + ".json"),
                  encoding="utf-8") as fh:
            v = json.load(fh).get("ctx_pct")
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except (IOError, OSError, ValueError, AttributeError):
        return None


def capped(now):
    """5h window at or past the throttle curve's cap - the watchdog's step 3. A
    reading whose window has already reset says nothing about the new one."""
    try:
        with open(paths.USAGE_LAST, encoding="utf-8") as fh:
            snap = json.load(fh)
        w = (snap.get("rate_limits") or {}).get("five_hour") or {}
        used, cap = w.get("used_percentage"), (snap.get("caps") or {}).get("five_hour")
        resets = w.get("resets_at")
        if not isinstance(used, (int, float)) or not isinstance(cap, (int, float)):
            return False
        if isinstance(resets, (int, float)) and resets <= now:
            return False
        return used >= cap
    except (IOError, OSError, ValueError, AttributeError):
        return False


def enabled():
    try:
        import prefs
        return prefs.effective(FEATURE)
    except Exception:
        return False


def decide(inp, st, now, ctx, is_capped, on, idle_min=IDLE_MIN, any_trigger=False,
           may_block=True, block_why=""):
    """(decision, reason, idle_minutes) - pure, so gate and tests share it.
    `may_block` is auto_done's verdict for this session (mode, cache TTL, switches);
    when False, a would-be block passes with `block_why` as its reason."""
    if not on:
        return "pass", "feature off", None
    trig = inp.get("trigger")
    if trig != "auto" and not any_trigger:
        return "pass", "trigger %s" % trig, None
    if inp.get("agent_id") or inp.get("agent_type"):
        return "pass", "subagent", None
    if st is None:
        return "pass", "no transcript", None
    if st["last_activity"] is None:
        return "pass", "no activity found", None
    idle = int((now - st["last_activity"]) // 60)
    if st["report_ln"] > st["prompt_ln"]:
        return "keep", "report since the last prompt", idle
    if idle < idle_min:
        return "pass", "active (idle %d min)" % idle, idle
    if ctx is not None and ctx >= CTX_MAX:
        return "pass", "context %s%% (full-context compaction)" % ctx, idle
    # Either wake: the idle timer may have woken this session minutes before the
    # compaction, and a second wake for the same prompt would be a second report.
    if max(st["wake_ln"], st.get("timer_ln", -1)) > st["prompt_ln"]:
        return "pass", "already woken since the last prompt, no report", idle
    if scheduled(st, now):
        return "pass", "scheduled work", idle
    if is_capped:
        return "pass", "5h window at its cap", idle
    if not may_block:
        return "pass", block_why or "auto_done does not block here", idle
    return "block", "idle %d min, no report yet" % idle, idle


def report_block(text):
    """The report's own diff fence when it has one, else the whole message; capped."""
    m = FENCE_RE.search(text or "")
    out = (m.group(0) if m else (text or "")).strip()
    if len(out) > REPORT_MAX:
        out = out[:REPORT_MAX].rstrip() + "\n[... cut at %d chars]" % REPORT_MAX
    return out


def keep_instructions(report):
    return ("claudette auto_done: the done report below was written on the full "
            "conversation just before this compaction. Copy it verbatim into the summary "
            "under a heading \"Done report before compaction\", and keep in full the details "
            "behind every line marked \"!\" (what is not saved anywhere yet), even where the "
            "rest of the summary is condensed.\n\n" + report_block(report))


def wake_text(idle, ctx, override=None):
    if override:  # the live check's stand-in for the done skill (--wake-file)
        return WAKE_MARKER + " " + override
    try:
        import hook_watchdog
        chart = hook_watchdog._chart_step(2)
    except Exception:
        chart = ""
    return (WAKE_MARKER + " Claude Code was about to compact this session, idle for %s "
            "min%s. claudette held that compaction back once, so the done report runs on "
            "the full conversation first; the next compaction keeps the report. This is the "
            "auto_done feature the user switched on, not a request they typed now.\n"
            "1. Invoke the claudette:done skill with no argument and present its report.\n"
            "%s"
            "REPORT ONLY: do not commit, stage, push, write a memory file, or change "
            "anything else - the user is away and will act on the report when they return."
            % (idle if idle is not None else "?",
               "" if ctx is None else ", context %s%%" % ctx, chart))


def restore_text(path, report):
    return ("%s This session was just compacted while idle. Right before, claudette ran "
            "the done report on the full conversation; here it is, verbatim. Lines marked "
            "\"!\" name what is not saved anywhere yet; their details are in the transcript "
            "from before the compaction: %s\n\n%s"
            % (RESTORE_MARKER, str(path).replace("\\", "/"), report_block(report)))


# --- decision log (single writer: the gate) ------------------------------------

def signature(st):
    """What the gate's decision rests on, as one string. The wake recomputes it from
    the same transcript and follows only a gate line carrying the same one: a time
    window alone lets the wake of a SECOND compaction read the FIRST one's `block`
    line before the gate has logged its `keep`, and wake a compacted session.
    Bookkeeping rows appended between the two reads change no field here; a woken
    turn changes three."""
    if not st:
        return ""
    return "%d:%d:%d:%s" % (st["prompt_ln"], st["wake_ln"], st["report_ln"],
                            st["last_activity"])


def log_decision(entry):
    p = paths.COMPACT_DONE_DECISIONS
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if os.path.getsize(p) > LOG_CAP_BYTES:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-LOG_KEEP:]
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, p)
    except (IOError, OSError):
        pass


def last_decision(session_id, since, sig=None):
    """The gate's newest line for this session at or after `since` (and carrying
    `sig`, when given), or None."""
    try:
        with open(paths.COMPACT_DONE_DECISIONS, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-200:]
    except (IOError, OSError):
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("session_id") != session_id:
            continue
        if (e.get("ts") or 0) < since:
            return None
        if sig is None or e.get("sig") == sig:
            return e
    return None


# --- modes -----------------------------------------------------------------------

def _transcript(inp):
    p = inp.get("transcript_path")
    if p and os.path.isfile(p):
        return p
    try:
        import hook_watchdog
        return hook_watchdog.find_transcript(inp)
    except Exception:
        return None


def may_block_here(sid, path):
    """(allowed, reason): the `.autodone` switches first, then auto_done.py. A
    failure to decide never blocks: the compaction runs as if we were absent."""
    try:
        import hook_watchdog
        off, where = hook_watchdog._switch(sid)
        if off:
            return False, "switched off %s (.autodone)" % where
    except Exception:
        pass
    try:
        import auto_done
        return auto_done.allows("gate", path)
    except Exception as e:
        return False, "auto_done undecidable: %s" % str(e)[:80]


def _context(inp, opts):
    now = time.time()
    path = _transcript(inp)
    st = scan(path) if path else None
    sid = str(inp.get("session_id") or "")
    ctx = ctx_pct(sid) if sid else None
    may_block, why = may_block_here(sid, path)
    decision, reason, idle = decide(inp, st, now, ctx, capped(now), enabled(),
                                    idle_min=opts["idle_min"], any_trigger=opts["any_trigger"],
                                    may_block=may_block, block_why=why)
    return now, path, st, sid, ctx, decision, reason, idle


def gate(inp, opts):
    now, path, st, sid, ctx, decision, reason, idle = _context(inp, opts)
    log_decision({"ts": int(now), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "session_id": sid, "trigger": inp.get("trigger"), "decision": decision,
                  "reason": reason, "idle_min": idle, "ctx_pct": ctx, "sig": signature(st)})
    if inp.get("trigger") == "auto" and idle is not None and idle >= IDLE_MIN:
        try:  # an auto compaction of an idle session IS an idle compaction
            import idle_compaction
            idle_compaction.note_seen(now)
        except Exception:
            pass
    if decision == "block":
        sys.stderr.write("claudette auto_done: compaction held back once, so the done "
                         "report can run on the full conversation first\n")
        return 2
    if decision == "keep":
        sys.stdout.write(keep_instructions(st["report"]))
    return 0


def wake(inp, opts):
    start = time.time()
    sid = str(inp.get("session_id") or "")
    path = _transcript(inp)
    st = scan(path) if path else None
    if not sid or not st:
        return 0
    # The gate decides; this only follows - the line it logged for THIS compaction,
    # recognised by the transcript state both read (signature()).
    sig, entry = signature(st), None
    while time.time() - start < WAKE_WAIT_S:
        entry = last_decision(sid, start - 120, sig)
        if entry:
            break
        time.sleep(0.25)
    if not entry or entry.get("decision") != "block":
        return 0
    override = None
    if opts.get("wake_file"):
        with open(opts["wake_file"], encoding="utf-8") as fh:
            override = fh.read().strip()
    sys.stderr.write(wake_text(entry.get("idle_min"), entry.get("ctx_pct"), override))
    return 2


def restore(inp, opts):
    if inp.get("source") not in (None, "compact"):
        return 0
    sid = str(inp.get("session_id") or "")
    if not sid or not enabled():
        return 0
    entry = last_decision(sid, time.time() - RESTORE_WINDOW_S)
    if not entry or entry.get("decision") != "keep":
        return 0
    path = _transcript(inp)
    st = scan(path) if path else None
    if not st or not st["report"]:
        return 0
    sys.stdout.write(restore_text(path, st["report"]))
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    opts = {"idle_min": IDLE_MIN, "any_trigger": "--any-trigger" in argv, "wake_file": None}
    if "--idle-min" in argv:
        try:
            opts["idle_min"] = int(argv[argv.index("--idle-min") + 1])
        except (IndexError, ValueError):
            pass
    if "--wake-file" in argv:
        try:
            opts["wake_file"] = argv[argv.index("--wake-file") + 1]
        except IndexError:
            pass
    try:
        inp = json.load(sys.stdin)
    except (ValueError, IOError):
        return 0
    if not isinstance(inp, dict):
        return 0
    fn = {"gate": gate, "wake": wake, "restore": restore}.get(mode)
    if fn is None:
        return 0
    try:
        return fn(inp, opts)
    except Exception as e:  # never break a compaction or a session start
        try:
            sys.stderr.write("claudette auto_done %s: %s\n" % (mode, str(e)[:200]))
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
