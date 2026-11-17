---
name: done
description: Report whether everything this session knows is safely persisted, and close the gaps. Use at the END of a session, in whatever language the user says it — about to /clear, about to /compact, closing the window, handing off, switching machine, "done for now", "safe to close?". NOT for ordinary task completion — this is about leaving the session, not finishing a piece of work.
argument-hint: "[all | U | 123c | push | commit context | 6 | R <path> | help]"
---

# /claudette:done

The user is about to walk away from this context. Your job is one invariant:

> **Everything needed to resume this work must be reachable from something that outlives the
> session** — normally the repo. Not from this conversation, not from Claude memory, not from
> the scratchpad.

Assume **other sessions are editing the same worktree right now**, usually the same user in
another window. They are not a threat to warn about; they are a fact to work around.

**Before changing this skill's model — the slots, the numbering, the marks, the report shape —
read `DESIGN.md` next to this file (design notes, not published).** It records what was already tried and rejected, and why.
**Rationale lives there, not here** — this file is re-sent on every round trip of every run, so a
sentence that does not change what you *do* is paid for many times over. It earns a place here
only when it stops you taking a plausible shortcut mid-run.

## The eight slots — two scopes

Volatile stores drain into files; the files reach a commit, and the commit a remote. Each store
exists in up to **two scopes**:

- **Session** — what *this* context owes. A gap here is mine to close.
- **Folder / repo** — what the *shared* state holds, whoever put it there. A gap here is the
  user's call, and never mine to close unasked.

`memory` and `scratchpad` are **folder**-scoped, not repo-scoped — they live beside the project,
not inside git. `commit` and `push` are the git hops. Folder slots come first, hence the header
`Folder/repo:`, in that order.

| | scope | store → target | clean when |
| --- | --- | --- | --- |
| **1** | session | `Context` — this conversation → project files | nothing key lives only in this conversation |
| **2** | session | `Memory` — files this session wrote → project files | you could **delete every memory file this session wrote and lose nothing** |
| **3** | session | `Scratch` — this session's temp dir → repo or user | nothing there is a deliverable, or all of it is regenerable |
| **C** | session | `Commit` — this session's files → commit | every file this session created or changed is committed |
| **6** | folder | `Memory` — the whole store → project files | every `type: project` entry has a mirror **you found** |
| **7** | folder | `Scratch` — other sessions' temp dirs → user | every sibling scratchpad is empty, or belongs to a live session |
| **R** | repo | `Commit` — anyone's dirty files → commit | the tree is clean |
| **P** | repo | `Push` — commits → remote | the current branch is 0 ahead |

**2 is not "write memory".** Memory is a convenience copy; the project file is the record. A
memory-only fact is a **failed 2**, however well written. Same for 6.

`C`/`R`, `2`/`6` and `3`/`7` each share a label; the block header is the disambiguator, applied
uniformly to all four pairs.

### The numbering is reserved — 4, 5, 8 and 9 are meant to stay empty

**Numbers are stores that drain; letters are hops along the chain.** Session stores are **1–5**,
folder stores **6–9**, and the folder twin of session store N is **N+4**. A new store type lands
at `4`/`8` or `5`/`9` without shifting an id the user has already learned — which is the whole
reason the gap exists. **Do not close it.**

`1 Context` and `P Push` are unpaired **by construction**: the shared form of `1` would be other
sessions' live contexts, unknowable from in here, so it gets no slot rather than a slot nobody
can act on. One unpaired slot at each end of the chain, none in the middle.

## Marks — `!` where the gap is confirmed, `?` where it is merely unopened

| | means |
| --- | --- |
| ` ` | checked, nothing missing |
| `?` | **content is there and nobody has looked** — 6 and 7 only |
| `!` | **confirmed: something needed is not persisted** |

`1 2 3 C R P` can each be confirmed, so each can be marked `!`. **`6` and `7` never take `!`**:
`6` cannot confirm anything (*not found* is not *absent*), and `7` reports residue, not a verdict.

They take `?` instead, and only when there is something to open: a default run names them without
investigating, so "9 entries" is a fact about volume, not about risk. `?` says exactly that — *this
has contents and this run did not read them* — and pairs with `→ check 6` / `→ check 7`, the
read-only analysis. **Empty means no mark at all**: nothing to look at is not a question.

A mark states what is true, never what you were allowed to do. The block header
`Folder/repo — your call:` already says whose move it is, and the parenthetical says what was asked.

