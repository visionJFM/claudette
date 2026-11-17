#!/usr/bin/env python3
"""".note" — a remark about this moment, kept in the transcript, skipping the agent's context.

Why: the "why" behind a correction, voiced inside a session, derails it. A dot-command
costs no turn and never enters the model's context, and the client already keeps the
prompt: a blocked prompt is written into the transcript as the tail of the block's
system entry ("Original prompt: <prompt>"), chained after the message it followed. So
this command stores nothing in the transcript itself; it confirms.

    .note <text>     keep it (line breaks allowed; every character kept)
    .note            this session's notes, oldest first
    .note N          note N in full
    .note ?          usage
    ..note <text>    the same in context: reaches the phone, costs a turn

WHERE A NOTE IS, the contract every reader keys on (CLAUDETTE_LOGS.md "notes_typed"):
  free channel     a system entry opening "UserPromptSubmit operation blocked by
                   hook" whose first "\\n\\nOriginal prompt: " is followed by the prompt;
  typed mid-turn   through client 2.1.294, a "queue-operation" entry, operation
                   "remove", reason "dropped_by_hook", whose "content" is the prompt:
                   the hook runs when the prompt is queued, and the client writes no
                   system entry. From 2.1.295 (checked on 2.1.300) the client writes
                   NOTHING for it: the copy below is its only record;
  ".." channel     the user's own prompt entry — it was not blocked.
Every prompt goes through dot_registry.match() and kind(): only a "record" is a
note, so the listing, "?" and ".note N" never count as one. The queue's "enqueue"
line is never read: a queued "..note" has one too, and would count twice.

The confirmation never echoes the text: the client already stores it, and an echo
would be a second copy.

Transcript first, copy second. That suffix is an undocumented client string that a
client upgrade can change, so every note is also appended to logs/notes_typed.jsonl:
the ground truth verify() holds transcripts against (doctor, dot_transcript_check.py),
and the rescue when the client stops recording, as it does for notes typed mid-turn.
Readers read session_notes(): the transcript's notes plus the copy's rows it lacks,
placed by time. The copy fills gaps and never replaces a note the transcript holds.
The doctor warns when a note typed while the agent was idle is missing, since that is
the shape the client still records.
"""
import collections
import datetime
import json
import os
import re

import dot_common as dc
import dot_registry
import paths

LEDGER = paths.log("notes_typed.jsonl")
BLOCKED = "UserPromptSubmit operation blocked by hook"
ORIGINAL = "\n\nOriginal prompt: "
# The client writes its entry after the hook returns: a check run in the same
# instant must not call a note typed a second ago "missing".
GRACE_S = 30
# System entries the client writes when a turn ends (an Esc writes neither: its
# "[Request interrupted by user" entry ends the turn instead).
TURN_END = ("turn_duration", "stop_hook_summary")

_SHOW = re.compile(r"\d+")

USAGE = [
    (".note <text>", "kept in the transcript, skipping the agent's context (multi-line ok)"),
    (".note N", "note N in full"),
    ("..note <text>", "the same in context: reaches the phone, costs a turn"),
]


def kind(tail):
    """What a .note tail asks for: list | usage | show | record. One place, so the
    handler and the transcript reader can never disagree about what was a note.
    Every character counts: ".note." keeps ".", ".note!" keeps "!"."""
    if not tail:
        return "list"
    if tail == "?":
        return "usage"
    if _SHOW.fullmatch(tail):
        return "show"
    return "record"


