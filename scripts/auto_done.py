#!/usr/bin/env python3
"""The auto_done feature's one decision: may this session get an idle done report,
and by which mechanism. Shared by its hooks, so they cannot disagree.

WHY ONE FEATURE. The idle report has three mechanisms, and which one can work depends
on the session's prompt-cache TTL, which is per session and can change from one day
to the next (an idle compaction needs a 1-hour cache). So all the hooks stay installed
and this module decides, per session, at the moment it runs (CLAUDETTE_DESIGN.md,
"auto_done: one feature, and the session's cache decides").

THE MECHANISMS: `cron` (the watchdog, hook_watchdog.py), `gate` (the compaction gate,
hook_compact_done.py) and `timer` (the idle timer, hook_idle_timer.py: a background
Stop hook that does the cron's job with nothing in the conversation until it wakes,
and yields to the gate where the idle compaction is due).

THE MODE, param `auto_done_watcher`:
  auto         (default) the gate and the idle timer while the session's cache is
               1 hour; nothing on a 5-minute cache or before one is known. A report on
               a 5-minute cache runs cold - it re-writes the whole context, and the
               user's return re-writes it again - and Claude Code never compacts
               such a session, so its context is not at risk. The cron is not used.
  cron         the watchdog alone, whatever the cache.
  idlecompact  the compaction gate alone.
  both         the watchdog and the gate, whatever the cache.
An unknown value reads as `auto`.

The TTL comes from cache_ttl.py, i.e. from the API's own `cache_creation` in the
transcript - never from what the model is told, which can differ from what the API
applied.

What the mode does NOT govern: the gate's `keep` (a report already written rides
through the compaction) and the restore after it are free and stay on in every
mode; the `.autodone` switches (watchdog_switch.py) stop both mechanisms.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FEATURE = "auto_done"
PARAM = "auto_done_watcher"
MODES = ("auto", "cron", "idlecompact", "both")
MECHANISMS = ("cron", "gate", "timer")


def mode():
    """The configured mode; anything unreadable or unknown is `auto`."""
    try:
        import prefs
        m = prefs.param(PARAM, "auto")
    except Exception:
        return "auto"
    m = str(m).strip().lower() if m is not None else "auto"
    return m if m in MODES else "auto"


def session_ttl(transcript_path):
    """'5m' | '1h' | None, from the transcript."""
    try:
        import cache_ttl
        st = cache_ttl.state(transcript_path)
        return st["ttl"] if st else None
    except Exception:
        return None


def allows(mechanism, transcript_path, current_mode=None, ttl=None):
    """(allowed, reason). `ttl` may be passed when already read (tests, callers that
    read it once for several decisions)."""
    m = current_mode or mode()
    if mechanism not in MECHANISMS:
        return False, "unknown mechanism %r" % mechanism
    if m == "cron":
        return mechanism == "cron", "mode cron"
    if m == "idlecompact":
        return mechanism == "gate", "mode idlecompact"
    if m == "both":
        return mechanism in ("cron", "gate"), "mode both"
    if mechanism == "cron":
        return False, "mode auto runs the idle timer, not the cron"
    t = ttl if ttl is not None else session_ttl(transcript_path)
    if t == "1h":
        return True, "mode auto, 1-hour cache"
    if t == "5m":
        return False, "mode auto, 5-minute cache (a report would run cold)"
    return False, "mode auto, cache TTL not known yet"


def describe(transcript_path):
    """One line for `.autodone` and the doctor: the mode and what it means for the
    session at hand."""
    m = mode()
    ttl = session_ttl(transcript_path) if transcript_path else None
    # Only what runs, to keep the line short.
    parts = [label for mech, label in (("cron", "watchdog"), ("timer", "idle timer"),
                                       ("gate", "compaction gate"))
             if allows(mech, transcript_path, m, ttl)[0]]
    cache = {"1h": "1-hour cache", "5m": "5-minute cache"}.get(ttl, "cache TTL unknown")
    return "mode %s; this session: %s -> %s" % (m, cache, ", ".join(parts) or "nothing runs")
