#!/usr/bin/env python3
"""Shared code under the dot-commands (declared in dot_registry.py).

Two halves. (1) Transcript parsing (promptSource, sidechains, harness wrappers,
compact boundaries). (2) The channel ENVELOPE — banner line, ANSI palette, emit_* —
so every command opens and ships the same way. Per-channel LAYOUT stays per command:
the app reflows what the terminal needs, so each channel has its own renderer.

Imported by hook_status_prompt.py only after a registry match - never at module
import time, so an ordinary prompt keeps paying nothing for it.
"""
import collections
import datetime
import json
import re
import textwrap

import dot_registry

# --- channel envelope -----------------------------------------------------------
# ANSI works in the block-reason channel, with an inverted default: the client
# paints the whole block orange, a reset RETURNS to orange, so neutral tones must
# be named explicitly and banner-orange is had by emitting no code at all.
#
# Semantic palette: the block's default text IS the client's hook orange — the same
# slot most themes draw yellow from — so ACTIONS (anything the user can type) take
# no code and inherit it; a yellow code would be invisible against it. Colors mark
# what is NOT an action.
#
# ACT IS ONLY ACT AT THE START OF A LINE. The client repaints the block PER LINE, so
# bare text inherits its colour only until our first escape code: after any
# "\x1b[0m" the rest of THAT line falls back to the terminal default. Bare text
# mid-line renders white, and no value can fix that — the fix is to name the colour.
# ACT_MID is that same colour written out, #FFC107; it cannot be read back from the
# client.
#
# So: ACT at a line start, ACT_MID anywhere else. Hardcoded on purpose; if the
# client's theme changes, this is the one line to update.
#
# YELLOW MEANS "YOU CAN TYPE THIS". ".hist" numbers its rows bare so the index wears
# the block colour and nothing else does; ".sess" and ".proj" follow. A tier or a
# state therefore may not be yellow — CYAN is for a state such as "recent".
ACT = ""              # actions: command names, usage forms — the hook's own orange
ACT_MID = "\x1b[38;2;255;193;7m"   # the same colour, for mid-line use
CYAN = "\x1b[96m"    # a state that must not be confused with a typeable number
OK = "\x1b[92m"       # confirmation: banner next to the ✅, "done" marks (92: 32 reads dim)
TITLE = "\x1b[94m"    # neutral structure: section titles, totals (94: 34 reads dim)
RED = "\x1b[91m"
TIME = "\x1b[2;37m"   # dim gray: meta, explanations
TEXT = "\x1b[37m"     # normal gray: content
R = "\x1b[0m"         # back to the client's orange, not to a neutral fg

# The client indents blocked-hook output by 2 and wraps it short of the terminal
# width; a wrapped tail is what makes this output read as an error dump. A 99-cell
# row wraps on the default 120-column Windows console. 94 is the ceiling a line may
# reach, and a row that has columns fills it exactly.
WIDTH = 94


# Appended by the envelope when a command flags its capture as NOISY — an
# unexpected tail that might have been meant as prose (".hist is the hidden file
# in this folder"). One wording, owned here, true because the dispatcher never
# strips a leading space into a match. Only for unexpected TAILS: an out-of-range
# index (".hist 99") is clearly an intended command and gets no hint.
ESCAPE_HINT = "Meant this as a prompt? Resubmit with a leading space."

# "?" IS THE HELP ARGUMENT, across every dot-command: ".cmd ?" answers that
# command's usage and does nothing else — never its default action, never a search
# for "?". A bare ".cmd" stays what each command makes it, usage or a default like
# a listing, so "?" is the one spelling that is the same everywhere. A deliberate
# request for help is not prose swallowed by accident,
# so it never gets ESCAPE_HINT: the dispatcher records the tail in TAIL, and the
# emitters below leave the hint off when it is HELP — one rule, however many
# handlers compute "noisy" on their own.
HELP = "?"
TAIL = None


def is_help(tail):
    return tail == HELP


