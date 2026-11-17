#!/usr/bin/env python3
""".proj — every project folder Claude was ever launched in, and a way back into one.

".sess" answers "which SESSION was that"; ".proj" answers the question one level
up: "WHERE have I been working, and how much". Same data, different unit of
account — the folders under ~/.claude/projects/ are exactly the directories a
"claude" was started in, one per project, and the count of transcripts inside one
is the count of sessions launched there.

GRAMMAR — the frozen namespaces of dot_registry.py, nothing new invented:

    digits      position    .proj 3          that project's sessions
    :word       scope       :20 :*
    !word       action      .proj !new 3 7   a new session in each, one window
                            .proj !opus 3    the same, on a named model
                            .proj !sonnet    no project named = the one you are in
    anything    content     .proj lab        substring AND over the path
                            .proj lab !new   content NAMES it, when it matches one

"!" is the side-effect sigil: ".proj" alone cannot change anything.

AN ACTION WITH NO PROJECT MEANS HERE. ".proj !sonnet" opens another window on the
project already on screen, at its STARTUP FOLDER, never at the live cwd — see
here_project() and handle().

THE STARTUP FOLDER IS THE ONLY PATH THAT MATTERS. It is the directory claude was
launched in: the sandbox root, and the key its transcripts are filed under. A "cd"
changes neither, so the live cwd is irrelevant to which project a session is in.
Everything here keys off the project folder name: "←you", the bare-action target,
and path_of()'s check that a recorded cwd re-encodes to the folder it was found in.

The window options ":N" and ":*" are the whole scope namespace; ":here" and ":live"
answer "unknown option".

WORDS WITH AN ACTION ARE THE IDENTITY FORM — what "#hex" is to ".sess". They must
match exactly ONE project; zero and many are both answered rather than guessed at,
because picking a best match would make a launch depend on a ranking nobody saw.
It is also the only way to act with no listing on disk.

".proj N" IS ".sess :here" FOR A PROJECT YOU ARE NOT IN. It renders with ".sess"'s
rows and ".sess"'s GLOBAL ranks (sparse: 1, 4, 9, 23) and writes the SESSION map,
so ".sess 4" and ".sess !r 4" answer from it. The project map is left untouched,
so ".proj !new 2" still works right after drilling in.

THE MODEL IS SPELLED AS THE ACTION, not as a second argument: "!new", "!opus",
"!sonnet", "!haiku" and "!fable" are five spellings of the one action ".proj" has,
so ".sess"'s "one action at a time" rule holds unchanged and no non-scope enters
the scope namespace. "!new" passes no --model at all.

LISTED ALPHABETICALLY BY PATH, so the numbers hold still: the order changes only
when a NEW folder is first used, which lets a number be typed from memory instead
of read. The order says nothing about importance, so the EMPHASIS is colour:
green = a session open now, cyan = used within 7 days, dim = the folder is gone
(dim rather than red: dead weight to skip past). NOT yellow for recent: yellow is
reserved for the numbers you can type into the next command, which is ".hist"'s
contract. Each tier prints its keyword in the last column too, so the row reads
correctly with colour stripped. See tier().

THE FIRST LINE NAMES ITS SUBJECT IN WHITE, before anything about arrangement:
"11 project folders" for a listing, the path and "25 sessions" for one project.

":N" stays DEPTH IN RECENCY — "the 20 projects I used most recently, listed by
path" — a meaningful filter and a bound on the work (mtime is a stat; the path is
a read). group() is the recency order, alpha() is the display order, and every
rank anywhere comes from alpha().

COST: grouping is free (the scan stats, never opens), so the count, the last
activity and the total size cost nothing. The PATH does need a read — the folder
name is a one-way encoding (dot_sess.slug_of) and cannot be decoded back — so it
is read from the newest transcript of each project: one head-read per PROJECT,
not per session.

Indices resolve against the LAST LISTING (logs/proj_index/), never a fresh scan,
and for the same correctness reason as ".sess !r": launching into project 3
writes a transcript there, which makes it project 1 and shifts everything below
it. Its OWN directory, never sess_index — one map per command, or ".proj" would
silently answer a ".sess !r N".
"""
import os
import re
import time

import dot_common as dc
import dot_sess as ds
import paths
import sess_launch

WIDTH = dc.WIDTH
DEFAULT_WINDOW = 50       # depth, in projects; the footer always says which ran
COL_PATH = 60             # see row(): WIDTH minus every fixed cell, "  NN open" included
# What the last listing showed: {rank: slug}. Same contract and same pruning as
# sess_index (CLAUDETTE_LOGS.md), a separate directory on purpose.
INDEX_DIR = paths.log("proj_index")

