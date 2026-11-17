#!/usr/bin/env python3
"""Claudette UserPromptSubmit hook - arms an idle auto-done watchdog for this session.

WHY. A `/claudette:done` run on a cold prompt cache costs several times what it costs
warm. One one-shot cron fires ~50 min after the last prompt, inside the 1h cache TTL,
and leaves the report waiting.

WHAT THIS HOOK DOES, AND WHAT IT CANNOT. A hook is a shell process: it cannot call
CronCreate, which is an agent tool. So this hook never arms anything itself - it
decides, and when the answer is yes it injects one line of `additionalContext`
telling the agent to make exactly one CronCreate call. The hook computes the cron
string, so the agent has no decision left to make. Silent path is `sys.exit(0)`:
zero tokens on every prompt it declines.

The fired prompt is plain text, not `/claudette:done`, so `SKILL.md` loads only if
the watchdog actually decides to run.

THE STAND-DOWN CHECK IS EXECUTABLE, NOT PROSE. The fired run stands down if the user
prompted within STAND_DOWN_MIN, but the agent's context carries no timestamps. The
fired prompt therefore bakes in `hook_watchdog.py --idle <transcript>`, which prints
minutes since the last REAL user message - watchdog fires excluded, so transcript
mtime cannot work: the fire itself touches the file. The check runs FIRST: standing
down is the common outcome in an active session, and one Bash call is cheaper than
CronList.

STATE LIVES IN THE TRANSCRIPT, NOT IN A LOG. Cron jobs are in-session memory and
invisible from here, and a ledger under logs/ would need two writers (this hook arms,
the agent fires). A CronCreate tool call carrying MARKER means armed; a user message
carrying MARKER means it already fired. armed = last arm is newer than last fire,
compared by FILE POSITION (see read_state).

Blind spot: `/clear` mints a new session id and a new transcript, while the cron store
lives in the REPL process and outlives it. A cleared window reads as never-armed while
its earlier jobs are still pending, so the injected instruction has the agent call
CronList first and create nothing when a MARKER job is already pending. Residual:
CronList truncates the prompt, so a pending job cannot be told apart from a pre-`/clear`
one - a stale job carries the OLD transcript path, so its stand-down check cannot see
that the user is still active.

ANTI-GOALS:
  - never arm in a session running scheduled work: the FIRED prompt checks CronList,
    because only the agent can see its own jobs
  - the fired run is REPORT-ONLY. It must never commit, write a memory file, or
    change anything unattended - the user acts when they return.
  - never arm a `-p`/headless run (guarded by MIN_TURNS: a one-shot never gets there)
  - never re-arm on every prompt: that adds a round trip per turn, costing far more
    than the cold cache it saves. A FIRED run that stands down is the one exception:
    it re-arms itself from `rearm_cron`, and only there: reaching step 4 means it
    has reported.

A MANUAL /done AFTER ARMING STANDS THE JOB DOWN. The pending job knows nothing about
what the user did after it was armed. `--idle` applies the same QUIET_TURNS rule the
arm path uses, and read_state treats slot replies to the report (`1`, `c`, `p`, `all`,
`check 6`...) as the done dialogue continuing, not as fresh turns.

THE CACHE DECIDES WHETHER TO ARM. This hook is the `cron` half of the auto_done
feature; auto_done.py says whether the session may have it. In the default mode that
takes a 1-hour prompt cache, read from the transcript: on a 5-minute cache a fire 50
min out runs cold, so nothing is armed, and a job armed while the cache was 1 hour
stands down at fire time if it has dropped since (`--idle`).

NOT IN MODE `auto`. There the idle timer (hook_idle_timer.py) does this job from a
background Stop hook; auto_done refuses `cron`, so this hook arms nothing and a job
still pending stands down through `--idle`. Modes `cron` and `both` arm normally.

CONTROL: the ".autodone [on|off] [all]" dot-command (dot_autodone.py, dispatched by
hook_status_prompt.py) and the watchdog_switch.py CLI both write the same marker
files this hook reads (logs/watchdog_off, logs/watchdog/<id>.off).

CLI:
    hook_watchdog.py --idle <transcript.jsonl>   used by the fired prompt, see above
"""
import calendar, glob, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

HOME = paths.HOME
LOGS = paths.LOGS
WDIR = os.path.join(LOGS, "watchdog")
GLOBAL_OFF = os.path.join(LOGS, "watchdog_off")