def emit_block(lines, noisy=False):
    """Free channel: blocked prompt, 0 tokens, terminal only. The banner is the
    first line everywhere — the block renders orange and a first-time reader must
    not mistake it for an error. "✅" not a coloured "✔": the client repaints the
    leading glyph, so the only reliable green is the one baked into the emoji."""
    head = ("✅ %sNo tokens wasted by this claudette-dot-command "
            "(output skips context)%s" % (OK, R))
    lines = [head] + list(lines)
    if noisy and not is_help(TAIL):
        # RED, not dim: this is the one line that can rescue swallowed prose, and
        # it must not fade into the usage text above it.
        lines.append("%s%s%s" % (RED, ESCAPE_HINT, R))
    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def emit_context_block(lines, name, noisy=False):
    """Fenced twin of emit_block: the SAME lines the terminal renderer built,
    ANSI stripped, in one 4-backtick fence — monospace is the only layout the
    app cannot reflow, and horizontal scroll beats a reflowed board. This is
    the DEFAULT for the ..form of tabular commands; prose output (..recap) keeps
    emit_context, chosen case by case in the handler."""
    emit_context(["````"] + [ANSI.sub("", ln) for ln in lines] + ["````"],
                 name, noisy=noisy)


def emit_context(md_lines, name, noisy=False):
    """In-context channel: costs a turn, reaches the phone app. Verbatim echo is a
    model instruction, not a pipe — best-effort, unlike the deterministic block."""
    head = "✅ Turn intercepted by this claudette-dot-command (output in context)"
    md_lines = [head] + list(md_lines)
    if noisy and not is_help(TAIL):
        md_lines.append(ESCAPE_HINT)
    instr = ("Claudette %s request. Reply with EXACTLY these lines and nothing else "
             "- no preamble, no summary, keep the markdown:\n" % name
             ) + "\n".join(md_lines)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": instr}}))

# Which user entries are a PERSON typing, according to the client's own field. This
# allowlist alone is NOT enough - see classify().
HUMAN = ("typed", "queued", "suggestion_accepted")

# Tools whose call IS the write. Bash-driven writes (a script the agent ran) are
# invisible here by construction - the caller must say so rather than imply coverage.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# --- what a transcript entry can be ------------------------------------------------
# A slash command IS a person typing, but the client writes it with NO promptSource, so
# the HUMAN allowlist alone would drop every one: in wrapper form or as bare text.
#
# ANCHORED on purpose: a prompt that QUOTES "<command-name>" inside a code fence must
# not read as a command; a genuine one OPENS the entry.
CMD_AT_START = re.compile(r"^\s*<command-(?:name|message|args)>")
# The two tags arrive in either order, so never parse by position. <command-name> is
# always "/" + <command-message>: no alias exists.
CMD_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
CMD_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)

# A slash command with no wrapper at all (a bare "/compact"). Safe ONLY because this
# test runs AFTER the HUMAN one: a typed prompt that happens to open with a path
# ("/usr/bin/env ...") always carries promptSource "typed" and never reaches here.
BARE_CMD = re.compile(r"^\s*/([A-Za-z][\w:-]*)\s*(.*)$", re.S)
BARE_CMD_MAX = 400

INTERRUPT = re.compile(r"^\s*\[Request interrupted by user")
BASH_ECHO = re.compile(r"^\s*<(?:bash-input|bash-stdout|bash-stderr|local-command-)")
# The harness's own post-compact summary - generated, never typed.
COMPACT_BANNER = re.compile(
    r"^\s*This session is being continued from a previous conversation")

# Kinds a caller numbers. "compact" is a divider; "interrupt" never becomes a row.
NUMBERED = ("prompt", "command")

# ts:      the record's own timestamp - NOT sortable across kinds, see read_transcript
# one:     one line, for a listing
# raw:     the prompt's own line breaks kept, for a detail view
# kind:    see classify()
# stopped: the user hit ESC right after this, or this compaction never ran
# meta:    compactMetadata, for kind "compact" only
Entry = collections.namedtuple("Entry", "ts one raw kind stopped meta")


