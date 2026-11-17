#!/usr/bin/env python3
"""Claudette auto_done idle timer - the done report ~50 minutes after a session's last
turn, with no cron and nothing in the conversation until it decides to wake.

PART OF auto_done. The `timer` mechanism beside the compaction gate
(hook_compact_done.py). In mode `auto` it replaces the cron watchdog (hook_watchdog.py),
which costs every session it arms an injected instruction and tool calls the model has
to make.

HOW. A `Stop` hook with `asyncRewake: true`: Claude Code runs it in the background and,
when it exits 2, wakes the model with its stderr. So every turn's end starts a sleeper
that waits, polling once a minute, and decides at the end with everything known. Live
facts this rests on (client 2.1.302):
  - the wake reaches an idle interactive session and gives the model a turn;
  - a background hook lives as long as its `timeout` (600 s by default), hence 3900;
  - a user prompt does NOT cancel a waiting sleeper, nor does /clear, whose wake then
    lands in the NEW conversation; /exit kills it; a hard kill orphans it;
  - in a non-interactive run (`-p`, entrypoint `sdk-cli`) the same hook runs INLINE and
    holds the run until it exits: 50 minutes added to every `claude -p`.
Hence the guards, in the order they act:
  1. Anything but entrypoint `cli` exits at once, before any other import or file read.
     VS Code (`claude-vscode`) stays out until tested: if its host also ran the hook
     inline, the cost would be a 50-minute hang, against a report the gate still gives.
  2. Newest only: each sleeper writes its token to the session's state file; one that
     finds another token there was superseded by a later turn and leaves.
  3. Session ended: the SessionEnd hook (`end`) marks the session id, and a sleeper that
     finds the mark leaves - what keeps a pre-/clear sleeper out of the new conversation.
  4. At fire time, decide(), with the cron's and the gate's rules: switches, mode and
     cache TTL (auto_done.allows), no activity since this sleeper armed, a young session
     or a /done just run (hook_watchdog's MIN_TURNS / QUIET_TURNS), no report and no wake
     (the gate's or ours) since the last prompt, no scheduled work, the 5h cap, a cache
     still warm. When the idle compaction should come (200k tokens or more, idleCompaction
     not off, the gate allowed here), it waits for the gate until GATE_FALLBACK_S and
     wakes only if the gate never acted: the gate goes first.
  5. Wall clock, never slept time: a machine asleep through the window finds the cache
     cold at its first poll after resume, and the sleeper leaves.

THREE MODES, ONE SCRIPT:
  arm    Stop, asyncRewake, timeout 3900. The sleeper above.
  end    SessionEnd. Marks the session ended. On /clear, also hands this session's
         `.autodone off` to the next session of the same process (CLAUDE_PID), since
         /clear mints a new session id and the switch is keyed on it.
  start  SessionStart, matcher `clear`. Takes that hand-off.

REPORT ONLY, like the cron's fired prompt and the gate's wake.

TEST-ONLY ENVIRONMENT (the registry sets none):
  CLAUDETTE_IDLE_TIMER_FIRE_S / _FALLBACK_S / _POLL_S   shorter waits
  CLAUDETTE_IDLE_TIMER_WAKE_FILE   replaces the wake's instructions after the marker
"""
import os
import sys

INTERACTIVE = ("cli",)


