#!/usr/bin/env python3
"""How long a session's prompt cache stays warm, read from its transcript tail.

WHY. Claude Code tells neither the status line nor any hook which prompt-cache TTL
a session runs on, and what the model is told can be wrong: in client 2.1.300 the
tool text said "1-hour Anthropic prompt-cache TTL" while the API applied 5 minutes.
The API's own answer is in every assistant row,
`usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, so
that is the only source read here.

WHAT COUNTS.
- TTL: the newest main-thread response that wrote to the cache. A response that
  wrote nothing (a full hit) does not say, so the walk goes on to an older one.
  A row carrying both kinds reads as 5m, the client's own rule (the shorter one
  expires first).
- Start of the countdown: when the last main-thread request was SENT, i.e. the
  newest user row (prompt or tool result) before that response's first row. The
  cache is refreshed when the request is read, not when the answer ends; a long
  answer would otherwise add its own duration to the display. A request sent
  after the last response counts from its own send: a long first answer writes
  no row for minutes.
  While a tool runs no request is sent, so a long tool can let the cache go
  cold for real, and the line says so.
- Cold without waiting: a compaction newer than the last response (the next
  request writes a new prefix), or a model different from the one that answered
  (each model has its own cache).
- Ignored: subagent rows (`isSidechain`), synthetic rows (no API call), and
  helper requests that may also refresh the prefix (away summary, suggestions) -
  the display can only err toward less time left.

Python floor 3.9, stdlib only. Never raises: None means "nothing to show".
"""
import calendar
import json
import os
import re
import time

TTL_S = {"5m": 300, "1h": 3600}
# Tail sizes tried in turn: a transcript is often 10 MB+, and one tool result
# can be a single line of several MB.
TAIL_STEPS = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024)
_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$")


def _epoch(ts):
    m = _TS.match(ts or "")
    if not m:
        return None
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) + float(m.group(2) or 0)


def _model_key(m):
    # `claude-opus-5-5[1m]` and `claude-opus-5-5` share one cache.
    return re.sub(r"\[.*\]$", "", (m or "").strip()).lower()


