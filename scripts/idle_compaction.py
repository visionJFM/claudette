#!/usr/bin/env python3
"""Does Claude Code compact idle sessions on this machine? Observed, never assumed.

WHY AN OBSERVATION. Idle compaction is Claude Code's, switched on from its own
servers: `idleCompaction: false` in settings.json turns it off, but `true` "does
not turn idle compaction on" (its own setting description), and it also needs a
1-hour prompt cache and sessions of 200k tokens or more. Nobody can answer "is it
on here?". The result only feeds the doctor's "idle compaction" row, which tells
whether the compaction half of auto_done has ever had anything to act on here. The
evidence is on disk: each idle compaction leaves "Compacted while idle, before the
prompt cache expired" in that session's transcript, and auto_done's compaction gate
sees every one it is asked about.

THE FILE. logs/idle_compaction_seen.json, single writer: this module (refresh()
from features_apply.py, note_seen() from auto_done's compaction gate):
    {"state": "seen"|"unseen"|"off", "last_seen": epoch|null,
     "checked_at": epoch, "setting_off": bool}
`seen` means within WINDOW_DAYS. prefs.features() reads `state` to pick a
feature's recommendation; it never scans.

THE SCAN is bounded because it reads transcripts: newest first, only files written
since the last sighting (or WINDOW_DAYS), at least MIN_BYTES (an idle compaction
needs a 200k-token context), at most MAX_FILES, and it stops at the first file
holding the evidence. TTL_S between scans; refresh(force=True) ignores it.
"""
import json
import os
import time

import paths

SEEN = paths.IDLE_COMPACTION_SEEN
EVIDENCE = b"Compacted while idle"
WINDOW_DAYS = 30
TTL_S = 6 * 3600
MIN_BYTES = 300 * 1024
MAX_FILES = 200
MAX_READ = 64 * 1024 * 1024


def read():
    try:
        with open(SEEN, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def _write(data):
    try:
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        tmp = SEEN + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, SEEN)
    except (IOError, OSError):
        pass


def setting_off(settings_path=None):
    """True when the user's settings.json says `idleCompaction: false`."""
    p = settings_path or os.path.join(paths.CLAUDE_DIR, "settings.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("idleCompaction") is False
    except (IOError, OSError, ValueError, AttributeError):
        return False


def _state(off, last_seen, now):
    if off:
        return "off"
    if last_seen and now - last_seen < WINDOW_DAYS * 86400:
        return "seen"
    return "unseen"


def _evidence_time(path):
    """Epoch of the newest idle-compaction notice in one transcript, or None."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_READ)
    except (IOError, OSError):
        return None
    i = blob.rfind(EVIDENCE)
    if i < 0:
        return None
    start = blob.rfind(b"\n", 0, i) + 1
    end = blob.find(b"\n", i)
    try:
        row = json.loads(blob[start:end if end >= 0 else len(blob)].decode("utf-8", "replace"))
        import calendar
        return calendar.timegm(time.strptime(str(row.get("timestamp"))[:19],
                                             "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError, AttributeError):
        return int(os.path.getmtime(path))


def scan(since, projects=None):
    """Newest evidence epoch in transcripts written after `since`, or None."""
    root = projects or os.path.join(paths.CLAUDE_DIR, "projects")
    files = []
    try:
        for proj in os.listdir(root):
            d = os.path.join(root, proj)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(d, fn)
                try:
                    s = os.stat(p)
                except OSError:
                    continue
                if s.st_mtime > since and s.st_size >= MIN_BYTES:
                    files.append((s.st_mtime, p))
    except OSError:
        return None
    for _, p in sorted(files, reverse=True)[:MAX_FILES]:
        t = _evidence_time(p)
        if t is not None:
            return t
    return None


def refresh(force=False, now=None, projects=None, settings_path=None):
    """Bring the file up to date (TTL'd) and return it."""
    now = now or time.time()
    cur = read()
    off = setting_off(settings_path)
    fresh = (not force and isinstance(cur.get("checked_at"), (int, float))
             and now - cur["checked_at"] < TTL_S and cur.get("setting_off") == off)
    if fresh:
        return cur
    last = cur.get("last_seen") if isinstance(cur.get("last_seen"), (int, float)) else None
    found = scan(max(last or 0, now - WINDOW_DAYS * 86400), projects)
    if found and (last is None or found > last):
        last = found
    data = {"state": _state(off, last, now), "last_seen": last, "checked_at": int(now),
            "setting_off": off}
    _write(data)
    return data


def note_seen(now=None):
    """The compact_done gate saw an idle compaction happen: record it now."""
    now = now or time.time()
    cur = read()
    off = bool(cur.get("setting_off"))
    data = {"state": _state(off, now, now), "last_seen": int(now),
            "checked_at": cur.get("checked_at") or int(now), "setting_off": off}
    _write(data)


def doctor_rows(auto_done_on):
    """One row: what is known about idle compaction here, and what it means for the
    compaction half of auto_done. Never a warn on its own: in the default mode the
    watchdog covers a 1-hour session that never gets compacted, and the
    "prompt cache" row already says when a 5-minute cache stops both."""
    cur = read()
    st, last = cur.get("state"), cur.get("last_seen")
    when = time.strftime("%Y-%m-%d", time.localtime(last)) if last else ""
    name = "idle compaction"
    if st == "off":
        return [(name, "off", "idleCompaction is false in settings.json: auto_done relies "
                 "on its watchdog alone", "")]
    if st == "seen":
        detail = "Claude Code compacts idle sessions here (last seen %s)" % when
        if auto_done_on:
            return [(name, "ok", detail + "; auto_done's gate " + _last_decision(), "")]
        return [(name, "ok", detail + "; auto_done is off",
                 paths.cmd("features_apply.py", "--feat", "auto_done=on"))]
    if st == "unseen":
        return [(name, "off", "not seen on this machine in the last %d days" % WINDOW_DAYS, "")]
    return [(name, "off", "not checked yet", "")]


def _last_decision():
    try:
        with open(paths.COMPACT_DONE_DECISIONS, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-50:]
        for line in reversed(lines):
            e = json.loads(line)
            if e.get("trigger") == "auto":
                return "last decided %s (%s) on %s" % (e.get("decision"), e.get("reason"),
                                                       str(e.get("iso", ""))[:16].replace("T", " "))
    except (IOError, OSError, ValueError):
        pass
    return "has decided nothing yet"


if __name__ == "__main__":
    import sys
    print(json.dumps(refresh(force="--force" in sys.argv), indent=1))