def entrypoint_ok():
    """Guard 1, before anything slow: in a `-p` run this process is on the run's
    critical path for as long as it lives."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") in INTERACTIVE


if __name__ == "__main__" and not entrypoint_ok():
    sys.exit(0)

import json  # noqa: E402
import time  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

FEATURE = "auto_done"
TIMER_MARKER = "[claudette idle-timer]"
WATCHDOG_MARKER = "[claudette auto-done watchdog]"
FIRE_S = 50 * 60            # the cron's FIRE_MIN, counted from the turn's end instead
GATE_FALLBACK_S = 57 * 60   # idle compaction comes ~54 min after the last request
POLL_S = 60
ACTIVITY_GRACE_S = 5        # the arming turn's own last rows can share its second
MIN_WARM_S = 120            # less cache than this left: the report would run cold
CTX_GATE_TOKENS = 200000    # Claude Code's idle compaction threshold
CARRY_MAX_S = 60            # a /clear hand-off older than this belongs to nobody
STATE_KEEP_S = 7 * 86400
LOG_CAP_BYTES = 256 * 1024
LOG_KEEP = 400


def _env_s(name, default):
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


# --- files (single writer each: this script; the off switch goes through watchdog_switch)

def _state_path(sid):
    return os.path.join(paths.IDLE_TIMER_DIR, sid + ".json")


def _ended_path(sid):
    return os.path.join(paths.IDLE_TIMER_DIR, sid + ".ended")


def _carry_path(claude_pid):
    return os.path.join(paths.IDLE_TIMER_DIR, "carry.%s" % claude_pid)


def _write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = "%s.tmp.%d" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def _prune(now):
    """State files of sessions gone for a week. Cheap: one directory listing per arm."""
    try:
        for fn in os.listdir(paths.IDLE_TIMER_DIR):
            p = os.path.join(paths.IDLE_TIMER_DIR, fn)
            try:
                if now - os.path.getmtime(p) > STATE_KEEP_S:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def log(entry):
    p = paths.IDLE_TIMER_DECISIONS
    entry = dict({"ts": int(time.time()), "iso": time.strftime("%Y-%m-%dT%H:%M:%S")}, **entry)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if os.path.getsize(p) > LOG_CAP_BYTES:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-LOG_KEEP:]
            tmp = p + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, p)
    except (IOError, OSError):
        pass


# --- what the session says ------------------------------------------------------

def context_tokens(path):
    """Tokens in context at the last main-thread response: what it sent, cached or not."""
    try:
        import cache_ttl
        rows, _ = cache_ttl._rows(path, cache_ttl.TAIL_STEPS[1])
        for r in reversed(rows):
            if cache_ttl._is_response(r):
                u = (r.get("message") or {}).get("usage") or {}
                return sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    except Exception:
        pass
    return None


def user_crons(crons):
    """The session's scheduled jobs (Stop input `session_crons`) that are the user's:
    the cron watchdog's own jobs are bookkeeping, not work."""
    out = []
    for c in crons or []:
        if isinstance(c, dict) and WATCHDOG_MARKER not in str(c.get("prompt") or ""):
            out.append(c)
    return out


def decide(now, armed_at, phase, st, turns, since_done, crons, may, capped,
           cache_left, ctx, gate_due):
    """(verdict, reason) - pure, so tests need no live session.
    verdict: wake | wait | stand_down. phase: fire | fallback.
    st: hook_compact_done.scan() plus `timer_ln`; may: (allowed, why) from auto_done;
    cache_left: seconds of prompt cache left, None unknown; gate_due: the idle
    compaction should come for this session (size, setting, gate allowed)."""
    import hook_watchdog
    ok, why = may
    if not ok:
        return "stand_down", why
    if st is None:
        return "stand_down", "no transcript"
    if st["last_activity"] is not None and st["last_activity"] > armed_at + ACTIVITY_GRACE_S:
        return "stand_down", "session active since this timer armed"
    if turns < hook_watchdog.MIN_TURNS:
        return "stand_down", "young session (%d prompts)" % turns
    if since_done < hook_watchdog.QUIET_TURNS:
        return "stand_down", "/done already run"
    if st["report_ln"] > st["prompt_ln"]:
        return "stand_down", "report since the last prompt"
    if max(st["wake_ln"], st.get("timer_ln", -1)) > st["prompt_ln"]:
        return "stand_down", "already woken since the last prompt"
    if user_crons(crons):
        return "stand_down", "scheduled work (cron)"
    try:
        import hook_compact_done
        if hook_compact_done.scheduled(st, now):
            return "stand_down", "scheduled work (loop or cron)"
    except Exception:
        pass
    if capped:
        return "stand_down", "5h window at its cap"
    if cache_left is not None and cache_left < MIN_WARM_S:
        return "stand_down", "prompt cache cold (%ds left)" % cache_left
    if phase == "fire" and gate_due:
        return "wait", "idle compaction due (%s tokens): the gate goes first" % ctx
    return "wake", ("gate did not come" if phase == "fallback" else "idle, no report yet")


def wake_text(idle_min, override=None):
    if override:
        return TIMER_MARKER + " " + override
    try:
        import hook_watchdog
        chart = hook_watchdog._chart_step(2)
    except Exception:
        chart = ""
    return (TIMER_MARKER + " This session has been idle for %d min and its prompt cache is "
            "still warm. This is the auto_done feature the user switched on, not a request "
            "they typed now.\n"
            "1. Invoke the claudette:done skill with no argument and present its report.\n"
            "%s"
            "REPORT ONLY: do not commit, stage, push, write a memory file, or change "
            "anything else - the user is away and will act on the report when they return."
            % (idle_min, chart))