**A clean line never contains the name of the thing that would be missing.** `no mirror`,
`not committed`, `not pushed` belong on a `!` line only. Clean is `Ok, nothing to do` (Report).
Same reason, everywhere in the report: use short plain verbs. `owes`, `carries`, `holds`, `stands`
flip to their opposite for a reader whose first language is not English.

Every line, marked or not, names its **magnitude** and what it would **cost to resolve**
(`14 files, one R away`); a line without both is unactionable.

**Live state is not a slot.** Background tasks and shells, `/loop`s, scheduled wakeups,
in-session crons, `/claudette:throttle` jobs, git worktrees entered and not exited: you cannot
drain a running process into a file. Report them in one trailing line and let the user decide.

**The usage chart rides that line too, in ONE line, in these words** — a reader who has not
adopted the feature owes it no explanation:

    · Usage chart_autoupdate is off (machine 47m behind, /claudette:chart if interested)
    · Usage chart: update handed to a background session (1769 readings, 6 windows)
    · Usage chart: nothing new                    # feature on, nothing had moved

With the feature on, the line is what `chart_runner.py launch` printed (see Preflight).

No `usage_chart` section, or `configured: false` → say nothing at all: a machine with no
chart is not a gap.

**No git in this folder** (a cloud session, a plain notes folder): `C R P` collapse to one line,
`+ C R P    : Ok, no git tracking here; 1-3 and 6-7 carry everything` — nothing to do, so it is a
clean line and takes the `+` and the `Ok`. **`repo.git_missing` is the opposite case**: unknown,
not clean, so that collapse line gets neither — two spaces, no `Ok`, and the reason. A legitimate
end state, not a failure — the numbered slots still apply, because the folder outlives the session even when
nothing tracks it. **Legal only when `repo.subrepos` is empty too** — an untracked folder whose
subfolders hold repos is not "no git"; `C R P` then apply per subrepo (see Preflight, and
`DESIGN.md` for the session this line misled).

## What counts as key

For 1, 2 and 6, **both halves must hold**: *would the next session redo work, or decide wrong,
without this?* **and** *is it not already derivable from the code, the repo, or git history?*
Either half fails → not key. Do not persist it, do not report it. Session chatter, a
restatement of the diff, transient status, a TODO for work you are about to commit — none of it
is key.

**Over-capturing is not the safe default.** Every file written is a context tax on every future
session and a candidate for rot. "Keep it all" trades a small risk of forgetting for a certain
cost in noise and a real risk of confidently-wrong stale notes.

**Scan for the user's feedback first: it is the scarcest thing in the context** — the code keeps
what was built, and the reasons die with the session (`DESIGN.md`, "Why this file exists"). So
look first for:

- what the user **rejected**, and the reason — worth more than a description of what was built
- the reason **behind** an accepted choice, when it is not obvious from the result
- a constraint or preference stated once in passing that shaped everything after it

Where it lands: a rejected design alternative → the project's design file (`DESIGN.md`,
`*_DESIGN.md`). Working knowledge → the project's md. See `DESIGN.md` here for the filter that
keeps such a file from turning into a journal.

## Arguments

**Naming a slot means: do the work needed to do it properly, then act, up to that slot's
ceiling.** Not "act blindly" and not "analyse and come back to ask" — one turn, no second wait.

Every run assesses all eight. **Unnamed slots are never analysed deeply** — that is why the
default run is fast: `6` and `7` get named, not investigated.

- *(no argument)* — assess. Cheap on 6 and 7, which are named and not opened. Writes nothing,
  **except the usage chart update when `chart_autoupdate` is on** (see Preflight): the user
  turned that feature on knowing what it records, and it is the one write a bare run makes —
  a watchdog-fired one included, and nothing else in that run writes.
- `U` — the **read-only deep pass over the folder scope** (`6`, `7`, `R`), then report. Writes
  nothing, commits nothing. The slowest read-only mode: "tell me everything, I'll decide."
  Legal as a launch argument and as a one-character reply to a report.
- `check <id>` — the same read-only analysis as `U`, on **one** slot: `check 6`, `check 7`,
  `check R`. This is what a `?` line offers, because the alternative for a curious user is `6`,
  which *mirrors what it finds* — a write, in answer to a question. `check` never writes.
  It is the only verb where a word precedes the id, so the parser must special-case it.