# Every spelling of the one action. None = pass no --model, so the machine's own
# default (settings.json / the /model choice) wins. The models come from dot_sess,
# which is also where ".sess !sonnet" reads them: one table for both commands.
ACTIONS = dict({"new": None, "n": None}, **ds.MODELS)
MODEL_OK = ds.MODEL_OK                            # what may reach a command line

ACT, OK, TITLE, RED, TIME, TEXT, R = (dc.ACT, dc.OK, dc.TITLE, dc.RED,
                                      dc.TIME, dc.TEXT, dc.R)
CYAN = dc.CYAN
USAGE = [
    "%s.proj%s %s— usage:%s" % (ACT, R, TIME, R),
    "   %s.proj%s              %sproject folders, alphabetical; colour = open / recent / gone%s"
    % (ACT, R, TIME, R),
    "   %s.proj 2%s            %sits sessions, newest first — then %s.sess N%s%s%s"
    % (ACT, R, TIME, R + ACT, R + TIME, "", R),
    "   %s.proj lab%s          %sfilter by path (every word must match)%s"
    % (ACT, R, TIME, R),
    "   %s.proj !new 3 7%s     %sa new session in each, one new window each%s"
    % (ACT, R, TIME, R),
    "   %s.proj !opus 3%s      %ssame, on a named model (%s!sonnet !haiku !fable%s%s)%s"
    % (ACT, R, TIME, R + ACT, R + TIME, "", R),
    "   %s.proj !sonnet%s      %sno project named — a new session where you are "
    "standing%s" % (ACT, R, TIME, R),
    "   %s.proj lab !sonnet%s  %sname it by a word instead of a number — it must "
    "match one%s" % (ACT, R, TIME, R),
    "   %s:20 :*%s %sdepth — how many of the most recently used are read%s"
    % (ACT, R, TIME, R),
    "   %s!%s %sis the only thing here with a side effect; without one nothing "
    "changes%s" % (ACT, R, TIME, R),
]


# ----------------------------------------------------------------- arguments

class Args(object):
    __slots__ = ("window", "terms", "err", "action", "model", "idxs", "idx",
                 "name", "label")

    def __init__(self):
        self.window = DEFAULT_WINDOW
        self.terms, self.err = [], ""
        self.action, self.model, self.idxs = None, None, []
        self.idx = None                  # a project to open up, no action
        self.name, self.label = None, None   # .new: the title, and its own label


# .new's title is the one piece of typed text that reaches a command line
# (`claude --name "<title>"`), through cmd, PowerShell, bash, zsh, wt and
# AppleScript. So it is held to characters none of them reads as syntax inside
# double quotes, rather than escaped for each parser (sess_launch: "prefer
# removing a parser to escaping for it"). No leading "-": an option to claude.
NAME_OK = re.compile(r"(?=[^-])[\w .,:'()-]{1,80}\Z")