MARKER = "[claudette auto-done watchdog]"
# compact_done's wake (hook_compact_done.py WAKE_MARKER), repeated here so this hook,
# which runs on every prompt, does not import that module.
COMPACT_DONE_MARKER = "[claudette compact-done]"
# The idle timer's wake (hook_idle_timer.py TIMER_MARKER), same reason.
TIMER_MARKER = "[claudette idle-timer]"
# An INVOCATION, never a mention: a session discussing claudette:done says the words in
# every turn. Four shapes: the typed command, the <command-name> row, the skill-expansion row
# ("# /claudette:done"), and the already-loaded stub ("Skill /claudette:done is already
# loaded above") - the last two are what a fired run's own step 4 leaves behind.
DONE_CALL = re.compile(r"(<command-name>\s*|^\s*|^#\s*|^Skill\s+)/(claudette:)?done\b",
                       re.I | re.M)
# A REPLY to the done report continues the done dialogue, it is not new work: `1`, `c p`,
# `123cu`, `all`, `check 6`, `R <path>`, `commit`, `go` - the argument grammar of
# skills/done/SKILL.md, whole message only, short.
_DONE_WORD = r"[1-9crpu]+|all|push|help|check|context|memory|scratch|commit|go|ok|yes|oui|y"
DONE_REPLY = re.compile(r"^\s*(?:%s)(?:[\s,]+(?:%s))*(?:\s+\S+)?\s*[.!]?\s*$"
                        % (_DONE_WORD, _DONE_WORD), re.I)
DONE_REPLY_MAX = 40  # chars; longer is prose, and prose is a new prompt
# Harness-injected, not the user. Unclosed alternative on purpose: a truncated block at
# end of row must strip too, or the whole tail reads as user text.
SYS_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S | re.I)
ARM_CALL = re.compile(r'"type"\s*:\s*"tool_use".{0,200}?"name"\s*:\s*"CronCreate"', re.S)
FIRE_MIN = 50        # arm this far out; must stay < 60 so the 1h cache is still warm
STAND_DOWN_MIN = 45  # at fire time, a newer prompt than this means the user is back
MIN_TURNS = 3        # never arm a young session; also excludes one-shot `-p` runs
QUIET_TURNS = 2      # do not re-arm within N turns of a claudette:done

def _chart_url():
    """The chart's address when this run should update it, else None: the feature on and
    an address cached. Only a gate: the chart runner reads the address itself. A read
    that fails must not break the arm: no url, no chart step."""
    try:
        import prefs
        if not prefs.effective("chart_autoupdate"):
            return None
        import usage_chart
        return (usage_chart._read_json(usage_chart.ADDR) or {}).get("url") or None
    except Exception:
        return None


def _chart_step(n=5):
    """The one write an away run makes, emitted only when the feature is on, as
    step `n` of the fired prompt (compact_done's wake reuses it as its own step).

    Known risks of an unattended write: a permission mode that prompts for an Artifact
    publish stalls this run (auto mode, the default, does not), and auto mode's
    classifier may refuse the publish, which denies rather than hangs. Either shows up
    as a missed update, never as lost readings: they wait in the snapshot folder for the
    next run.
    """
    url = _chart_url()
    if not url:
        return ""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode":
        # prepare() refuses to publish from this host (usage_chart.py, VSCODE_HOST), so
        # the step would promise a write that cannot happen. Drop it at arm time.
        return ""
    return ("%d. chart_autoupdate is ON for this machine, and the usage chart update is the "
            "ONE write this run makes: claudette:done's recipe hands it to a background "
            "session (`chart_runner.py launch`, riding the probe's message); put the line it "
            "prints in the report. Never create or discover a chart.\n" % n)


def fired_prompt(transcript):
    # Both paths are injected text read by a future session: they come from paths.*.
    chart = _chart_step()
    return (
        MARKER + " fires once when idle ~%dmin: report-only, no writes%s. "
        "Run these checks IN ORDER, in one turn, and stop at the first that applies.\n"
        "1. Run in Bash: %s --idle \"%s\"\n"
        "   If that JSON carries a 'stand_down' field (switch flipped, or the user ran "
        "/done themselves after this job was armed): follow that field verbatim and stop.\n"
        "   If idle_minutes < %d, the user is back: call CronList; if it shows NO job "
        "carrying the marker above, CronCreate one one-shot job with cron=<the rearm_cron "
        "field of that JSON>, recurring=false, and THIS ENTIRE PROMPT verbatim as its "
        "prompt. If it already shows one, create NOTHING - the arm-time hook armed this "
        "session when this prompt was submitted, and a second job means two reports. "
        "Either way reply exactly 'auto-done: session still active, re-armed' and stop. "
        "That re-arm is the whole point - the arm-time hook only runs when the user "
        "submits a prompt, so a session abandoned right after a stand-down would never be "
        "watched again. A null idle_minutes means unknown: continue.\n"
        "2. Call CronList. Ignore every job whose prompt carries the marker above: those "
        "are this feature's own bookkeeping, and one of them was created for THIS session "
        "by the arm-time hook the moment this prompt was submitted. Counting them made "
        "every run stand down and never reach step 4. If a job remains that is NOT a "
        "watchdog job, it is real user work: reply exactly 'auto-done: scheduled work "
        "active, standing down' and stop.\n"
        "3. Read %s. If "
        "rate_limits.five_hour.used_percentage >= caps.five_hour, reply exactly "
        "'auto-done skipped (5h cap NN%%)' with the real number, and stop.\n"
        "4. Otherwise invoke the claudette:done skill and present its report.\n"
        "%s"
        "REPORT ONLY: do not commit, stage, push, write a memory file, or change "
        "anything else - the user is away and will act on the report when they return. "
        "Re-arm ONLY in the stand-down case of step 1; a run that reaches step 4 has "
        "reported and is done."
    ) % (FIRE_MIN, " but the usage chart (step 5)" if chart else "",
         paths.cmd("hook_watchdog.py"), transcript.replace("\\", "/"),
         STAND_DOWN_MIN, paths.tilde(paths.USAGE_LAST, "~"), chart)