- `all` — the full session checkpoint plus push (`1 2 3 C P`). **`all` deliberately excludes
  `6`, `7`, `R` and `U`**: acting on the shared scope is never implied by a word that merely
  sounds total. Ask for it with `all 6` / `all U`.
- `context memory commit` / `1 2 C` — persist and commit, do not push
- `push` / `P` — push what is already committed and nothing else. A legitimate end state: the
  user may want the committed state on the remote *before* adding more. Not a failure.
- `6` — the same full analysis `U` performs, and then mirror whatever is genuinely missing.
- `7` — **investigate in full**: open what you need to, then say in plain words whether anything
  there is worth persisting. Usually not. Moves nothing — a move is a normal request afterwards.
- `R <path>` — adopt the dirty files under `<path>` that no live session owns. **A bare `R` is
  refused**: the path is the user's assertion that nobody is editing there, and that assertion
  is the only thing making the action safe.
- `help` — the help word, reserved. **`?` is never an argument**, so a question mark arriving as
  ordinary punctuation can never be read as a request.

**Parsing.** Ids are single characters (`1`–`9`, `C`, `R`, `P`, `U`) and **case-insensitive**, so
they fuse: `123cu` = `1 2 3 C U`. **Ids come first; anything after the id run is an instruction,
not an argument** — `123c and can you check "this" too?` parses `1 2 3 C`, honours the request, and
treats the trailing `?` as punctuation. Never drop the prose: losing the request is worse than
misreading it.

**One exception: a leading `check`** — `check 6`, `check 7`, `check R`, `check 67`. Only when it
is the FIRST word and an id run follows it immediately; anywhere else `check` is prose, as in the
example above. It reads as read-only analysis of those slots and nothing else: `check 6` must
never mirror, where a bare `6` would.

**Act in report order: `1 2 3 C`, then `6 7 R P`** — so an interrupted run still leaves this
session committed. Files written while acting on `6` get their **own** commit, exactly as `R`'s
adoption does: folder-scope actions produce folder-scope commits.

**Language.** The slot ids, their capitalised labels, the block headers, `DONE PERSISTENCE
STATUS`, the marks and the clean line's `Ok,` are fixed. `Ok` reads in every language this runs
in and is scanned as a glyph, not read as a word — never translate it to `D'accord` or `Bon`. Everything else follows the language of what it goes
into: prose to the user follows the conversation; a commit message or a file written inside a
repo follows that repo's language (read it off recent `git log` or the file you are editing);
`~/.claude` memory files stay English, because `MEMORY.md` is. Examples in this file and in
`DESIGN.md` are always English — they show the shape, not the wording.

## Announce, in one sentence, before the first tool call

Whoever typed this may not know it writes files and commits. One sentence, no preamble, in the
user's language, then go — a heads-up, not a confirmation prompt.

**The announce line is user-facing UI, and internal vocabulary is banned from it**: no slot ids,
no slot count, no "conditions", no "assessing all eight". Open on the user's situation, then say
what is about to happen in words a first-time user understands.

No argument, and `U` too — **both write nothing, so both say so.** Somebody may type `U` expecting
a checkpoint:

> Done with this session? Let me check whether anything still needs to be persisted — I'm only
> looking, I won't write anything.

With `chart_autoupdate` on, that sentence would be false, so say the one thing it
does, in the words the feature was accepted in — **never "sending data from your machine"**,
which describes telemetry, not three integers a second off the user's own status line:

> Done with this session? Let me check what still needs persisting — I'll only look, apart
> from updating your usage chart with this machine's allowance percentages.

With arguments that act, name the actions in plain words rather than the ids:

> Done with this session? I'll get what must survive out of this context, commit it, then show
> you what is persisted and what is not.

Never: *"claudette-done (no argument): assessing only the eight slots, writing nothing."* That is
the skill talking to itself in front of the user.

## Preflight — ONE call, every repo

```bash
bash "$HOME/.claude/skills/claudette/scripts/run.sh" done_probe.py \
    --repo <repo_root> [--repo <another repo root you touched>]... \
    --session <THIS session's id> --pretty
```

**Repeat `--repo` for every repo root this session touched — one call, not one per repo.**
The first is primary: it keeps the `repo` / `upstream_check` keys and sets `project_key`.
The others come back under `extra_repos[]`, each with its own `repo` and `upstream_check`.
Memory, scratchpad, status lines and throttle are session-scoped and are computed once, for
the primary's project — calling the probe again per repo re-derived them under the WRONG
project key, so a second call is not just a wasted round trip, it is wrong.