# --- modes ------------------------------------------------------------------------

def _enabled():
    try:
        import prefs
        return prefs.effective(FEATURE)
    except Exception:
        return False


def _may(sid, path):
    """(allowed, why): the `.autodone` switches, then auto_done.py."""
    try:
        import hook_watchdog
        off, where = hook_watchdog._switch(sid)
        if off:
            return False, "switched off %s (.autodone)" % where
        import auto_done
        return auto_done.allows("timer", path)
    except Exception as e:
        return False, "auto_done undecidable: %s" % str(e)[:80]


def _transcript(inp):
    p = inp.get("transcript_path")
    if p and os.path.isfile(p):
        return p
    try:
        import hook_watchdog
        return hook_watchdog.find_transcript(inp)
    except Exception:
        return None


def _facts(sid, path, now):
    """Everything decide() reads, gathered at fire time."""
    import hook_compact_done as cd
    import hook_watchdog
    st = cd.scan(path) if path else None
    turns, since_done = 0, 99
    if path:
        turns, _, since_done, _ = hook_watchdog.read_state(path)
    cache_left = None
    try:
        import cache_ttl
        s = cache_ttl.state(path)
        cache_left = s["remaining_s"] if s else None
    except Exception:
        pass
    ctx = context_tokens(path) if path else None
    gate_due = False
    if ctx is not None and ctx >= CTX_GATE_TOKENS:
        try:
            import auto_done
            import idle_compaction
            gate_due = (not idle_compaction.setting_off()
                        and auto_done.allows("gate", path)[0])
        except Exception:
            gate_due = False
    return st, turns, since_done, cache_left, ctx, gate_due, cd.capped(now)