def parse(tail):
    """Tail -> Args. Every rejection is a usage answer, never a pass-through."""
    a = Args()
    win_seen = False
    toks = [(m.group(1) is not None, m.group(2) if m.group(2) is not None else m.group(1))
            for m in ds._TOK.finditer(tail)]

    # Read FIRST: an action is what makes a digit mean a project rather than an
    # error, exactly as in ".sess".
    for quoted, tok in toks:
        if quoted or not tok.startswith("!"):
            continue
        word = tok[1:].lower()
        if not word:
            a.err = "! on its own does nothing here — !new opens a session"
            return a
        if word not in ACTIONS:
            a.err = ("unknown action !%s — !new, or a model: %s"
                     % (tok[1:], " ".join("!" + k for k in ("opus", "sonnet", "haiku"))))
            return a
        if a.action and word != a.action:
            a.err = "one action at a time"
            return a
        a.action, a.model = word, ACTIONS[word]

    for quoted, tok in toks:
        if quoted:
            a.terms.append(tok.lower())
            continue
        if tok.startswith("!"):
            continue
        if tok.startswith("#"):
            # Projects have no id: the folder name is a one-way encoding of a
            # path, so there is nothing stable and short to name one by.
            a.err = "projects have no id — name one by its number, or by a word in its path"
            return a
        if tok.startswith(":"):
            opt = tok[1:].lower()
            if opt in ("*", "all"):
                opt_win = 0
            elif opt.isdigit() and int(opt) > 0:
                opt_win = int(opt)
            else:
                # ":here" and ":live" land here on purpose: the scope namespace
                # is depth and nothing else.
                a.err = "unknown option :%s" % opt
                return a
            if win_seen:
                a.err = "two windows given — pick one of :N or :*"
                return a
            a.window, win_seen = opt_win, True
            continue
        if tok.isdigit():
            if int(tok) < 1:
                a.err = "project 0 does not exist — the list starts at 1"
                return a
            if a.action:
                a.idxs.append(int(tok))
            elif a.idx is None:
                a.idx = int(tok)
            else:
                # Two numbers would have to mean project-then-session, and a
                # session already has a command: the detail view hands out
                # ".sess" numbers precisely so it does not need a second one.
                a.err = ("one project at a time — %s.sess %s%s %sshows a session "
                         "from the list %s.proj %d%s %sprints"
                         % (ACT, tok, R + TIME, "", ACT, a.idx, R + TIME, ""))
                return a
            continue
        a.terms.append(tok.lower())

    if a.action:
        # Terms WITH an action are allowed, and they are the identity form: a
        # filter matching exactly one project names it the way ".sess !r #ad2"
        # names a session, needs no listing to resolve against, and cannot go
        # stale when a launch renumbers the list. Ambiguity is answered by
        # showing the candidates, never by picking one (act()).
        if a.terms and a.idxs:
            a.err = "name projects by number or by word, not both"
        elif win_seen:
            a.err = "scope options do not apply to an action"
        # NO project named is not an error: it means the one you are standing
        # in, resolved in act().
        elif len(a.idxs) > ds.MAX_SPAWN:
            a.err = "%d projects — at most %d windows at once" % (len(a.idxs), ds.MAX_SPAWN)
        elif len(set(a.idxs)) != len(a.idxs):
            a.err = "the same project named twice"
        return a
    if a.idx is not None and (a.terms or win_seen):
        # The detail view resolves against every project, window or not, so a
        # scope beside it would silently mean nothing (".sess"'s own rule).
        a.err = "a project and a filter at once — drop one"
    return a


# ------------------------------------------------------------------ the data

def group(allrows):
    """Transcripts -> one record per PROJECT FOLDER, most SESSIONS first.

    The folder IS the project: the client derives it from the cwd, and one folder
    can hold cwds differing only in case (dot_sess.slug_of), so grouping by
    anything finer would split one project in two. stat data only — no read here.
    """
    by = {}
    for r in allrows:
        g = by.get(r["slug"])
        if g is None:
            by[r["slug"]] = {"slug": r["slug"], "n": 1, "mtime": r["mtime"],
                             "size": r["size"], "sids": [r["sid"]], "rows": [r]}
        else:
            g["n"] += 1
            g["size"] += r["size"]
            g["mtime"] = max(g["mtime"], r["mtime"])
            g["sids"].append(r["sid"])
            g["rows"].append(r)
    out = list(by.values())
    # mtime order, and it is NOT what gets displayed: ":N" means "the N projects
    # I used most recently", so this is the order the WINDOW cuts. What the
    # reader sees is alpha() below. Kept free (mtime is a stat) because it runs
    # over every project, where the path costs a read.
    out.sort(key=lambda g: g["mtime"], reverse=True)
    return out


def alpha(groups):
    """Paths fetched, then ordered BY PATH — the one and only display order.

    Every path that prints a numbered project row resolves its ranks through
    here — the listing, the ambiguity refusal in by_terms(), and the detail
    view's no-map fallback — because a project showing two different numbers in
    two outputs of the same command is the wrong-launch the index map exists to
    prevent, arriving by a different road.

    Case-folded on the FULL path: it clusters siblings (every path under the
    same parent lands together), which is how a disk is actually thought about,
    where the basename alone would scatter them.
    """
    for g in groups:
        if "path" not in g:
            g["path"] = path_of(g)
            g["gone"] = bool(g["path"]) and not os.path.isdir(g["path"])
    return sorted(groups, key=lambda g: (g["path"] or g["slug"]).lower())


PROBE = 3         # transcripts tried for a cwd before giving up on a project