def _switch(session):
    """(off_here, reason) - global switch wins over the per-session one."""
    if os.path.exists(GLOBAL_OFF):
        return True, "globally"
    if session and os.path.exists(os.path.join(WDIR, session + ".off")):
        return True, "for this session"
    return False, ""


def find_transcript(data):
    p = data.get("transcript_path")
    if p and os.path.isfile(p):
        return p
    sid = data.get("session_id")
    if not sid:
        return None
    hits = glob.glob(os.path.join(HOME, ".claude", "projects", "*", "%s.jsonl" % sid))
    return hits[0] if hits else None


def _epoch(ts):
    """Epoch seconds from a transcript ISO-8601 UTC timestamp; None if unparseable."""
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def read_state(path):
    """(user_turns, armed, turns_since_done, last_user_epoch). Substring pre-filter
    before json.loads: this runs on EVERY prompt and a transcript can be megabytes."""
    turns, last_arm, last_fire, last_done, i, ln, last_ts = 0, -1, -1, -1, 0, 0, None
    in_done = False  # inside a /done dialogue: the call, its skill expansion, slot replies
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Arm and fire are compared by FILE POSITION (ln), never by turn index
                # (i): the arm that ANSWERS a fire lands in the same turn as the fire.
                # since_done stays on i - QUIET_TURNS counts turns, not lines.
                ln += 1
                # A tool_use BLOCK, not the word: this file quotes both MARKER and
                # CronCreate, so any session that reads or writes it would otherwise
                # look permanently armed.
                if MARKER in line and ARM_CALL.search(line):
                    last_arm = ln
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user" or row.get("isSidechain"):
                    continue
                c = (row.get("message") or {}).get("content")
                if isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue
                    txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                else:
                    txt = c if isinstance(c, str) else ""
                # The harness appends <system-reminder> blocks to user rows and sometimes
                # sends a row that is nothing but one (a session-name notice); strip them
                # (reminder-only row, appended reminder with DONE_REPLY).
                # compact_done's wake is not the user talking either, nor a fire: the
                # arm state stays as it was. Read before the strip below, because the
                # wake's text arrives INSIDE a <system-reminder> block.
                if COMPACT_DONE_MARKER in txt or TIMER_MARKER in txt:
                    continue
                txt = SYS_REMINDER.sub(" ", txt).strip()
                if not txt:
                    continue
                if MARKER in txt:
                    last_fire = ln                # a watchdog fire is not the user talking:
                    continue                      # neither a turn nor distance from a /done
                i += 1
                turns += 1
                last_ts = _epoch(row.get("timestamp")) or last_ts
                # A slash command lands as TWO user rows (<command-name>, then the
                # skill expansion "# /claudette:done"); DONE_CALL matches both. A
                # slot reply extends the dialogue only while the chain from the
                # call is unbroken - a bare `1` after real prose is real prose.
                if DONE_CALL.search(txt):
                    last_done, in_done = i, True
                elif in_done and len(txt) <= DONE_REPLY_MAX and DONE_REPLY.match(txt):
                    last_done = i
                else:
                    in_done = False
    except IOError:
        return 0, False, 99, None
    return turns, last_arm > last_fire, (i - last_done) if last_done >= 0 else 99, last_ts