def norm(text):
    """Compare form: the client may re-space what it stores; a whitespace
    difference must never read as a lost note."""
    return re.sub(r"\s+", " ", text or "").strip()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def ledger(sid=None):
    """Rows of the copy, oldest first; corrupt lines skipped."""
    rows = []
    try:
        with open(LEDGER, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and (sid is None or r.get("session_id") == sid):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _copy(data, text, to_context):
    """Append one row; the only file this command writes (claudette's own log)."""
    row = {"ts": _utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
           "session_id": str(data.get("session_id") or ""),
           "transcript": str(data.get("transcript_path") or ""),
           "cwd": str(data.get("cwd") or ""),
           "channel": "context" if to_context else "free",
           "text": text}
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def note_of(prompt):
    """The note a prompt records, or None."""
    m = dot_registry.match(prompt or "")
    if m and m.entry and m.entry[1] == "dot_note" and kind(m.tail) == "record":
        return m.tail
    return None


def original_prompt(content):
    """The prompt the client appended to a blocked-hook entry, or None. The FIRST
    marker is the client's: nothing this hook prints carries the user's text, so
    the marker cannot come from the reason above it."""
    if not isinstance(content, str) or not content.startswith(BLOCKED):
        return None
    parts = content.split(ORIGINAL, 1)
    return parts[1] if len(parts) == 2 else None


def notes_in(tpath):
    """[(ts, text, channel)] recorded in one transcript, in FILE order. Raises
    OSError when the file cannot be read — the caller decides what that means."""
    out = []
    dropped = {}                  # prompt -> when the queue dropped it
    with open(tpath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # cheap prefilter (transcripts run to MBs), case-blind: a phone
            # keyboard capitalises "..Note"
            if "note" not in line.lower():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ts = d.get("timestamp", "")
            if (d.get("type") == "queue-operation" and d.get("operation") == "remove"
                    and d.get("reason") == "dropped_by_hook"):
                prompt = str(d.get("content") or "")
                text = note_of(prompt)
                if text is not None:
                    dropped[prompt] = dc.when(ts)
                    out.append((ts, text, "free"))
            elif d.get("type") == "system":
                prompt = original_prompt(d.get("content"))
                text = note_of(prompt)
                # both shapes for one prompt count once: the queue's line wins
                t0, t1 = dropped.get(prompt), dc.when(ts)
                if t0 and t1 and 0 <= (t1 - t0).total_seconds() <= 60:
                    continue
                if text is not None:
                    out.append((ts, text, "free"))
            elif d.get("type") == "user":
                k, txt = dc.classify(d)
                text = note_of(txt) if k == "prompt" else None
                if text is not None:
                    # A user entry means the model saw it: ".." by choice, or a
                    # "." the hook never intercepted.
                    out.append((ts, text, "context"))
    return out


def _tool_result(d):
    c = (d.get("message") or {}).get("content")
    return "toolUseResult" in d or (isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c))


def _turns(tpath):
    """(working, ended): when the agent was at work, and when a turn ended, in one
    transcript. Times, never file order: the client writes some lines out of order
    (a queue line can sit seconds ahead of the one after it)."""
    working, ended = [], []
    with open(tpath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            t = dc.when(d.get("timestamp"))
            if t is None:
                continue
            if d.get("type") == "system" and d.get("subtype") in TURN_END:
                ended.append(t)
            elif d.get("type") == "assistant":
                working.append(t)
            elif d.get("type") == "user":
                k, _txt = dc.classify(d)
                if k == "interrupt":
                    ended.append(t)
                elif k == "prompt" or _tool_result(d):
                    # a local command (/model, /compact) starts no agent turn
                    working.append(t)
    return working, ended


def mid_turn_of(tpath, stamps):
    """For each time, whether the agent was at work then: True mid-turn, False
    idle, None when the transcript cannot tell (it never marks a turn's end, the
    time does not parse, the file does not read). Callers treat None like idle,
    so a doubt still warns."""
    if not stamps:
        return []                 # nothing to place: spare the read of the transcript
    try:
        working, ended = _turns(tpath)
    except OSError:
        return [None] * len(stamps)
    out = []
    for ts in stamps:
        t = dc.when(ts)
        if t is None or not ended:
            out.append(None)
            continue
        w = max((x for x in working if x <= t), default=None)
        e = max((x for x in ended if x <= t), default=None)
        out.append(w is not None and (e is None or w > e))
    return out


def _by_time(notes):
    """Notes in time order; an undated one keeps its place after the note before
    it, and a tie keeps the transcript's note first."""
    keyed, last = [], float("-inf")
    for i, n in enumerate(notes):
        o = dc.when(n[0])
        if o is not None:
            last = o.timestamp()
        keyed.append((last, i, n))
    return [n for _k, _i, n in sorted(keyed, key=lambda k: (k[0], k[1]))]


def session_notes(tpath, sid):
    """[(ts, text, channel, kept)] for one session, oldest first: the transcript's
    notes (kept "transcript"), plus the rows of the copy it does not hold (kept
    "ledger"), mostly typed mid-turn, which the client records nowhere since
    2.1.295. Matching is by text, as a multiset: when the same text was typed
    twice and only one landed, which row counts as the missing one is arbitrary.
    Raises OSError like notes_in()."""
    found = notes_in(tpath)
    extra = missing_from(ledger(sid) if sid else [], [t for _ts, t, _c in found])
    return _by_time([(ts, t, c, "transcript") for ts, t, c in found]
                    + [(str(r.get("ts") or ""), str(r.get("text") or ""),
                        r.get("channel") or "free", "ledger") for r in extra])


def _age_s(row, now):
    o = dc.when(row.get("ts"))
    return (now - o).total_seconds() if o else 0.0


def missing_from(rows, found_texts):
    """The rows of the copy a transcript does not hold (multiset: the same text
    typed twice needs two entries)."""
    have = collections.Counter(norm(t) for t in found_texts)
    lost = []
    for r in rows:
        k = norm(r.get("text"))
        if have[k] > 0:
            have[k] -= 1
        else:
            lost.append(r)
    return lost


def verify(last=20, now=None):
    """Hold the newest `last` rows of the copy (None = all) against their
    transcripts. Returns (checked, missing_rows, unchecked): unchecked counts rows
    whose transcript is unknown or gone — never "missing", since nothing was lost
    that a deleted file does not also explain. Each missing row is a copy carrying
    "mid_turn" (see mid_turn_of()): the client records no note typed mid-turn since
    2.1.295, so only the others say it changed again."""
    now = now or _utc_now()
    rows = [r for r in ledger() if _age_s(r, now) >= GRACE_S]
    if last:
        rows = rows[-last:]
    by_t = collections.OrderedDict()
    for r in rows:
        by_t.setdefault(r.get("transcript") or "", []).append(r)
    checked, lost, unchecked = 0, [], 0
    for tpath, rs in by_t.items():
        try:
            if not tpath:
                raise OSError("no transcript path")
            found = [t for _ts, t, _c in notes_in(tpath)]
        except OSError:
            unchecked += len(rs)
            continue
        checked += len(rs)
        miss = missing_from(rs, found)
        lost += [dict(r, mid_turn=v)
                 for r, v in zip(miss, mid_turn_of(tpath, [r.get("ts") for r in miss]))]
    return checked, lost, unchecked


def doctor_rows():
    """One row once this machine has a note, none before: a check with nothing
    to check is noise in a report read on every update. A note typed mid-turn
    missing from its transcript is expected (client 2.1.295+) and stays "ok"."""
    if not os.path.isfile(LEDGER):
        return []
    checked, lost, unchecked = verify()
    gone = (", %d unchecked (transcript gone)" % unchecked) if unchecked else ""
    real = [r for r in lost if not r.get("mid_turn")]
    mid = len(lost) - len(real)
    only = ("; %d typed mid-turn, kept only in logs/notes_typed.jsonl (Claude Code "
            "records none of those since 2.1.295; .note lists them)" % mid) if mid else ""
    if real:
        return [(".note recording", "warn",
                 "%d of the last %d note(s) not found in their transcripts, typed while "
                 "the agent was idle or at a time the transcript cannot place — missing, "
                 "or recorded in a shape .note does not read yet%s%s"
                 % (len(real), checked, only, gone),
                 "nothing is lost: .note lists them from logs/notes_typed.jsonl, but "
                 "readers of the transcript alone will not see them until .note is adapted")]
    if not checked:
        return [(".note recording", "ok", "nothing old enough to check yet" + gone, "")]
    if mid:
        return [(".note recording", "ok", "the last %d note(s): %d in their transcripts%s%s"
                 % (checked, checked - mid, only, gone), "")]
    return [(".note recording", "ok",
             "the last %d note(s) are all in their transcripts%s" % (checked, gone), "")]


# --- rendering ----------------------------------------------------------------------

def _usage_lines():
    w = max(len(u) for u, _ in USAGE)
    return ["   %s%-*s%s %s— %s%s" % (dc.ACT, w, u, dc.R, dc.TIME, d, dc.R)
            for u, d in USAGE]


def _head(rest):
    return "%s.note%s %s— %s%s" % (dc.ACT, dc.R, dc.TIME, rest, dc.R)


def _record(data, text, to_context):
    """A confirmation, never the text: the client shows and stores the prompt
    itself, and an echo would be a second copy."""
    try:
        _copy(data, text, to_context)
        warn = None
    except Exception as e:           # the transcript still gets it: say so, go on
        warn = "copy to claudette's log failed (%s) — the transcript still has it" % e
    n = len(ledger(str(data.get("session_id") or ""))) if warn is None else 0
    where = ("kept in the transcript" if to_context
             else "kept in the transcript, skipping the agent's context")
    lines = [_head(where + ((" · #%d this session" % n) if n else ""))]
    if warn:
        lines.append("%s%s%s" % (dc.RED, warn, dc.R))
    return lines


def _session_notes(data):
    """(notes, err, lost, mid) for this session: notes as session_notes(); lost,
    the notes only the copy holds that were not typed mid-turn and are past the
    grace window; mid, how many only the copy holds because they were."""
    tpath = str(data.get("transcript_path") or "")
    sid = str(data.get("session_id") or "")
    try:
        if not tpath:
            raise OSError("no transcript path from the client")
        notes = session_notes(tpath, sid)
    except FileNotFoundError:
        # Not written yet (see dc.read_transcript): the copy's rows are the notes,
        # and nothing holds them against anything — a note typed before the file
        # exists is not lost, it is not written yet either.
        return [(str(r.get("ts") or ""), str(r.get("text") or ""),
                 r.get("channel") or "free", "ledger") for r in ledger(sid)], "", [], 0
    except OSError as e:
        return [], "transcript unreadable: %s" % e, [], 0
    now = _utc_now()
    extra = [n for n in notes if n[3] == "ledger"]
    verdict = mid_turn_of(tpath, [n[0] for n in extra])
    lost = [n for n, v in zip(extra, verdict)
            if not v and _age_s({"ts": n[0]}, now) >= GRACE_S]
    return notes, "", lost, sum(1 for v in verdict if v)


def _listing(data):
    notes, err, lost, mid = _session_notes(data)
    if err:
        out = [_head(err)]
    elif not notes:
        out = [_head("no note in this session yet")]
    else:
        out = [_head("%d note(s) in this session, oldest first" % len(notes))]
        for i, (ts, text, _c, kept) in enumerate(notes, 1):
            # "*" takes the space after the number: the row keeps its width
            out.append("%2d%s%s%s%s %s%s%s" % (i, "*" if kept == "ledger" else " ",
                                               dc.TIME, dc.hm(ts), dc.R, dc.TEXT,
                                               dc.cut(norm(text), dc.WIDTH - 10), dc.R))
        if any(n[3] == "ledger" for n in notes):
            out.append("%s * only in claudette's log%s%s" % (
                dc.TIME, (" — %d typed mid-turn, which Claude Code no longer records"
                          % mid) if mid else "", dc.R))
    if lost:
        # Missing, or in a shape this reader does not know yet (as the queue
        # shape once was).
        out.append("%s%d note(s) typed here not found in the transcript (missing, or a shape "
                   ".note does not read yet)%s" % (dc.RED, len(lost), dc.R))
        out.append("%s   safe in logs/notes_typed.jsonl; run /claudette doctor%s"
                   % (dc.RED, dc.R))
    return out + _usage_lines()


def _show(data, n):
    notes, err, _lost, _mid = _session_notes(data)
    if err:
        return [_head(err)]
    if not 1 <= n <= len(notes):
        return [_head("no note %d — this session has %d" % (n, len(notes)))] + _usage_lines()
    ts, text, _c, _kept = notes[n - 1]
    d = dc.day(ts)
    out = ["%s.note %d%s %s— %s%s%s" % (dc.ACT, n, dc.R, dc.TIME,
                                        (d + " ") if d else "", dc.hm(ts), dc.R)]
    return out + ["%s%s%s" % (dc.TEXT, ln, dc.R) for ln in dc.wrapped(text, dc.WIDTH)]


def handle(data, tail, to_context):
    k = kind(tail)
    if k == "record":
        lines = _record(data, tail, to_context)
    elif k == "show":
        lines = _show(data, int(tail))
    elif k == "usage":
        lines = [_head("a note on this moment, for later review")] + _usage_lines()
    else:
        lines = _listing(data)
    if to_context:
        dc.emit_context_block(lines, ".note")
    else:
        dc.emit_block(lines)