def strip_wrappers(text):
    """Drop harness wrappers but KEEP the prompt's own line breaks - the detail view
    needs them, and they are what makes a long prompt re-readable."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1 ", text, flags=re.S)
    text = re.sub(r"<command-(message|args)>(.*?)</command-\1>", r"\2 ", text, flags=re.S)
    text = re.sub(r"<local-command-[^>]*>.*?</local-command-[^>]*>", " ", text, flags=re.S)
    return re.sub(r"<[^>\n]{1,40}>", " ", text).strip()   # any stray wrapper tag


def flat(text):
    """One line per prompt for a listing: collapse every newline / whitespace run."""
    return re.sub(r"\s+", " ", strip_wrappers(text)).strip()


def when(ts):
    try:
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def hm(ts):
    o = when(ts)
    return o.strftime("%H:%M") if o else "--:--"


def day(ts):
    """"Mon 13 Jul", or None. Shown so a session spanning several days does not read
    as if 09:44 came after 15:27."""
    o = when(ts)
    return o.strftime("%a %d %b") if o else None


def day_of_epoch(secs):
    """day() for a file mtime — ".sess" groups by last activity, which is a stat,
    not a record stamp."""
    return datetime.datetime.fromtimestamp(secs).strftime("%a %d %b")


def wrapped(raw, w):
    """Wrap here, not at the client: an unexpected wrap is what made this output read
    as an error dump. Blank lines are kept (collapsed) so paragraphs survive."""
    res = []
    for src in raw.splitlines():
        src = src.rstrip()
        if not src:
            if res and res[-1]:
                res.append("")
            continue
        res.extend(textwrap.wrap(src, w) or [""])
    while res and not res[-1]:
        res.pop()
    return res


def cut(s, w):
    """At most w cells, "…" counted INSIDE them, so the budget a caller names is the
    one it gets (cut_counts_its_ellipsis_inside)."""
    if len(s) <= w:
        return s
    return s[:w - 1] + "…" if w >= 1 else ""


def mid(s, w):
    """At most w cells, "…" counted INSIDE them (unlike cut), keeping the start
    AND the end — for paths. The end is the folder that tells two rows apart and
    the start is the root that says where it lives. Tail-heavy, two thirds to
    the end, because the folder name is the identity."""
    if len(s) <= w:
        return s
    if w <= 1:
        return "…"[:max(0, w)]
    head = (w - 1) // 3
    return s[:head] + "…" + s[len(s) - (w - 1 - head):]


def fit_parts(parts, indent="   ", sep=" · ", width=WIDTH):
    """Join parts with sep into as few lines as fit width, breaking only BETWEEN
    parts, each line opened with indent. For a summary line whose clauses pile
    up (".sess"'s legend): a clause is never split, and the order reads the
    same across the break. Widths are
    counted without escape codes, so a coloured part costs only its text."""
    lines, cur = [], ""
    for p in parts:
        cand = cur + sep + p if cur else p
        if cur and len(ANSI.sub("", indent + cand)) > width:
            lines.append(cur)
            cand = p
        cur = cand
    if cur:
        lines.append(cur)
    return [indent + ln for ln in lines]


def hist_lines(entries, width=WIDTH):
    """The numbered prompt listing — shared by ".hist" and ".sess N".

    Shared so ".sess N" and ".hist" render prompts with one layout. The
    numbering contract belongs to the renderer, not to its callers — dividers
    are never rows, so "N" keeps meaning "the Nth thing I typed" in both.

    width is the WHOLE line, prefix included.
    """
    out, n, last_day = [], 0, None
    for e in entries:
        # A compaction is a divider, not an entry: "N" on one would have nothing
        # to show, and the boundary record carries no prompt text.
        if e.kind == "compact":
            h = "   ─── %s " % compact_label(e)
            out.append("%s%s%s%s" % (TIME, h, "─" * max(0, width - len(h)), R))
            continue
        d = day(e.ts)
        if d and d != last_day:
            h = "   ══ %s " % d
            out.append("%s%s%s%s" % (TIME, h, "═" * max(0, width - len(h)), R))
            last_day = d
        n += 1
        # "⏹" sits between time and prompt, not at the tail: the 1-char shove of
        # the prompt column is what makes an interrupt scannable. The prompt gets
        # what the prefix leaves, measured, so row 100 and a "⏹" still fit.
        stop = "⏹" if e.stopped else ""
        pre = len("%2d %s%s " % (n, hm(e.ts), stop))
        out.append("%2d %s%s%s%s %s%s%s"
                   % (n, TIME, hm(e.ts), R, stop, TEXT, cut(e.one, width - pre), R))
    return out


def tokens(n):
    """"315k" - a compaction's before/after never needs more than one significant digit."""
    try:
        n = int(n)
    except Exception:
        return "?"
    return "%dk" % round(n / 1000.0) if n >= 1000 else str(n)


def entry_text(d):
    c = (d.get("message") or {}).get("content")
    if not isinstance(c, list):
        return str(c or "")
    txt = "\n".join(b.get("text", "") for b in c
                    if isinstance(b, dict) and b.get("type") == "text")
    return txt if txt.strip() else ("[attachment]" if c else "")


def classify(d):
    """(kind, text) for ONE transcript record - the single place that decides what a
    person actually did, which is why ".hist" and ".done" can never disagree.

        prompt     a typed prompt                        -> numbered
        command    a slash command they typed            -> numbered
        compact    a compact_boundary record             -> divider; meta has the tokens
        interrupt  an ESC                                -> folds onto the row before it
        junk       recognised, deliberately dropped
        unknown    human-shaped and NOT recognised       -> the regression alarm

    "unknown" must stay at 0; dot_transcript_check.py is what watches it. It exists
    because this whole classifier is a whitelist over a format nobody here controls
    (CLAUDETTE_DESIGN.md, "Reading a transcript: the classifier, and why it has an alarm").
    """
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
        # The AUTHORITY on a compaction: ONE record per real one (the client types two
        # "/compact" entries per compaction), carrying its own correct timestamp - and
        # it is written for an AUTO compaction too, which types no "/compact" at all.
        return "compact", ""
    if d.get("type") != "user" or d.get("isSidechain"):
        return "junk", ""
    if d.get("isMeta") or "toolUseResult" in d:
        return "junk", ""            # harness injection (a skill's own body); tool result
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                   for b in c):
        return "junk", ""
    ps = d.get("promptSource")
    if ps in ("system", "sdk"):
        return "junk", ""            # <task-notification>; a program typing, not a person
    txt = entry_text(d)
    if BASH_ECHO.match(txt) or COMPACT_BANNER.match(txt):
        return "junk", ""
    if INTERRUPT.match(txt):
        return "interrupt", ""
    if CMD_AT_START.match(txt):
        m, a = CMD_NAME.search(txt), CMD_ARGS.search(txt)
        arg = a.group(1).strip() if a else ""
        name = ("/" + m.group(1).strip().lstrip("/")) if m else "/?"
        return "command", name + ((" " + arg) if arg else "")
    if ps in HUMAN:
        return "prompt", txt
    m = BARE_CMD.match(txt) if len(txt) <= BARE_CMD_MAX else None
    if m:
        rest = m.group(2).strip()
        return "command", "/" + m.group(1) + ((" " + rest) if rest else "")
    if not txt.strip():
        return "junk", ""
    return "unknown", txt


