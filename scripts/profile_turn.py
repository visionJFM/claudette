#!/usr/bin/env python3
"""Attribute a turn's wall-clock time from a Claude Code session transcript.

Single purpose: answer "where did those 3 minutes go?" for one turn, splitting
wall clock into model latency, tool execution, and round trips.

Transcripts live at ~/.claude/projects/<project-key>/<session-id>.jsonl, one JSON
object per line, each with an ISO `timestamp`. A turn is the span from a user
entry to the entry before the next user prompt. Deltas between consecutive
entries are attributed by the KIND of the later entry:
  assistant row  -> model time (reasoning + emitting the tool call or the text)
  tool_result    -> tool execution
Round trips = user/tool_result -> assistant transitions. Each one re-sends the
whole context, which is why a long skill body is a per-round-trip tax, not a
one-time one.

ANTI-GOALS:
  - read-only; never edits or truncates a transcript
  - no per-token cost model: this measures LATENCY, not spend (see claude_limits.py)
  - never prints message CONTENT, only kinds and sizes - transcripts hold the
    user's work and this tool's output tends to get pasted into chats

CLI:
    python profile_turn.py                       # newest transcript, last turn
    python profile_turn.py --session <id>        # by session id, any project
    python profile_turn.py --file <path.jsonl>
    python profile_turn.py --grep claudette-done # last turn whose prompt matches
    python profile_turn.py --list                # turns in the transcript
"""
import datetime, glob, json, os, sys

PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _ts(row):
    t = row.get("timestamp")
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_transcript(session=None, path=None):
    if path:
        return path
    pats = os.path.join(PROJECTS, "*", "%s.jsonl" % session) if session \
        else os.path.join(PROJECTS, "*", "*.jsonl")
    hits = glob.glob(pats)
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def prompt_text(row):
    """The user's own text for a row, '' for tool results and injected context."""
    m = row.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        return "\n".join(parts)
    return ""


def turn_starts(rows):
    """Indices of user entries that begin a turn (a real prompt, not a tool result)."""
    out = []
    for i, r in enumerate(rows):
        if r.get("type") != "user" or not _ts(r):
            continue
        txt = prompt_text(r)
        if not txt.strip():
            continue
        if out and out[-1][0] == i - 1 and rows[i - 1].get("type") == "user":
            continue                      # skill/command expansion of the same prompt
        out.append((i, txt))
    return out


def classify(row):
    """(kind, detail, chars) for one entry."""
    m = row.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return row.get("type"), "text", len(c)
    if not isinstance(c, list):
        return row.get("type"), "-", 0
    kinds, chars = [], 0
    for b in c:
        if not isinstance(b, dict):
            continue
        k = b.get("type")
        if k == "tool_use":
            kinds.append("tool:%s" % b.get("name"))
            chars += len(json.dumps(b.get("input", {}), ensure_ascii=False))
        elif k == "tool_result":
            kinds.append("result")
            cc = b.get("content")
            chars += len(cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False))
        elif k == "thinking":
            kinds.append("thinking")
            chars += len(b.get("thinking", "") or "")
        elif k == "text":
            kinds.append("text")
            chars += len(b.get("text", "") or "")
        else:
            kinds.append(str(k))
    return row.get("type"), "+".join(kinds) or "-", chars


def profile(rows, start, end, show_steps=True):
    seq = [r for r in rows[start:end] if _ts(r) and r.get("type") in ("assistant", "user", "system")]
    if len(seq) < 2:
        print("  (nothing timed in this turn)")
        return
    # The turn ends at its last assistant entry; later rows (system/hook rows can
    # land minutes later) are the user reading, not turn time.
    last_a = max((i for i, r in enumerate(seq) if r.get("type") == "assistant"), default=len(seq) - 1)
    j = last_a
    while j + 1 < len(seq) and (_ts(seq[j + 1]) - _ts(seq[j])).total_seconds() < 10:
        j += 1
    idle = (_ts(seq[-1]) - _ts(seq[j])).total_seconds() if j < len(seq) - 1 else 0.0
    seq = seq[:j + 1]
    t0, t1 = _ts(seq[0]), _ts(seq[-1])
    model = tools = other = 0.0
    tool_calls = round_trips = 0
    injected = 0
    prev = None
    lines = []
    for r in seq:
        t = _ts(r)
        typ, detail, chars = classify(r)
        d = (t - prev).total_seconds() if prev else 0.0
        if prev is not None:
            if typ == "assistant":
                model += d
            elif "result" in detail:
                tools += d
            else:
                other += d
        if typ == "assistant":
            tool_calls += detail.count("tool:")
        prev = t
        lines.append("  %8.1fs  %-9s %-28s %8d ch" % (d, typ, detail[:28], chars))
        if typ == "user" and "result" not in detail:
            injected += chars
    # round trips: count assistant blocks that follow a non-assistant entry
    last = None
    for r in seq:
        typ = r.get("type")
        if typ == "assistant" and last != "assistant":
            round_trips += 1
        last = typ
    span = (t1 - t0).total_seconds()
    if show_steps:
        print("\n".join(lines))
    tot = span or 1.0
    print("  " + "-" * 62)
    print("  span            %7.1fs" % span)
    print("  model           %7.1fs   %4.0f%%   (reasoning + emitting calls and text)" % (model, 100 * model / tot))
    print("  tool execution  %7.1fs   %4.0f%%   (%d calls)" % (tools, 100 * tools / tot, tool_calls))
    if other:
        print("  other           %7.1fs   %4.0f%%" % (other, 100 * other / tot))
    print("  round trips     %7d           (each re-sends the whole context)" % round_trips)
    print("  injected text   %7d ch        (prompt + skill body, re-sent per round trip)" % injected)
    if round_trips:
        print("  -> %.0fs of model latency per round trip on average" % (model / round_trips))
    if idle:
        print("  (+%.0fs of post-turn idle excluded)" % idle)


def main(argv):
    session = path = grep = None
    do_list = False
    steps = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--session" and i + 1 < len(argv):
            session = argv[i + 1]; i += 1
        elif a == "--file" and i + 1 < len(argv):
            path = argv[i + 1]; i += 1
        elif a == "--grep" and i + 1 < len(argv):
            grep = argv[i + 1]; i += 1
        elif a == "--list":
            do_list = True
        elif a == "--totals":
            steps = False
        else:
            sys.stderr.write(__doc__); return 2
        i += 1

    tpath = find_transcript(session, path)
    if not tpath or not os.path.isfile(tpath):
        sys.stderr.write("no transcript found (looked under %s)\n" % PROJECTS); return 1
    rows = load(tpath)
    starts = turn_starts(rows)
    print("transcript: %s\n  %d entries, %d turns" % (tpath, len(rows), len(starts)))
    if not starts:
        return 1

    if do_list:
        for idx, (i0, txt) in enumerate(starts):
            print("  [%3d] %s  %s" % (idx, (_ts(rows[i0]) or "").strftime("%H:%M:%S"),
                                      " ".join(txt.split())[:90]))
        return 0

    chosen = None
    if grep:
        for i0, txt in starts:
            if grep.lower() in txt.lower():
                chosen = i0
        if chosen is None:
            sys.stderr.write("no turn matching %r\n" % grep); return 1
    else:
        chosen = starts[-1][0]
    nxt = next((i0 for i0, _ in starts if i0 > chosen), len(rows))
    print("  turn at %s: %s\n" % ((_ts(rows[chosen]) or "").strftime("%H:%M:%S"),
                                  " ".join(prompt_text(rows[chosen]).split())[:80]))
    profile(rows, chosen, nxt, show_steps=steps)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
