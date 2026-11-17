#!/usr/bin/env python3
"""The dot-command registry and its matcher — the ONE place a command is declared.

Dependency-free on purpose: both the dispatcher (hook_status_prompt.py) and the
transcript recall filter (dot_common.py) need the declared names, and a registry
living inside either would make the other import it circularly.

The channel is part of the NAME, not a modifier (CLAUDETTE_DESIGN.md "Dot-commands: the
framework"):

    .name [args]      free channel: 0 tokens, blocked prompt, terminal only
    ..name [args]     in-context channel: costs a turn, reaches the phone app
    .                 free index          ..    in-context index

so every ".x" is structurally free and every "..x" structurally a turn. A command
with free=False exists only behind ".." — its "." form gets a zero-token pointer
from the dispatcher, which is how the contract survives context-only commands.

Adding a command = one REGISTRY row + one scripts/dot_<module>.py exposing

    handle(data, tail, to_context)

(data = the hook's stdin dict; tail = the argument string exactly as typed, bar
its outer whitespace; to_context = which channel). The handler answers on
that channel for EVERY input — valid, invalid and out-of-range arguments included —
and never falls through to the model. The dispatcher imports the module only after
a match, so a prompt that is not a dot-command never pays for any handler.

"!" IS THE SIDE-EFFECT SIGIL, across every dot-command. An argument that changes
anything outside claudette's own logs — spawns a process, writes a file the user can
see, sends something — is spelled "!word" (or a bare "!" for a confirm). So:

    an invocation with no "!" in it cannot change anything.

Two exceptions: ".close" and ".new" act on their bare name, for speed, and their
index line opens with "WARN:" instead. ".close" acts only with no argument; ".new"
takes only a title. The bang marks the ARGUMENT, never the command: ".sess" stays
read-only, ".sess !r 7" acts. "-" is deliberately unspent: it reads as a getopt
switch and promises a flag contract these grammars do not keep.

".note text" leaves something DURABLE without a bang: the hook itself writes only
claudette's own log (the copy in logs/notes_typed.jsonl); the transcript keeps the
note because the client records every prompt it is handed, except one typed
mid-turn since client 2.1.295, which only the copy holds (CLAUDETTE_DESIGN.md
".note: a remark the agent's context never holds").

EVERY CHARACTER TYPED REACHES THE HANDLER: a trailing "." or "!" is typed on
purpose, so it is the handler's to answer (usually with usage).

"?" IS THE HELP ARGUMENT: ".name ?" must answer that command's usage and nothing
else (dot_common.HELP; tests/test_dot_commands.py pins it for every row).

LINE BREAKS ARE DECLARED, flag "multiline". A multi-line prompt is a command only
for a row that declares it; for any other row it reaches the model. That decision
lives here, not in the handlers, because a handler must answer and can never hand a
prompt back to the model. It is also the safe default: ".sess" reads a "!" word
anywhere in its tail, so a pasted ".sess !r 7 12" would reopen sessions. The "!"
guarantee above holds for text typed; this flag keeps it true for text pasted.
"""
import re