def _is_compact_cmd(e):
    return e.kind == "command" and e.one.split()[:1] == ["/compact"]


def fold_compacts(entries):
    """Collapse the "/compact" the user typed into the boundary record that proves it ran.

    Neither "drop the duplicate" nor "look ahead one row" is enough: the client writes
    TWO "/compact" entries per real compaction and puts them
    on BOTH sides of the boundary. So the unit is the gap between two prompts - inside
    it a boundary wins outright, and a "/compact" with no boundary anywhere in that gap
    is a compaction that never happened, which is worth one line of its own.
    """
    out, run = [], []

    def flush():
        cmds = [e for e in run if _is_compact_cmd(e)]
        boundaries = [e for e in run if e.kind == "compact"]
        for e in run:
            if not _is_compact_cmd(e):
                out.append(e)
        if cmds and not boundaries:
            # Still a divider, not an entry: ".hist N" on it would have nothing to
            # show either. kind "compact" with no meta is what renders the failure.
            out.append(cmds[0]._replace(kind="compact", stopped=True, meta=None))
        del run[:]

    for e in entries:
        if e.kind == "prompt":
            flush()
            out.append(e)
        else:
            run.append(e)
    flush()
    return out


def numbered(entries):
    """Only the rows a listing numbers - what ".hist N" indexes into."""
    return [e for e in entries if e.kind in NUMBERED]