That returns, read-only, in one round trip: the upstream check, `status --porcelain`, ahead /
behind, in-progress merge-rebase-cherry-pick-bisect, detached HEAD, dirty submodules, every
registered worktree with its dirt, repos nested in
or below the folder, the dirty files **already grouped by folder and aged**, the memory store's
entries with their frontmatter
`type`, this session's and every sibling session's scratchpad with a liveness verdict, throttle
jobs, and both status lines with their anchor suffix precomputed. **Read it once instead of
issuing a dozen shell calls** — round trips, not tool time, are what this run costs
(`DESIGN.md`).

- `upstream_check.verified_behind` > 0 or `moved_unknown` → **stop, warn, wait for the user's go.**
- `repo.in_progress` non-empty, `repo.detached`, or `repo.dirty_submodules` → `C`, `R` and `P`
  cannot hold; that is their reason.
- `--repo` takes the folder the session works in. Below a repo root → the probe resolves the
  root itself and reports `repo.resolved_from`; memory and scratchpad stay keyed on the folder
  given, which is correct — they are folder-scoped, not repo-scoped.
- `repo.is_git` false but `repo.subrepos` non-empty → the folder is untracked, its children are
  not. A repo is a property of a **file**, not of the session: resolve each TOUCHED path upward
  to its repo (`git -C <its folder> rev-parse --show-toplevel`); those repos are `C`'s, and each
  gets its own probe run — the upstream gate above applies per repo. Subrepos nobody touched are
  `R`/`P` facts, reported from the scan's per-repo `dirty`/`ahead` — never a reason to probe
  hundreds. The scan is bounded and discloses each cap that bit (`subrepos_statused`, `scan`);
  print a bitten cap, or the report claims coverage it did not do.
- `repo.nested_repos` non-empty (the folder IS a repo with unregistered repos nested inside):
  the outer repo's `C`/`R`/`P` never cover their content — its git status shows at most one
  opaque dir. Same rule as subrepos: a TOUCHED path inside one resolves to the nested root,
  which gets its own probe run. Registered submodules are excluded — `dirty_submodules`
  already carries them.
- `repo.worktrees` non-empty → extra checkouts registered to this repo, wherever they live —
  worktree-isolated agents leave them under hidden or temp folders, and the registry, not a
  scan, is what finds them. A dirty one is uncommitted work of this repo → `R`;
  `prunable`/`locked` are git's own verdicts, quote them.
- `repo.git_missing` true → git itself is absent, so "repo or not" is **unknown, not untracked**.
  `C R P` collapse with that reason; `repo.subrepos` still lists what the `.git` markers say,
  path-only and unverified.
- `scratchpad.unknown` non-null → the probe could not find where this project's session temp
  folders are, so slot 7 is **unknown rather than clean**: two spaces, no `+`, no `Ok`, and the
  reason in plain words. The old-layout leftovers it did find are still reported.
- `scratchpad.self` null → assess slot 3 from the scratchpad folder your own instructions name,
  listing it yourself, rather than from the probe.
- `repo.is_git` false and `repo.subrepos` empty → the `C R P` collapse described under Marks.
- Pass every repo root you touched this session as a repeated `--repo` — the folder run never
  covers a subrepo, and `extra_repos[]` carries the others' git facts from the same single call.

**Auto-update the usage chart, in the SAME message as that probe** — `chart_autoupdate` on,
whether a person typed this run or an idle auto-done fired it, one more Bash call:

```bash
bash "$HOME/.claude/skills/claudette/scripts/run.sh" chart_runner.py launch
```

It returns at once and prints the chart line for the live-state line, verbatim: nothing new,
or the update handed to a background Claude session that publishes it outside this
conversation (`skills/chart/DESIGN.md`, "The chart runner"). Nothing else to call: no read,
no publish, no confirmation; the outcome reaches `.chart` and the next run's line. Feature
off → no call; the chart line says how far behind the machine is and names
`/claudette:chart`.

Then build the **TOUCHED** set yourself: paths this session created or edited, reconstructed from
your own tool history (Write/Edit/NotebookEdit, scripts you ran) — **not** from mtimes. The probe
deliberately returns `touched: null`; it cannot know, and guessing is the one thing that would
make it dangerous. Everything else dirty is another session's or nobody's: never committed under
`C`, never reverted, never stashed. It is what `R` reports.

Everything the probe reports is a **fact**; every mark, and every judgment of what is *key*, is
still yours.