# (names, module, usage, help, flags)
#   names:  first is primary (index sort key); the rest are aliases, shown by the
#           index automatically. Matched exactly, case-insensitively, longest first.
#   usage:  SHORT canonical form for the index column (<= USAGE_MAX chars) — the
#           full grammar belongs to the command's own usage answer (".cmd ?" gets
#           it for free: "?" is an invalid argument).
#   flags:  free=False -> in-context only (usage should show the ".." form).
#           multiline=True -> the tail may span lines, see the docstring.
REGISTRY = (
    (("autodone", "auto-done"), "dot_autodone", ".autodone [on|off] [all]",
     "idle done-watchdog switch / state", {}),
    (("chart",), "dot_chart", ".chart",
     "usage chart: the link, and if it is current", {}),
    (("close",), "dot_close", ".close",
     "WARN: ends this session, closes its terminal window", {}),
    (("context", "ctx"), "dot_context", ".context",
     "context ledger: files, baseline, exact totals", {}),
    (("done",), "dot_done", ".done",
     "session-close facts: recall + persistence board", {}),
    (("hist", "history"), "dot_hist", ".hist [N]",
     "prompt history, oldest first; N = one in full", {}),
    (("new",), "dot_new", ".new [title]",
     "WARN: opens a new session window here, titled", {}),
    (("note",), "dot_note", ".note [text|N]",
     "note this moment for later review; skips the agent's context", {"multiline": True}),
    (("proc", "procs", "process", "processes"), "dot_proc", ".proc [:all]",
     "processes Claude started", {}),
    (("proj", "projects"), "dot_proj", ".proj [N|word|!new [N]]",
     "project folders: sessions, open a new one", {}),
    (("recap",), "dot_recap", "..recap",
     "concise conversation summary + snapshot", {"free": False}),
    (("sess", "sessions"), "dot_sess", ".sess [N|word|!r|!model]",
     "sessions: list, search, reopen in a window", {}),
    (("status", "s"), "dot_status", ".status",
     "status-line snapshot: session + rate limits", {}),
)

USAGE_MAX = 26          # leaves a help text (aliases included) >= 62 of WIDTH 94

for _names, _mod, _usage, _help, _flags in REGISTRY:
    for _n in _names:
        # The matcher takes the maximal [A-Za-z][\w-]* run after the dots, so a
        # name must be exactly such a run (and not end in "-", which would let
        # ".name-x" match as "name" + junk instead of passing through).
        assert re.fullmatch(r"[A-Za-z][\w-]*[\w]", _n) or re.fullmatch(r"[A-Za-z]", _n), _n
    assert len(_usage) <= USAGE_MAX, _usage

_BY_NAME = {n: e for e in REGISTRY for n in e[0]}

# The command word after the leading dots: its maximal extent is the boundary
# (".hist19" and ".histogram" are not ".hist"; ".hist!" is).
_WORD = re.compile(r"([A-Za-z][\w-]*)")


class Match(object):
    """entry is a REGISTRY row, or None for the index command ("." / "..")."""
    __slots__ = ("entry", "tail", "to_context")

    def __init__(self, entry, tail, to_context):
        self.entry, self.tail, self.to_context = entry, tail, to_context


def _leading_dots(p):
    """Count the leading dot-run, '…' worth 3; return (count, rest)."""
    n = 0
    for i, ch in enumerate(p):
        if ch == ".":
            n += 1
        elif ch == "…":
            n += 3
        else:
            return n, p[i:]
    return n, ""


def match(prompt):
    """Match a typed prompt against the registry.

    Returns None (not a dot-command: the prompt passes through untouched) or a
    Match. Guarantees, in order:
      - the sigil is the LITERAL first character: a leading space is the escape
        hatch and is never stripped into a match;
      - a prompt containing a newline is never a command (pasted content),
        except for a row flagged multiline;
      - dots-only prompt -> index: 1 dot free, 2+ in context;
      - the word after the dots must equal a declared name, else None
        (".claude/x", "../scripts/x.py", ".gitignore", ".histogram of x");
      - 1 leading dot -> free, 2+ -> in context ("...hist" folds to context);
      - the tail is every character after the name but its outer whitespace
        ("\\r\\n" becomes "\\n"): ".hist." hands "." to .hist, which answers usage.
    """
    p = prompt.rstrip()
    if not p or p[0] not in ".…":
        return None
    dots, rest = _leading_dots(p)
    if not rest.strip():
        if rest:                     # inner spaces after the dots: not the index
            return None
        return Match(None, "", dots >= 2)
    m = _WORD.match(rest)
    if not m:
        return None                  # "./x", "../y", ".!?", ".\nx" — never a command
    entry = _BY_NAME.get(m.group(1).lower())
    if entry is None:
        return None
    if "\n" in p and not entry[4].get("multiline"):
        return None
    return Match(entry, rest[m.end():].replace("\r\n", "\n").strip(), dots >= 2)