def compact_label(e):
    """The divider's text. "manual" is never printed - it is the command the user just
    typed, and only an AUTO compaction is news."""
    if e.stopped or not e.meta:
        return "/compact — did not complete"
    meta = e.meta
    pre, post = meta.get("preTokens"), meta.get("postTokens")
    bits = []
    if isinstance(pre, int) and isinstance(post, int) and pre > 0:
        head = "/compact %s → %s" % (tokens(pre), tokens(post))
        bits.append("−%d%%" % round(100 - 100.0 * post / pre))
    else:
        head = "/compact"
    if (meta.get("trigger") or "manual") != "manual":
        bits.append(str(meta["trigger"]))
    ms = meta.get("durationMs")
    if isinstance(ms, (int, float)) and ms >= 1000:
        bits.append("%ds" % round(ms / 1000.0))
    return head + ("  (%s)" % ", ".join(bits) if bits else "")


def compact_footer(entries):
    """One line, only when there was a compaction: what this session dropped in total.
    cumulativeDroppedTokens is the client's own running count, so take the last one."""
    done = [e for e in entries if e.kind == "compact" and e.meta and not e.stopped]
    if not done:
        return ""
    drop = done[-1].meta.get("cumulativeDroppedTokens")
    secs = sum((e.meta.get("durationMs") or 0) for e in done) / 1000.0
    tail = ", %s dropped" % tokens(drop) if isinstance(drop, int) else ""
    return "%d compaction(s)%s, %dm%02ds spent" % (len(done), tail, secs // 60, secs % 60)


def read_transcript(tpath, want_tools=False):
    """(entries, tools, err) from a session JSONL.

    entries: Entry rows in FILE order, never timestamp order - a "/compact" entry
             carries a stamp EARLIER than the prompt it follows, so sorting by time
             reorders the session. Only kind in NUMBERED is numbered by a caller.
             An interruption never becomes a row of its own: it sets .stopped on the
             row before it, which is the only question it answers - did that finish?
    tools:   [{name, input, ts, sidechain}] for every assistant tool_use, subagents
             included - a file a subagent wrote is still this session's work.
             Empty unless want_tools, since ".hist" pays nothing for what it ignores.
    """
    entries, tools, err = [], [], ""
    try:
        with open(tpath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("type") == "assistant":
                    if not want_tools:
                        continue
                    for b in (d.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tools.append({"name": b.get("name"),
                                          "input": b.get("input") or {},
                                          "ts": d.get("timestamp", ""),
                                          "sidechain": bool(d.get("isSidechain"))})
                    continue
                kind, txt = classify(d)
                if kind in ("junk", "unknown"):
                    continue
                if kind == "interrupt":
                    if entries:
                        entries[-1] = entries[-1]._replace(stopped=True)
                    continue
                if kind == "compact":
                    entries.append(Entry(d.get("timestamp", ""), "", "", "compact",
                                         False, d.get("compactMetadata") or {}))
                    continue
                if kind == "command":
                    # classify() already rendered it as "/name args"; strip_wrappers
                    # would give back "claudette /claudette prefs" - the display name
                    # repeated - because <command-message> carries it too.
                    entries.append(Entry(d.get("timestamp", ""), txt, txt,
                                         "command", False, None))
                    continue
                one = flat(txt)
                if not one:
                    continue
                # Only a DECLARED dot-command invocation is hidden (the "..x" form
                # does land in the transcript), not every prompt that starts with a
                # dot (".claude/settings.json is broken"). Matched on the RAW text,
                # not the flattened line: flattening would join ".hist\npaste" and
                # " .hist 5" into commands.
                if txt[:1] in ".…" and dot_registry.match(txt):
                    continue
                entries.append(Entry(d.get("timestamp", ""), one, strip_wrappers(txt),
                                     "prompt", False, None))
    except FileNotFoundError as e:
        # The client names the transcript before it writes it: a session whose
        # every prompt so far was a dot-command has no file yet. Nothing written
        # yet is not an error; no path at all still is.
        if not tpath:
            err = "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    return fold_compacts(entries), tools, err