def arm(inp):
    sid = str(inp.get("session_id") or "")
    if not sid or not _enabled():
        return 0
    path = _transcript(inp)
    ok, why = _may(sid, path)
    if not ok:
        return 0          # the common silent case: mode, cache or switch says no
    crons = user_crons(inp.get("session_crons"))
    if crons:
        log({"session_id": sid, "event": "skip", "reason": "scheduled work (cron)"})
        return 0
    start = time.time()
    token = "%d:%d" % (os.getpid(), int(start * 1000))
    _prune(start)
    try:
        # A turn just ended, so the session is alive: an `ended` mark here is from an
        # earlier life of this id (exited, then resumed).
        os.remove(_ended_path(sid))
    except OSError:
        pass
    try:
        _write_json(_state_path(sid), {"token": token, "armed_at": start})
    except (IOError, OSError):
        return 0
    log({"session_id": sid, "event": "arm", "token": token})

    fire_s = _env_s("CLAUDETTE_IDLE_TIMER_FIRE_S", FIRE_S)
    fallback_s = _env_s("CLAUDETTE_IDLE_TIMER_FALLBACK_S", GATE_FALLBACK_S)
    poll_s = _env_s("CLAUDETTE_IDLE_TIMER_POLL_S", POLL_S)
    phase, fire_at = "fire", start + fire_s
    while True:
        now = time.time()
        time.sleep(max(0.5, min(poll_s, fire_at - now)))
        now = time.time()
        # An unreadable file is not another token: on Windows a read can collide with
        # the next turn's os.replace, and leaving then would drop a live timer.
        cur = _read_json(_state_path(sid)).get("token")
        if cur and cur != token:
            log({"session_id": sid, "event": "superseded", "token": token})
            return 0
        if (_read_json(_ended_path(sid)).get("ts") or 0) >= int(start):
            log({"session_id": sid, "event": "ended", "token": token})
            return 0
        if now < fire_at:
            continue
        path = _transcript(inp)
        st, turns, since_done, cache_left, ctx, gate_due, capped = _facts(sid, path, now)
        verdict, reason = decide(now, start, phase, st, turns, since_done, inp.get("session_crons"),
                                 _may(sid, path), capped, cache_left, ctx, gate_due)
        idle = int((now - start) // 60)
        log({"session_id": sid, "event": verdict, "reason": reason, "token": token,
             "idle_min": idle, "phase": phase, "ctx": ctx, "cache_left": cache_left})
        if verdict == "wait" and phase == "fire":
            phase, fire_at = "fallback", start + fallback_s
            continue
        if verdict != "wake":
            return 0
        override = None
        wf = os.environ.get("CLAUDETTE_IDLE_TIMER_WAKE_FILE")
        if wf:
            try:
                with open(wf, encoding="utf-8") as fh:
                    override = fh.read().strip()
            except (IOError, OSError):
                override = None
        sys.stderr.write(wake_text(idle, override))
        return 2


def end(inp):
    sid = str(inp.get("session_id") or "")
    if not sid:
        return 0
    reason = str(inp.get("reason") or "")
    try:
        _write_json(_ended_path(sid), {"ts": int(time.time()), "reason": reason})
    except (IOError, OSError):
        pass
    if reason == "clear":
        try:
            import hook_watchdog
            pid = os.environ.get("CLAUDE_PID")
            if pid and os.path.exists(os.path.join(hook_watchdog.WDIR, sid + ".off")):
                _write_json(_carry_path(pid), {"ts": int(time.time()), "from": sid})
        except (IOError, OSError, ImportError):
            pass
    return 0


def start(inp):
    if inp.get("source") != "clear":
        return 0
    sid = str(inp.get("session_id") or "")
    pid = os.environ.get("CLAUDE_PID")
    if not sid or not pid:
        return 0
    p = _carry_path(pid)
    carry = _read_json(p)
    if not carry:
        return 0
    try:
        os.remove(p)
    except OSError:
        pass
    if time.time() - (carry.get("ts") or 0) > CARRY_MAX_S:
        return 0
    import watchdog_switch
    watchdog_switch.set_switch(True, False, sid)
    log({"session_id": sid, "event": "switch carried", "from": carry.get("from")})
    return 0


DOCTOR_DAYS = 7
DOCTOR_MIN_ARMS = 5


def doctor_rows(auto_done_on, now=None):
    """One row: what the idle timer did lately. A warn only when it armed several times
    in the window and never once reached its fire-time check: Claude Code stopping
    background hooks early (a client change to `asyncRewake` or to hook timeouts) would
    otherwise fail silently, sessions simply getting no report."""
    name = "idle timer"
    if not auto_done_on:
        return []
    try:
        import auto_done
        mode = auto_done.mode()
    except Exception:
        mode = "auto"
    if mode != "auto":
        return [(name, "off", "mode %s does not use it" % mode, "")]
    now = now or time.time()
    try:
        with open(paths.IDLE_TIMER_DECISIONS, encoding="utf-8", errors="replace") as fh:
            events = [json.loads(x) for x in fh if x.strip()]
    except (IOError, OSError, ValueError):
        events = []
    recent = [e for e in events if now - (e.get("ts") or 0) < DOCTOR_DAYS * 86400]
    arms = [e for e in recent if e.get("event") == "arm"]
    checks = [e for e in recent if e.get("event") in ("wake", "stand_down", "wait")]
    if not arms:
        return [(name, "off", "has not armed in the last %d days (a session loads it when "
                 "it starts)" % DOCTOR_DAYS, "")]
    if (not checks and len(arms) >= DOCTOR_MIN_ARMS
            and now - (arms[0].get("ts") or now) > 2 * 3600):
        return [(name, "warn", "armed %d times in %d days and never reached its 50-minute "
                 "check: Claude Code may be stopping it early" % (len(arms), DOCTOR_DAYS),
                 "design notes, not published: CLAUDETTE_DESIGN.md, the \"Idle timer\" entries")]
    wakes = sum(1 for e in checks if e.get("event") == "wake")
    detail = "%d wake%s in %d days" % (wakes, "" if wakes == 1 else "s", DOCTOR_DAYS)
    if checks:
        last = checks[-1]
        detail += "; last check: %s (%s) on %s" % (
            last.get("event"), last.get("reason"),
            str(last.get("iso", ""))[:16].replace("T", " "))
    return [(name, "ok", detail, "")]


def main(argv):
    if not entrypoint_ok():
        return 0
    mode = argv[1] if len(argv) > 1 else ""
    fn = {"arm": arm, "end": end, "start": start}.get(mode)
    if fn is None:
        return 0
    try:
        inp = json.load(sys.stdin)
    except (ValueError, IOError):
        return 0
    if not isinstance(inp, dict):
        return 0
    try:
        return fn(inp)
    except Exception as e:  # never break a turn's end, a /clear or an exit
        try:
            sys.stderr.write("claudette idle timer %s: %s\n" % (mode, str(e)[:200]))
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