## Recall — the same shape as a `status` reply

Hours may have passed and several sessions look alike, so say what this session did before
anything else. **Use the shape the `..recap` dot-command produces** — the user parses those at a
glance. That hook asks for:

> a very concise bullet summary of where this conversation is at — 3 to 6 short bullets,
> done / in progress / pending or blocked, no prose paragraphs

Concretely: each bullet opens with its **state**, and anything pending names **whose move it is**.

```diff
+ Done: proved the importer drops every attachment — a field whitelist, not a size limit
+ Done: a1b2c3d + e4f5a6b, both pushed
- Waiting on you: open the three sample records on the phone, then in airplane mode — that
  verdict decides whether Leia's 151 records are worth the work
- Blocked on that: capturing the attachments and writing the push script for batch 2
```

- **One line per bullet. No bold, no parentheses, no nested detail, no counts of what you
  probed.** The pull is to prove the work happened; the user lived it and wants the state, not
  the evidence.
- Columns 1–2 are the colour prefix, per the state word — see Report for the mapping.
- **`Waiting on you` is the most valuable line in the report** — put it above whatever it blocks.
- **Persistence facts stay out of the bullets.** "the memory files all have their mirror", "a
  neighbouring scratchpad is unread" belong to the slot block — a bullet that repeats a slot line
  makes the user read the same fact twice.
- Cover the session since it started, or since its own previous claudette-done commit if there is
  one. Never a diff summary.

## Slot 1 — Context → project files

Apply the key test, feedback first (see above). Targets:

- A rejected alternative and its reason → the project's design file.
- Ongoing work, goals, constraints, decisions and the why → the project's md
  (design/notes/process file).
- Guidance the user gave, who they are, external pointers → the project md if the work needs
  it; `~/.claude` memory as well if it generalises beyond this project.
