#!/usr/bin/env python3
"""The reasoning effort a session actually runs at, for the status line's model segment.

WHY. A session can run at another level than any settings file says (Claude Code
also stores effort per model, `modelSettings.<model id>.effortLevel`): `claude
--effort low`, or `/effort` typed mid-session. The segment shows what the API
received.

ORDER.
1. The status line payload's own `effort` (`{"level": "high"}`, seen on client
   2.1.323): the client's live value, right from the first render, launch flag
   and `/effort` included. When the key is there it is the whole answer, even
   empty (a model without effort shows none).
2. Payload without the key (a client that does not send it): the `effort` of
   the newest main-thread, non-synthetic assistant row in the transcript tail,
   as sent on that request, when its model is the session's current model. A
   different model means `/model` switched since: that effort was the old
   model's. Nothing before the session's first answer.
3. Nothing: the segment shows the model alone.

Settings files are NOT read: a configured default is not the effort the session
runs at (`--effort low` overrides it), and a wrong level reads as a fact where an
absent one reads as "not known yet".

Python floor 3.9, stdlib only. Never raises: "" means "nothing to show".
"""
import os
import re

import cache_ttl

# What the line can show: a short word. Anything else (an object, a typo with
# control characters) is not displayed.
_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")


def _clean(v):
    return v if isinstance(v, str) and _WORD.match(v) else ""


def from_payload(payload):
    """(True, level or "") when the client sent an `effort` key, else (False, "")."""
    if not isinstance(payload, dict) or "effort" not in payload:
        return False, ""
    e = payload.get("effort")
    if isinstance(e, dict):
        return True, _clean(e.get("level"))
    return True, _clean(e)  # a bare word, should the client flatten it


def from_transcript(transcript_path, model_id):
    """Effort of the newest main-thread answer by the current model, or ""."""
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return ""
        want = cache_ttl._model_key(model_id)
        for size in cache_ttl.TAIL_STEPS:
            rows, whole = cache_ttl._rows(transcript_path, size)
            for r in reversed(rows):
                if not cache_ttl._is_response(r):
                    continue
                if want and cache_ttl._model_key((r.get("message") or {}).get("model")) != want:
                    return ""  # newest answer is another model: switched since
                # An answer without the field (a model without effort): looking
                # further back would show a stale value, so stop here.
                return _clean(r.get("effort"))
            if whole:
                break
        return ""
    except Exception:
        return ""


def current(payload):
    """Effort to show for the session this status line payload describes."""
    try:
        known, level = from_payload(payload)
        if known:
            return level
        return from_transcript(payload.get("transcript_path"),
                               (payload.get("model") or {}).get("id"))
    except Exception:
        return ""