def path_of(g):
    """The project's real path, read from its own transcripts, or "" if none says.

    The newest one first because it is the one most likely to still record a cwd;
    a couple more are tried because an empty or truncated transcript is common
    enough and costs ~5 ms to rule out. The folder name is NOT a fallback: it is a
    lossy encoding ("C--Work-a-b" could be three different paths) and printing it
    as if it were a path is how a wrong "cd" gets pasted.

    A recorded cwd must re-encode to this project's own slug (case-insensitive):
    the folder name is derived from the directory "claude" was STARTED in and never
    moves, while a cwd does (a "cd" is written into the transcript). A path that
    does not re-encode to its folder is a SUBFOLDER, and launching there would file
    the new session under a different project than the row that was picked. Every
    probe failing it is the same answer as no cwd at all: _blockers() refuses.
    """
    for rec in sorted(g["rows"], key=lambda r: r["mtime"], reverse=True)[:PROBE]:
        cwd = ds.meta(rec)["cwd"]
        if cwd and ds.slug_of(cwd).lower() == g["slug"].lower():
            return cwd
    return ""


def empty_folders(seen):
    """Project folders holding no transcript at all — worked in, history gone.

    Counted, never listed: the folder name cannot be turned back into a path, so
    a row for one would carry no usable information. It is a retention signal,
    and it belongs next to the "Nd kept" the footer already prints.
    """
    try:
        return sum(1 for d in os.scandir(ds.PROJECTS)
                   if d.is_dir() and d.name not in seen)
    except OSError:
        return 0


# --------------------------------------------------------------- the renders

def mark(g, live, here_slug):
    """● a session is open in this project (green) · ◐/· seen recently (dim).

    The glyph carries its own colour: GREEN for "open now", dim for the
    seen-recently fallbacks. Without it the mark sits in the inherited run that
    the bare rank opened, so it came out yellow — and yellow is reserved for what
    you can TYPE (dot_common), which a status dot is not. Colouring it here also
    costs nothing downstream: the next cell already emits its own code.
    """
    if any(s in live for s in g["sids"]):
        return "%s●%s" % (OK, R)
    if time.time() - g["mtime"] > ds.SEEN_WARM:
        # Bound on the work: claudette logs a session when it renders that
        # session's status line, and a render happens on activity — which wrote
        # the transcript. A project whose newest transcript is an hour cold
        # cannot hold a session seen inside the hour, so the per-session probes
        # below (one file open each) are skipped.
        return " "
    best = None
    for s in g["sids"]:
        age = ds.seen(s)
        if age is not None and (best is None or age < best):
            best = age
    if best is None:
        return " "
    if best < ds.SEEN_LIVE:
        return "%s◐%s" % (TIME, R)
    return "%s·%s" % (TIME, R) if best < ds.SEEN_WARM else " "


RECENT = 7 * 86400        # "used lately", the yellow tier


def tier(g, live):
    """(colour, keyword) for the row — the emphasis the ORDER does not carry.

    Alphabetical order makes position meaningless by design, so the emphasis is
    colour. Three tiers, and the keyword is printed beside each one so the row
    still reads correctly with colour stripped or unseen:

        gone    dim    the folder is gone — DIM, not red: it is dead weight to
                       skip past, and red is a claim on attention that a folder
                       you cannot open does not deserve
        open    green  a session is live in it right now
        recent  ACT    used within 7 days, nothing open

    "gone" is tested first because de-emphasis is the point: a dead folder must
    not be lit up by having been touched yesterday.

    CYAN for recent: YELLOW IS SPOKEN FOR. ".hist" prints the numbers you can type
    into the next command in the block's own yellow and nothing else in it, and
    ".sess" and ".proj" keep that contract, which rules yellow out for "recent".
    """
    if g["gone"]:
        return TIME, "✗ gone"
    nlive = sum(1 for s in g["sids"] if s in live)
    if nlive:
        return OK, "%d open" % nlive
    if time.time() - g["mtime"] < RECENT:
        return CYAN, "recent"
    return TEXT, ""


def row(n, g, live, here_slug):
    colour, word = tier(g, live)
    you = g["slug"].lower() == here_slug
    # The path is cut in the MIDDLE (dc.mid): its end is the folder that tells two
    # rows apart. "←you" sits inside the path column, as it does inside ".sess"'s
    # title column (ds.you_cell), and COL_PATH is what is left of WIDTH once every
    # fixed cell and the widest tag are paid: 25 + 60 + 9 = 94, so the widest row
    # fills the line exactly. "NNN open" would pass it by one; a hundred live
    # sessions in one folder is out of budget.
    path = dc.mid(g["path"] or ("?  (no cwd recorded — %s)" % g["slug"]),
                  COL_PATH - (len(ds.YOU) if you else 0))
    tag = ("  %s%s%s" % (colour, word, R)) if word else ""
    # NO LITERAL SPACE between the mark, the age and the session count: each
    # right-aligned mask carries its own separator. A space ON TOP of a mask is
    # width spent on the WIDEST value that will ever land there and wasted on every
    # other row. A 4-digit day count ("1234d", reachable only near the 3650d
    # retention ceiling) fills its mask and sits against the mark, which is a
    # blank on every row that is not open.
    # Bare rank: the number you can type is the block's own yellow, and it only
    # works at the start of the line. Same contract as ".hist" and ".sess".
    return "%3d %s%s%5s%s%s%4d%s  %s %s%s" % (
        n, mark(g, live, here_slug),
        TIME, ds._ago(int(time.time() - g["mtime"])), R,
        TITLE, g["n"], R,
        ds.you_cell(path, colour, COL_PATH, you),
        ds.size_cell(g["size"], colour), tag)