def _rows(path, size):
    """(rows oldest first, whether the read reached the file's start)."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        start = max(0, end - size)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start > 0:
        lines = lines[1:]  # cut mid-line by the seek
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue  # blank, or the line Claude Code is still writing
    return rows, start == 0


def _ttl_of(row):
    cc = ((row.get("message") or {}).get("usage") or {}).get("cache_creation") or {}
    if (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
        return "5m"
    if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
        return "1h"
    return None


def _is_response(row):
    if row.get("type") != "assistant" or row.get("isSidechain"):
        return False
    msg = row.get("message") or {}
    return msg.get("model") != "<synthetic>" and isinstance(msg.get("usage"), dict)


def _is_request(row):
    """A main-thread user row that was sent to the API: a prompt or tool results.
    Local commands (/rename, /model...) and their output, meta rows, the
    interruption notice and the summary a compaction leaves are written as user
    rows too, and sent nothing: the compacted context is cold until a real request."""
    if (row.get("type") != "user" or row.get("isSidechain") or row.get("isMeta")
            or row.get("isCompactSummary")):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return True
        content = texts[0] if texts else ""
    text = (content or "").lstrip() if isinstance(content, str) else ""
    return not text.startswith(("<command-name>", "<command-message>", "<local-command-",
                                "[Request interrupted"))


def _scan(rows):
    """{ttl, sent_at, model, compacted_after, in_flight} from rows oldest first, or None."""
    last = None
    for i in range(len(rows) - 1, -1, -1):
        if _is_response(rows[i]):
            last = i
            break
    if last is None:
        return None
    # Request still being answered; checked first, it overrides a compaction or
    # model change it follows.
    for i in range(len(rows) - 1, last, -1):
        if _is_request(rows[i]):
            ttl = None
            for j in range(last, -1, -1):
                if _is_response(rows[j]):
                    ttl = _ttl_of(rows[j])
                    if ttl:
                        break
            return {"ttl": ttl, "sent_at": _epoch(rows[i].get("timestamp")),
                    "model": None, "compacted_after": False, "in_flight": True}
    msg_id = (rows[last].get("message") or {}).get("id")
    first = last
    for i in range(last - 1, -1, -1):
        r = rows[i]
        if _is_response(r):
            if msg_id and (r.get("message") or {}).get("id") == msg_id:
                first = i
                continue
            break
        if r.get("type") == "user" and not r.get("isSidechain"):
            break
    sent_at = None
    for i in range(first - 1, -1, -1):
        r = rows[i]
        if r.get("type") == "user" and not r.get("isSidechain"):
            sent_at = _epoch(r.get("timestamp"))
            break
        if _is_response(r):
            break  # no user row between two responses: keep the fallback
    if sent_at is None:
        sent_at = _epoch(rows[first].get("timestamp"))
    ttl = None
    for i in range(last, -1, -1):
        if _is_response(rows[i]):
            ttl = _ttl_of(rows[i])
            if ttl:
                break
    compacted_after = any(
        r.get("type") == "system" and r.get("subtype") == "compact_boundary"
        for r in rows[last + 1:])
    return {"ttl": ttl, "sent_at": sent_at,
            "model": (rows[last].get("message") or {}).get("model"),
            "compacted_after": compacted_after, "in_flight": False}


def state(transcript_path, model_id=None, now=None):
    """{'ttl': '5m'|'1h', 'remaining_s': int} for this session, or None."""
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return None
        found = None
        for size in TAIL_STEPS:
            rows, whole = _rows(transcript_path, size)
            found = _scan(rows)
            if (found and found["ttl"] and found["sent_at"] is not None) or whole:
                break
        if not found or not found["ttl"] or found["sent_at"] is None:
            return None
        ttl_s = TTL_S[found["ttl"]]
        now = time.time() if now is None else now
        remaining = ttl_s - (now - found["sent_at"])
        cold = found["compacted_after"] or (
            model_id and found["model"]
            and _model_key(model_id) != _model_key(found["model"]))
        if cold:
            remaining = 0
        return {"ttl": found["ttl"], "remaining_s": int(max(0, min(ttl_s, remaining)))}
    except Exception:
        return None


def newest_session_ttl(home, entrypoint="cli", max_files=20):
    """(ttl, path) of the newest INTERACTIVE session that wrote to the cache, or
    (None, None). Interactive only: a `claude -p` run can get 1 hour where the
    interactive sessions of the same machine get 5 minutes."""
    import glob
    try:
        files = glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl"))
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    except OSError:
        return None, None
    for f in files[:max_files]:
        try:
            rows, _ = _rows(f, TAIL_STEPS[0])
        except OSError:
            continue
        resp = [r for r in rows if _is_response(r)]
        if not resp or resp[-1].get("entrypoint") != entrypoint:
            continue
        found = _scan(rows)
        if found and found["ttl"]:
            return found["ttl"], f
    return None, None


def doctor_rows(ttl, auto_done_on, prompt_cache_on):
    """One row: the TTL the newest interactive session got, and what it means for
    the idle done report."""
    name = "prompt cache"
    try:
        import auto_done
        mode = auto_done.mode()
    except Exception:
        mode = "auto"
    if ttl == "1h":
        return [(name, "ok", "newest interactive session: 1-hour cache", "")]
    if ttl == "5m":
        detail = "newest interactive session: 5-minute cache"
        if auto_done_on and mode == "auto":
            detail += " - auto_done gives it no report, and Claude Code never compacts it"
        if prompt_cache_on:
            return [(name, "warn", detail + "; promptCacheTtl is set, so that session "
                     "predates it or something overrides it", "open a new session and look again")]
        try:
            import paths
            fix = paths.cmd("features_apply.py", "--feat", "prompt_cache_1h=on")
        except Exception:
            fix = "features_apply.py --feat prompt_cache_1h=on"
        return [(name, "warn" if auto_done_on else "off", detail,
                 "on a Claude subscription: " + fix)]
    return [(name, "off", "no interactive session with a cache write found", "")]


def minutes_left(st):
    """Whole minutes, rounded up while any time is left: 30 s shows 1, expired 0."""
    return -(-st["remaining_s"] // 60)


if __name__ == "__main__":
    import sys
    print(json.dumps(state(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