- A **craft claim** — about working with agents, not about this system ("N sessions in one
  worktree behave like X", "a report of this shape gets misread") → **one line** in the inbox of
  a knowledge vault from `~/.claude/vaults.local.json`, if one resolves; skip silently if not.
  One line only: capture is cheap, promoting it to a pattern is a deliberate human-gated act
  with its own conventions. Never write a vault file here without reading that vault's rules.
- `CLAUDE.md` only if EVERY future session needs the line to act, or to detect that more
  applies. Otherwise: detail file, with a trigger + pointer.

An unwritten plan is context — either write it down or say it dies. Nothing qualifies → the line
is `Ok, nothing key in this context`; do not invent a finding to fill it. Files written here join
TOUCHED.

## Slot 2 — this session's memory → project files

For each memory file this session wrote or updated, apply the test: **delete it — does the
next session lose anything it needs?** If yes, mirror that content into a project file. Memory
keeps its copy; nothing depends on it alone.

Not requested → still assess, and report what would be lost if the memory vanished.

## Slot 3 — this session's scratchpad → repo or user

The session scratchpad dies with the session. For each file in it: is it a deliverable, and is
it regenerable? A regenerable intermediate is fine to lose — half a line says so. A deliverable
is not: move it into the repo, send it with `SendUserFile`, or record the recipe that
regenerates it. Untracked scratch files loose **inside** the repo belong here too → move them
to `temp/`, confirm it is ignored.

Assess-only run: name them and the move you would make, touch nothing.

## Slot C — this session's files → commit

Re-run `git status` immediately before committing. If the picture moved since preflight, another
session acted — rebuild TOUCHED rather than committing a stale view.

```bash
git -C <root> add -N -- <your new untracked paths>   # only your own; the one index touch
git -C <root> commit -F <msgfile> -- <TOUCHED paths> # pathspec: commits worktree content,
                                                     # ignores whatever is staged
```

- **A TOUCHED file commits whole, even if it also carries changes this session did not
  write** — another session's, or the user's own hand edit. Refusing deadlocks: several sessions
  each declining a shared file means nobody ever commits it. Report which files carried extra
  content, by name. A file **not** in TOUCHED is never committed under `C`.
- One commit per repo, imperative subject in the repo's language.
- **Message via `-F <file>` or a single-line `-m`. Never a here-string** — PowerShell `@'…'@`
  handed to Bash injects a literal `@` into the subject, and the repair costs more than the
  commit.
- **Never `--amend`**, and never `git add -A` / `git add .` / `git commit -a` / `git stash`.
  Amend re-reads the shared index and will sweep another session's staged work into your
  commit. Never commit the index at all: it is process-wide state.
- A pathspec commit is a **partial** commit and can be broken. If the project has a cheap check
  (lint, fast test), run it and report the result. If it cannot be trusted because other
  sessions' edits are entangled in the same module, say so — never claim a green you did not
  get.
- `C` covers **every repo touched**, including `~/.claude` if an earlier slot wrote there. A
  store the repo gitignores (claudette syncs memory separately) does not make `C` fail — note it
  and move on; slot 2 already put what matters in a project file.

## Slot 6 — the folder's memory store

The store is keyed by **project folder**, not by session, so entries from other windows sit in it.
Slot 2 covers what *this* session wrote; slot 6 covers the rest. `memory.entries` from the probe
already carries each entry's frontmatter `type`.

- `type: project` — project state. It needs a project-file mirror.
- `type: user` / `feedback` / `reference` — legitimately memory-native: they generalise past any
  one project, so demanding a repo mirror would produce a permanent false mark. Out of scope.

**Finding a mirror settles it. Failing to find one proves nothing.** So:

- mirror found → name where it lives.
- no mirror found → say **not found**, never *absent*. A mirror can sit in a `.bat`, in a
  filename, in a commit message, or be paraphrased rather than quoted.

**The search standard, because a sloppy search produces a confident wrong answer.** No
`--include` filters. No `head` truncating the result set. Every tracked file type, plus history:

```bash
git -C <root> grep -n -i -F "<distinctive phrase>"        # working tree, all tracked types
git -C <root> log -S "<distinctive phrase>" --oneline     # and anything that passed through
```

Every entry memory-native → `Ok, all memory-native`, and **name no entry**: there is nothing for
the user to decide, and listing them invites the reader to see a gap that is not there.

Not requested, and a `type: project` entry exists → name that entry and what it uniquely holds,
specifically enough that the user can recognise it and type `6` without needing to see the analysis
first. The probe hands you each entry's frontmatter `name` and `description` for exactly this.

Requested (`6`) → same analysis, then write the missing mirrors; those files get their own commit.
Already mirrored → **say so** (`Ok, already mirrored in starmap.bat`); never swallow that into
a bare `Ok`.

`memory.capped` true → **print `memory.unchecked` in the report.** A silent cap reads as
"audited everything".

## Slot 7 — other sessions' scratchpads

Sibling scratchpad directories under the same project temp root belong to other sessions. The
probe lists the roots it scanned in `scratchpad.roots` (the client has used two layouts) and
returns `scratchpad.unowned` (non-empty, **no recent statusline render**) and
`scratchpad.live_siblings`.

**No age cutoff. A non-empty unowned scratchpad is reported at any age** — hours or a long
weekend. Age belongs in the *description*, never in the filter.

**This slot never carries `!`; a non-empty unowned dir carries `?`.** Residue is not a verdict, but
"there is something here nobody read" is worth one glyph. Non-empty unowned dirs are normal, and the default run says
so in one line: how many dirs, how many files, what they look like (`chunk_*.js, dump_*.json`), how
old. That is usually enough for the user to dismiss the whole thing, which is the cheapest possible
outcome. Everything empty or live → say that in half a line.

**Liveness is the probe's statusline reading, never the scratchpad's mtime** — an mtime tells you
about a file, never about a person. It stays a hint: quote both ages (`21 files from 21h ago,
statusline 44m ago`) and let the user, who knows what is open, conclude.

`U` or `7` → **investigate in full.** Open what you need to and answer the only question that
matters: is any of this worth persisting? Say it in plain words, name it if yes, and say plainly
that it is junk if no — filenames and byte counts do not answer that question. **Slot 7 still moves
nothing**; a move is a normal request afterwards.

## Slot R — dirty files nobody owns

Dirty files that are not this session's have **three** cases, not two: a live session that will
commit them itself, the user's own hand edit, and **nobody's at all** — a session that did real
work and closed. You **cannot** tell them apart from in here (see Limits), so report enough for
the user, who knows which windows are open, to decide:

- `repo.dirty_groups` is already grouped by folder and aged — print it. The path is what lets the
  user recognise their own work: `trip/alderaan/` and `trip/naboo/` are not the same news.
- Say in words whether a group's owner looks live. **A group with no live owner is confirmed
  uncommitted work, and is what puts `!` on this line**; a group that looks live is reported
  without claiming loss.
- Minutes → almost certainly a live session. Days → smells orphaned. Cross-check
  `scratchpad.live_siblings`: a live window makes "leave it" much more likely.
- mtime stays banned for **attribution** (building TOUCHED). Using it for **triage** here is
  fine and must read as a hint: "2m ago" is near-proof of a live session, "3d ago" is a smell,
  neither is certain.

Acting requires `R <path>`, never a bare `R`, and lands in **its own commit**, separate from
`C`'s — so the message can say plainly that it adopts work left by a closed session, and a wrong
adoption reverts without taking this session's work with it. Every `C` prohibition still
applies: pathspec form, no `--amend`, no `git add -A`, never the index.

## Slot P — commits → remote

- Requested → push until the ahead-count is 0. `P` is an end condition, not a force flag: never
  `--force` / `--force-with-lease`; already up to date → push nothing and say so; verify by
  ahead-count afterwards, not by the push command's exit code.
- Not requested → report the ahead-count and stop. Commits survive `/clear`.
- Some of those commits may be another session's. Say so before pushing them blind.
- Ahead-count > 0 → `!`, with the count and whose commits they are. Commits survive `/clear`, so
  closing it is the user's call; the block header is what says so, not the mark.
- `P` is the current branch only. Other local branches are not this skill's business: the probe
  does not list them, and the report never mentions them.
- The leak scan governs `P` like any other push: run `push-diff.sh <repo_root>` for the repo you
  are pushing (roots already computed for `done_probe.py --repo`), then inspect what is leaving.
  Stop and report if leaks found or files skipped. `leak_scan.enabled` false → say "no push leak
  scan rule on this machine"; `null` → say it could not be determined. A `COVERAGE` line naming a
  skipped or unavailable class is reported with the verdict — `clean` then means clean of what
  ran. Never imply a scan that did not happen.
- Everything the project normally does around a commit still happens: its hooks, its message
  convention, its changelog step, its own "push after every commit" rule if it has one. This
  skill constrains **whose** files get committed and nothing else.

## Limits — state them once, here, never in the report

- **You cannot see other sessions.** No cross-session registry. mtimes and lock files are
  hints, not proof.
- Another session can commit or stage between your check and your commit. The re-check in slot
  `C` narrows that window; it does not close it.
- Say "cannot verify" where that is the truth. A confident all-clear is the one failure mode
  that costs real work.

True on every run — repeating them per run turns the report into noise.

## Report — last message of the turn, nothing after it

**The whole report — recall bullets included — goes inside one ` ```diff ` fence.** That is what
colours it; see below.

```diff
+ Done: <what shipped, one line>
+ Done: <commits by short sha; "pushed" only when they are>
  In progress: <what is under way and owned by nobody but you>
- Waiting on you: <the human action nobody else can take, and what it decides>
- Blocked on that: <what cannot start until the above lands>

DONE PERSISTENCE STATUS                        (requested: 1 2 3 C)
Session:
- ! 1 Context : <what is still only here>
                → <the one action that would fix it>
+   2 Memory  : Ok, empty
+   3 Scratch : Ok, 3 files, all regenerable
- ! C Commit  : 2 files uncommitted — trip/{day3,budget}.md
                → C to commit
Folder/repo — your call:
  ? 6 Memory  : 9 entries, 8 type:project — not opened this run
                → check 6
  ? 7 Scratch : 1 unowned dir, 21 files (chunk_*.js, dump_*.json) from 21h ago
                → check 7   (7 to also act on it)
- ! R Commit  : 14 dirty files, none this session's
                  docs/tools/   7   3d ago   no live owner — unowned work
                  docs/ui/      6   2m ago   live session, leave it
                  src/          1   3d ago   no live owner
                → /claudette:done R docs/tools   (its own commit, separate from mine)
- ! P Push    : 3 ahead, 1 not from this session
  · 1 background task would be orphaned        (or: nothing else would be lost)
Recommendation: C, then R docs/tools — 3 days of unowned work is the only real risk here

─── <session status line>
─── <global status line>
```

- **One rule governs both halves of the fence: `+` green means nothing to do, `-` red means maybe
  something to do.** In the bullets that is the state of the work, in the slot block the state of
  what is persisted; a reader who never learns the distinction still reads both correctly.
- **The prefix follows the state word, never the sentiment.** `Done:` → `+`, even when what
  shipped is a revert or a dead end: green says *settled*, not *good*. `Waiting on you:` and
  `Blocked on that:` → `-`, which also keeps the pair visually joined. `In progress:` and every
  wrapped continuation line → two spaces, neutral.
- **A `Done:` bullet never claims a push that `P` denies** — name the shas, and say "pushed" only
  when the ahead-count is 0. Two answers to one question is worse than one answer late.
- **Columns: the colour prefix in 1–2, the mark (`!` or `?`) in column 3, the id in 5, the label
  in 7, the colon in 15.** The mark column still works by scarcity — a gap carries `!`, a clean
  line leaves it blank.
- **Columns 1–2 are the `diff` fence's colour prefix and mean nothing else.** In the slot block:
  `- ` on every `!` line, `+ ` on every clean line, **two spaces on the rest** — `?` lines, continuation and
  `→` lines, group rows, headers, the live-state line, `Recommendation:`. That paints gaps red,
  clean green and the rest neutral, **identically in the terminal and the desktop app**, and costs
  nothing on mobile, where the fence renders monochrome and `!` and `Ok` still carry the verdict.
- **Only a `!` line or a `-` bullet may begin with `-`, only a clean line or a `Done:` bullet with
  `+`.** A continuation, a group row or the live-state line starting at column 1 with either
  character would be painted a verdict it does not have — that is why the live-state glyph is `·`
  and sits in column 3 with the marks, where a `+` would collide with the colour prefix two
  columns to its left.
- **A `?` line always ends in `→ check <id>`**, never in the bare id: the bare id writes. When
  the acting form is also worth naming, it goes in parentheses after it, as `7` does above.
- **All eight slots print on every run, even when nothing is marked.** Collapsing the clean case
  to one line is deliberately deferred, not forgotten — see `DESIGN.md`.
- Several repos in one report (an untracked folder over subrepos): `C`, `R` and `P` each name
  the repo they speak about, one line per repo with news; subrepos with none roll into a count.
- The parenthetical names what was requested, so a `!` on a requested slot reads as a failure.
- **Every clean line opens with `Ok,` and is never blank after the colon.** `Ok` is the `+`
  prefix's word and belongs to no other line — never on a `?`, never on a `!`. It is what survives
  monochrome, where a blank green line is indistinguishable from *nothing was checked*. Then the
  magnitude, in half a line: `Ok, empty`, `Ok, 3 files, all regenerable`, `Ok, nothing to do` when
  there is genuinely no fact to give. The prohibition above still holds — `Ok, nothing to do`,
  never `Ok, no mirror`. A `!` gets a reason and a `→` instead.
- Every folder line gets a `→` when there is something the user could act on, and none when there
  is not.
- **`Recommendation:` closes the block**, below the `+` line: one line naming what to type next and
  why, in the order it should happen. It **ranks** the `→` arrows rather than repeating them — the
  arrows say what each gap needs, this says which one matters. Nothing to do →
  `Recommendation: nothing`.
- No prose before or after the block on a normal run. A mistake you made and fully repaired,
  leaving no trace, is worth zero lines — not a paragraph.
- The two `─── ` lines close a claudette-done **report** — and only that. They are not a
  conversational sign-off: never append them to an ordinary turn just because this skill ran
  earlier in the session.
- **Read them from disk in the same turn you print them, or do not print them at all.** Never
  carry a line forward from earlier in the conversation, re-render one from memory, or increment
  its age by hand. A sent message is frozen while the numbers keep moving, so a remembered
  status line is a fabricated one.
  - `status_lines.session.line` and `status_lines.global.line` from the probe, which reads
    `logs/sessions/<THIS session's id>.json` — that exact id, never the newest file in the
    directory, which may belong to another session.
  - The run itself takes minutes. If the probe is more than ~5 minutes old by the time you print,
    re-run it (or just re-read those two files) — the same freshness rule applies to the probe as
    to your memory.
- **A relative age never appears without an absolute anchor.** Append `status_lines.*.suffix`
  verbatim; it is `  (HH:MM, Nm ago)` when `ts` is over 10 minutes old and empty otherwise. A bare
  `(Nm ago)` is forbidden: the reader may see the text much later, and the claim decays into a
  falsehood. `hook_statusline.py` rewrites both files on every render, so a live session normally
  has an empty suffix — a large age is itself the anomaly worth showing.
- Skip a line whose file or field is missing; skip both silently if neither exists. The prompt
  hook fires only when the prompt is exactly `status` / `stats` / `usage`, so read the files
  yourself.