def legend(a, shown, considered, machine, empties):
    """The line that must never let a filtered, windowed list look complete:
    what was shown, out of how many projects exist.

    There is no scope clause: every project on this machine is always in scope and
    the only things that hold rows back are the window and the terms, both of
    which the line names.
    """
    out = []
    win = "all" if not a.window else str(a.window)
    where = "%d project folder(s) with transcripts" % machine
    depth, keep = ds.retention()
    horizon = "%s%dd kept%s" % (RED if keep < ds.CLEANUP_FLOOR else "", keep,
                               R + TIME if keep < ds.CLEANUP_FLOOR else "")
    parts = (["%d shown" % shown, where] + (["%dd deep" % depth] if depth else [])
             + ["window :%s" % win, horizon])
    out += ["%s%s%s" % (TIME, ln, R) for ln in dc.fit_parts(parts)]
    if empties:
        out.append("%s   %d project folder(s) have no transcript left — worked in, "
                   "history already deleted%s" % (TIME, empties, R))
    if keep < ds.CLEANUP_FLOOR:
        out.append("%s   transcripts older than %dd are deleted by Claude Code — "
                   "%s/claudette prefs%s %sraises it (claude_history_floor)%s"
                   % (TIME, keep, R + ACT, R + TIME, "", R))
    if a.window and considered < machine:
        out.append("%s   %d project(s) outside the %d most recently used — widen with%s "
                   "%s.proj :100%s %sor%s %s:*%s"
                   % (TIME, machine - considered, a.window, R, ACT, R, TIME, R, ACT, R))
    return out


# -------------------------------------------------------------------- action

def here_project(groups, here_slug):
    """The path of the project THIS session belongs to — its startup folder, "".

    Not the live cwd. The folder "claude" was started in is the session's sandbox
    root AND the key its transcripts are filed under; a "cd" moves neither. So a
    session that has stepped into a subfolder is still in the same project, must
    still print "←you" on that row, and a ".proj !sonnet" from there must open at
    the ROOT — opening at the cwd would file the new session under a project of
    its own.

    here_slug is that key, taken from the transcript's own folder
    (ds.project_key), so this is a lookup and not a guess. Nothing found means ""
    and _blockers() REFUSES, like every other project with no recorded path; there
    is no fallback to the live cwd, so a missing path never moves anything.
    """
    for g in groups:
        if g["slug"].lower() != here_slug:
            continue
        if "path" not in g:
            g["path"] = path_of(g)
            g["gone"] = bool(g["path"]) and not os.path.isdir(g["path"])
        return g["path"]
    return ""


def _blockers(g):
    """Why this project must not be launched into. A cd is the whole mechanism,
    so both cases are the same case: we do not know a directory to cd to."""
    if not g["path"]:
        return "%sno working directory recorded in its transcripts — cd there by hand%s" \
            % (RED, R)
    if g["gone"]:
        return ("%sthe folder is gone: %s — recreate it first, or claude files the "
                "new session under another project%s" % (RED, g["path"], R))
    return ""


def index_write_hits(cur, pairs):
    """Every path that PRINTS numbered project rows records them, and there are
    three: the listing, the search, and the ambiguity refusal below."""
    ds.index_write(cur, [(n, g["slug"]) for n, g in pairs], INDEX_DIR)


def matching(groups, terms):
    """Projects whose PATH contains every term, carrying their display ranks."""
    return [(n, g) for n, g in enumerate(alpha(groups), 1)
            if all(t in (g["path"] + " " + g["slug"]).lower() for t in terms)]