def idle_report(path):
    """--idle <transcript>: print minutes since the last real user message. Called by
    the FIRED prompt itself (see module docstring) - never by the arm-time hook.

    THE SWITCH IS CHECKED HERE TOO, not only at arm time. A fired job carries the
    prompt baked when it was armed and cron lives in the REPL process, so an off
    switch written after arming must reach jobs already pending; this CLI is the one
    piece of live code every fired run touches. Session id is the transcript
    BASENAME - the fired prompt carries no sid of its own."""
    off, why = _switch(os.path.splitext(os.path.basename(path))[0])
    if off:
        # No idle_minutes and no rearm_cron on purpose: an older baked prompt that
        # does not know the stand_down field falls through to "unknown: continue" and
        # would run a full report.
        print(json.dumps({"watchdog_off": why, "stand_down":
                          "The auto-done watchdog is switched off %s. Do NOT re-arm, "
                          "do NOT run claudette:done, check nothing else: reply exactly "
                          "'auto-done: watchdog off (%s), standing down' and stop."
                          % (why, why)}))
        return 0
    allowed, why = _cron_allowed(path)
    if not allowed:
        # Armed while the cache was 1 hour, fired after it dropped to 5 minutes (or
        # the mode changed): the report would run cold. No re-arm; the next prompt
        # re-decides through the arm path.
        print(json.dumps({"auto_done": why, "stand_down":
                          "The auto-done report does not run in this session now (%s). "
                          "Do NOT re-arm, do NOT run claudette:done, check nothing else: "
                          "reply exactly 'auto-done: %s, standing down' and stop."
                          % (why, why)}))
        return 0
    _, _, since_done, last_ts = read_state(path)
    if since_done < QUIET_TURNS:
        # The user ran /done themselves after this job was armed and has said nothing
        # substantive since: the report already exists (ARM -> manual DONE -> FIRE).
        # No re-arm: a later real prompt re-arms through the hook once QUIET_TURNS pass.
        # Also reaches the pre-/clear job whose stale transcript ends in a /done.
        print(json.dumps({"user_done": True, "stand_down":
                          "The user already ran claudette:done in this session and has not "
                          "worked since. Do NOT re-arm, do NOT run claudette:done, check "
                          "nothing else: reply exactly 'auto-done: /done already run, "
                          "standing down' and stop."}))
        return 0
    if last_ts is None:
        print(json.dumps({"idle_minutes": None, "note": "no user message found"}))
    else:
        idle = int(time.time() - last_ts) // 60
        # Stand-down is not the end of the watch. The arm-time hook only runs on a
        # user prompt, so a session that stands down and is then abandoned would
        # never be watched again. The cron to re-arm with ships here, anchored on THIS
        # FIRE, not on the last user message: a fire is a real API turn on the session
        # prefix, so it refreshes the cache TTL and the last request is this one. The
        # report can land up to FIRE_MIN later, still inside the TTL. Same value the
        # arm-time hook computes, so the two dedupe to one time instead of racing.
        print(json.dumps({"idle_minutes": idle,
                          "last_user_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                        time.gmtime(last_ts)),
                          "rearm_cron": cron_at(FIRE_MIN)}))
    return 0


def _cron_allowed(path):
    """(allowed, reason) from auto_done.py; a failure to decide arms nothing."""
    try:
        import auto_done
        return auto_done.allows("cron", path)
    except Exception as e:
        return False, "auto_done undecidable: %s" % str(e)[:80]


def cron_at(minutes_ahead):
    t = time.localtime(time.time() + minutes_ahead * 60)
    minute = t.tm_min
    if minute in (0, 30):        # the whole planet schedules on :00 and :30
        minute += 1
    return "%d %d %d %d *" % (minute, t.tm_hour, t.tm_mday, t.tm_mon)


def emit(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": text}}))
    sys.exit(0)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--idle":
        return idle_report(sys.argv[2])
    try:
        data = json.load(sys.stdin)
    except (ValueError, IOError):
        return 0
    if data.get("hook_event_name") != "UserPromptSubmit":
        return 0
    session = str(data.get("session_id") or "")

    # --- arming decision ---------------------------------------------------------
    if _switch(session)[0]:
        return 0
    path = find_transcript(data)
    if not path:
        return 0
    # Before read_state: this reads the transcript's tail, read_state all of it.
    if not _cron_allowed(path)[0]:
        return 0
    turns, armed, since_done, _ = read_state(path)
    if armed or turns < MIN_TURNS or since_done < QUIET_TURNS:
        return 0

    emit("[claudette auto-done] This session has no idle watchdog armed. Before anything "
         "else, call CronList: if any job's prompt carries '%s', a watchdog is already "
         "pending in this process - create NOTHING and answer the user normally. Otherwise "
         "make EXACTLY ONE tool call, the one below, then answer normally.\n"
         "CronCreate(cron=\"%s\", recurring=false, prompt=%s)"
         % (MARKER, cron_at(FIRE_MIN), json.dumps(fired_prompt(path))))


if __name__ == "__main__":
    sys.exit(main() or 0)