def by_terms(a, groups, live, here_slug, cur):
    """([(rank, group)], error_lines) for ".proj lab !sonnet".

    Words WITH an action are the identity form, and the rule that makes them safe
    is that they must name exactly one project. Zero and many are both answered,
    never guessed at: picking the best match would make a launch depend on a
    ranking the user never saw, which is the whole failure the index map exists
    to prevent, reintroduced through the back door.

    Searched over EVERY project, not the default window — a word is not a
    position, so nothing about it should depend on how deep the last list went.
    """
    hits = matching(groups, a.terms)
    q = " ".join('"%s"' % t for t in a.terms)
    if not hits:
        return [], ["   %sno project path contains %s%s" % (RED, q, R)]
    if len(hits) > 1:
        shown = hits[:8]
        lines = ["   %s%s matches %d projects — add a word, or use its number:%s"
                 % (RED, q, len(hits), R)]
        for n, g in shown:
            lines.append(row(n, g, live, here_slug))
        # These numbers came from a FRESH scan, and ".proj !new 3" resolves
        # against the last listing — so unless this counts as one, the number on
        # screen and the number that launches can name different projects the
        # moment any live session writes. Telling someone to type a number is
        # what makes an output a listing; record it.
        index_write_hits(cur, shown)
        return [], lines
    return hits, []


def act(a, groups, cur, live, here_slug):
    """".proj !opus 3 7" / ".proj lab !sonnet" / ".proj !sonnet" — a NEW session in each.

    All or nothing, for the reason ".sess !r" is: a half-done batch leaves windows
    the user now has to sort out, a refusal costs one retype and nothing moved.
    """
    head = "%s%s%s" % (ACT, a.label or ".proj !%s" % a.action, R)
    if not a.idxs and not a.terms:
        # No project named: the one this session BELONGS to — its startup folder,
        # never the live cwd (here_project). Labelled "here" rather than a number
        # because there may be no listing this could refer to.
        picked = [("here", {"path": here_project(groups, here_slug),
                            "slug": here_slug})]
        errs = []
    elif a.terms:
        picked, errs = by_terms(a, groups, live, here_slug, cur)
        if errs:
            return ["%s %s— nothing launched:%s" % (head, TIME, R)] + errs
    else:
        imap, why = ds.index_read(cur, INDEX_DIR)
        if imap is None:
            return ["%s %s— %s; run %s.proj%s %sfirst%s"
                    % (head, RED, why, R + ACT, R + RED, "", R),
                    "   %sor name it by a word — that needs no listing: %s.proj lab !%s%s"
                    % (TIME, R + ACT, a.action, R)]
        by_slug = {g["slug"]: g for g in groups}
        picked, errs = [], []
        for n in a.idxs:
            slug = imap.get(str(n))
            if slug is None:
                shown = sorted(int(k) for k in imap)
                errs.append("   %s%d%s %swas not in that listing (it showed %d–%d)%s"
                            % (ACT, n, R, RED, shown[0], shown[-1], R))
            elif slug not in by_slug:
                errs.append("   %s%d%s %s— that project has no transcript left%s"
                            % (ACT, n, R, RED, R))
            else:
                picked.append((n, by_slug[slug]))

    for n, g in picked:
        if "path" not in g:
            g["path"] = path_of(g)
        g["gone"] = bool(g["path"]) and not os.path.isdir(g["path"])
        stop = _blockers(g)
        if stop:
            # "%s", not "%d": the bare form labels its one entry "here", because
            # there may be no listing whose number could name it.
            errs.append("   %s%s%s  %s" % (ACT, n, R, stop))
    if errs:
        return ([("%s %s— nothing launched; %d problem(s):%s" % (head, TIME, len(errs), R))]
                + errs + ["%s   nothing has moved — fix and retype%s" % (TIME, R)])

    # The only interpolation into a command line, and MODEL_OK is why it is safe:
    # the values come from ACTIONS, never from what was typed.
    model = a.model
    assert model is None or MODEL_OK.match(model), model
    assert a.name is None or NAME_OK.match(a.name), a.name
    cmd = ("claude" + (" --model %s" % model if model else "")
           + (' --name "%s"' % a.name if a.name else ""))
    det = sess_launch.detect()
    plans = [(n, g, sess_launch.plan_cmd(g["path"], cmd, det)) for n, g in picked]

    if not plans[0][2].ok:
        out = ["%s %s— %s; start them by hand:%s" % (head, TIME, plans[0][2].why, R)]
        for _n, g, p in plans:
            for ln in p.shown:
                out.append("     %s%s%s" % (ACT, ln, R))
        return out

    done, failed = [], []
    for i, (n, g, p) in enumerate(plans):
        if i:
            time.sleep(ds.SPAWN_GAP)
        ok, err = sess_launch.spawn(p)
        (done if ok else failed).append((n, g, err))

    # Echoed by PATH, never by the index just used: the first new session to write
    # its transcript has already made its project number 1.
    out = ["%s %s— opened %d new session(s)%s, one new window each%s"
           % (head, TIME, len(done), (" on %s" % model) if model else "", R)]
    for _n, g, _e in done:
        out.append("   %s%s%s" % (TEXT, g["path"], R))
    for n, g, err in failed:
        out.append("   %s%s%s %sfailed to launch: %s%s" % (ACT, n, R, RED, err, R))
    out.append("%s   %s · %s · %s%s"
               % (TIME, plans[0][2].terminal or "?", plans[0][2].shell or "?",
                  sess_launch.OUTLIVE, R))
    return out


# --------------------------------------------------------------- detail view

DETAIL_MAX = 50           # sessions listed for one project; each costs a read


def detail(n, g, allrows, live, cur):
    """".proj 2" — the sessions of that project, newest first.

    This is ".sess :here" for a project you are NOT standing in. It renders with
    ".sess"'s own row layout and its own GLOBAL ranks — the list comes out sparse
    (1, 4, 9, 23) exactly as a filtered ".sess" does, and a number means the same
    session in both.

    It therefore writes the SESSION map, not the project one: what is on screen
    is sessions, so ".sess 4" and ".sess !r 4" must answer from it. The project
    map is left untouched on purpose, so ".proj !new 2" still works right after
    drilling in — the two maps record two different listings and neither can
    answer for the other.
    """
    rows = [(i + 1, r) for i, r in enumerate(allrows) if r["slug"] == g["slug"]]
    over = max(0, len(rows) - DETAIL_MAX)
    rows = rows[:DETAIL_MAX]
    cands = [(i, r, ds.meta(r)) for i, r in rows]
    nlive = sum(1 for s in g["sids"] if s in live)
    count = "%d session%s" % (g["n"], "" if g["n"] == 1 else "s")
    state = (" · %d open" % nlive) if nlive else ("" if not g["gone"] else " · gone")
    # The path gets what the line leaves it, cut in the middle (dc.mid): printed
    # whole, a deep folder would put this head past WIDTH.
    room = WIDTH - len(".proj %d — " % n) - len(" · ") - len(count) - len(state)
    path = dc.mid(g["path"] or ("? (no cwd recorded — %s)" % g["slug"]), room)
    # Both anchors bright: WHICH project, and HOW MANY sessions are below.
    head = "%s.proj %d%s %s— %s%s%s %s· %s%s%s%s%s" % (
        ACT, n, R, TIME,
        R + (TIME if g["gone"] else TEXT),
        path, R + TIME, "",
        R + TEXT, count, R + TIME,
        state, R)
    out = ds.listing(cands, live, cur, head, proj=False)
    ds.index_write(cur, [(i, rec["sid"]) for i, rec, _m in cands])
    if g["gone"]:
        out.append("   %sthe folder is gone — resuming any of these files the "
                   "session under another project%s" % (RED, R))
    if over:
        out.append("%s   %d older session(s) not shown — %s.sess :here%s %sfrom inside "
                   "the project lists them all%s" % (TIME, over, R + ACT, R + TIME, "", R))
    out.append("%s   the numbers above are %sSESSION%s %snumbers: %s.sess N%s %sfor its "
               "prompts, %s.sess !r N%s %sto reopen it%s"
               % (TIME, R + TITLE, R + TIME, "", R + ACT, R + TIME, "",
                  R + ACT, R + TIME, "", R))
    # "in it", not "here": a bare ".proj !new" means the startup folder, so "here"
    # in a footer about project N would name the wrong folder.
    out.append("%s   %s.proj !new %d%s %sstarts a FRESH session in it (%s!opus%s %s…)%s"
               % (TIME, R + ACT, n, R + TIME, "", R + ACT, R + TIME, "", R))
    return out


# -------------------------------------------------------------------- entry

def handle(data, tail, to_context):
    # "?" is help (dc.HELP), before parse: parsed, it would be a path filter for "?".
    if dc.is_help(tail):
        return _emit(USAGE, to_context, False)
    a = parse(tail)
    cur = data.get("session_id") or ""
    # From the TRANSCRIPT'S folder, not from the cwd, so "←you" survives a cd into
    # a subfolder. The live cwd is not read anywhere in this command — see
    # ds.project_key.
    here_slug = ds.project_key(data)

    if a.err:
        return _emit(["%s.proj%s %s— %s%s" % (ACT, R, TIME, a.err, R)] + USAGE,
                     to_context, True)

    allrows = ds.scan()
    if not allrows:
        return _emit(["%s.proj%s %s— no transcripts under %s%s"
                      % (ACT, R, TIME, ds.PROJECTS, R)], to_context, False)
    groups = group(allrows)
    live = ds.registry()

    if a.action:
        return _emit(act(a, groups, cur, live, here_slug), to_context, False)

    if a.idx is not None:
        # Like ".sess N", this resolves from the last listing when there is one,
        # so ".proj 2" and ".proj !new 2" can never name different projects; with
        # no listing the global rank is the honest answer, and a view changes
        # nothing if it is wrong.
        imap, _why = ds.index_read(cur, INDEX_DIR)
        slug = (imap or {}).get(str(a.idx))
        g = next((x for x in groups if x["slug"] == slug), None) if slug else None
        if g is None:
            if a.idx > len(groups):
                return _emit(["%s.proj %d%s %s— no such project; this machine has %d%s"
                              % (ACT, a.idx, R, TIME, len(groups), R)], to_context, False)
            # No map: fall back to the rank a listing WOULD print, never to the
            # internal recency order — the same numbers or none.
            g = alpha(groups)[a.idx - 1]
        if "path" not in g:
            g["path"] = path_of(g)
            g["gone"] = bool(g["path"]) and not os.path.isdir(g["path"])
        return _emit(detail(a.idx, g, allrows, live, cur), to_context, False)

    # THE WINDOW IS STILL DEPTH IN RECENCY; only the DISPLAY is alphabetical.
    # ":20" therefore means "the 20 projects I used most recently, listed by
    # path" — which keeps ":N" a bound on the WORK (mtime is free, the path is a
    # read) and keeps it a meaningful filter, where "the 20 alphabetically first"
    # would be neither. `groups` is mtime-ordered for exactly this.
    considered = groups if not a.window else groups[:a.window]

    # The path costs a read, so it is fetched only for what the window kept —
    # and it has to be fetched before the sort, because it IS the sort key.
    # Ranks are the position in this order; at the default window that is every
    # project, so a number keeps meaning the same project until a NEW folder is
    # used — which is the whole reason the display order is alphabetical.
    considered = alpha(considered)
    rows = [(n, g) for n, g in enumerate(considered, 1)
            if all(t in (g["path"] + " " + g["slug"]).lower() for t in a.terms)]

    if a.terms:
        head = ("%s.proj%s %s— %s%d project folder(s)%s %smatching %s%s"
                % (ACT, R, TIME, R + TEXT, len(rows), R + TIME, "",
                   " + ".join('"%s"' % t for t in a.terms), R))
    else:
        # The subject of the listing is bright, the rest is meta: the first line
        # says WHAT is being read before it says how it is arranged.
        # Each word in the legend WEARS the colour it explains, so the header is
        # the key to the table under it: "sessions" is blue because the blue
        # column is the session count.
        head = ("%s.proj%s %s— %s%d project folders%s"
                " · by path · %ssessions%s"
                " · %sopen%s"
                " · %srecent%s (7d)"
                " · gone%s"
                % (ACT, R, TIME, R + TEXT, len(rows), R + TIME,
                   R + TITLE, R + TIME,
                   R + OK, R + TIME,
                   R + CYAN, R + TIME,
                   R))
    out = [head]
    for n, g in rows:
        out.append(row(n, g, live, here_slug))
    # Only the rows actually rendered: an index the user could not read is not
    # one they can type.
    index_write_hits(cur, rows)
    out += legend(a, len(rows), len(considered), len(groups),
                  empty_folders({g["slug"] for g in groups}))
    if not rows:
        out.append("%s   nothing matched — the filter reads the project PATH only,%s"
                   % (TIME, R))
        out.append("%s   not what was said in it (%s.sess word%s %sdoes that)%s"
                   % (TIME, R + ACT, R, TIME, R))
        return _emit(out, to_context, True)
    out.append("%sUse %s%s.proj N%s %sfor its sessions · %s%s.proj !new N%s %sfor a fresh "
               "one there (%s!opus%s %s…)%s"
               % (TIME, R, ACT, R, TIME, R, ACT, R, TIME, R + ACT, R + TIME, "", R))
    # Its own line: the footer above is already 74 of the 94 columns.
    out.append("%s    %s.proj !sonnet%s %swith no number opens one where you are "
               "standing%s" % (TIME, R + ACT, R + TIME, "", R))
    return _emit(out, to_context, False)


def _emit(lines, to_context, noisy):
    if to_context:
        dc.emit_context_block(lines, ".proj", noisy=noisy)
    else:
        dc.emit_block(lines, noisy=noisy)
