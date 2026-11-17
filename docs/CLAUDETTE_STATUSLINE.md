# Claudette status line — segments and customisation

The status line is assembled from **named segments**. Three parameters in
`prefs.local.json` (all settable via `/claudette prefs` or
`features_apply.py --param`) control the rendering:

| Parameter | Default | Meaning |
|---|---|---|
| `statusline_segments` | see below | segment names, in display order — **omission is deletion** |
| `statusline_separator` | `"  "` | string joined between segments |
| `statusline_custom` | `""` | path to a wrapper script (below) |

**Invariant: segment configuration affects rendering only.** Every value is
always computed and the snapshot written to `logs/claude_usage_last.json` on
every measured render is always complete — the throttle, the idle watchdog and
the done report read it, and hiding a segment must never blind them. (What
"measured" means, and why a render that measured nothing writes nothing:
`CLAUDETTE_LOGS.md`.)

## Segments

| Name | Shows | Notes |
|---|---|---|
| `folder_stem` | `/dirname` | last path component |
| `folder_2stems` | `/parent/dirname` | last two components |
| `folder_full` | `~/full/path` | home abbreviated to `~` |
| `git` | `(branch ↑1 ⚠↓2 U3 M1 S2)` | red when stale vs remote; `(no git)` outside a repo. U/M/S = untracked / modified / staged files in the whole repo, from any subfolder (one `git status`, `git_state.py`) |
| `model` | model + effort level | effort the session runs at, launch flag (`--effort`) and `/effort` included: the `effort` Claude Code sends the status line, else (a client that sends none) the newest answer in the transcript; never the settings files, whose default a session may not be running at (`scripts/effort.py`) |
| `context` | `c:42%\|84k/200k(60m)` | context window usage, colour by fill; yellow `?` where Claude Code sent no reading — see below. `(60m)`: minutes the prompt cache stays warm, counted from the last request sent (one still being answered included); grey parentheses, the number red under 10 (a 5-minute cache is always red) and green otherwise; `(0m)` once cold (expired — a tool running longer than the TTL does that —, compacted, or another model). Read from the API's own `cache_creation` in the transcript, never from what the model is told (`scripts/cache_ttl.py`); computed per render, so it stays at its last value while the session sits unredrawn |
| `usage` | `s:12%/3h  w:8%(F:15%)/5d` | 5h/7d rate limits, projection-coloured, scoped model limits, `(auth?)` hint; yellow `?` where a value is not known yet — see below |
| `repos` | `(↑my ↓claudette)` | claudette's own two repos — see below. Was `old_claudette` until 2026-08-12; the old name still works |
| `clock_hms` | `14:03:22` | render time |
| `clock_dhms` | `Sat 14:03:22` | render time with the weekday — a line can sit unredrawn for days |
| `clock_ymdhms` | `2026-08-07 14:03:22` | full timestamp variant |
| `claude_version` | `v2.1.9` | Claude Code version |
| `account` | login email | tells one account from another |

A segment that has nothing to show (no git repo staleness, no account email)
renders empty and is dropped from the join — no double separators.

## `?` — unknown, never a stand-in number

**A yellow `?` means the value is not known.** A missing number is shown only when
evidence gives it (a reading this machine measured, or 0% after a reset known to
have passed, below), never as a plausible default. Where it shows up:

| Render | Means |
|---|---|
| `s:?/?` or `w:?(F:15%)/?` | no reading of that window anywhere: not in Claude Code's payload, not in this session's previous renders, not in this machine's last reading of the same account — typically the first renders of a new session on a machine that has none |
| `s:0%/?` | that window's reset is known to have passed and nobody on this machine has measured the new window yet, so its reset time is not known |
| `s:12%/?` | the percentage is known, the reset time is not (each `?` is its own field) |
| `c:?\|?/1000k` | Claude Code sent no context reading: after a compaction (it also compacts an idle session on its own, about 55 min after the last turn) or before a session's first call. The next turn brings the real number |
| `⚠↓?` in `git` | the remote moved but the count is not local — red arrow (still a warning), yellow `?`. The `git_auto_fetch` feature makes it an exact `↓N` |
| `⚠↓? nofetch` in `git` | as above, and `git_auto_fetch` is on but this repo is not opted in — `"git_auto_fetch_default": true` (with a note) in its `.claudette.json` (every clone) or `.claudette.local.json` (this machine). With the repo opted in, `↓?` turns into an exact `↓N` after the background fetch |

**When the payload lacks a usage window.** Claude Code puts `rate_limits` in the
status-line payload only once the session has had its first API response, and at
a window's reset it redraws every open session without that window until the
session's next response; a window also goes missing for a few renders away from a
reset. For such a window the line shows, in order: the highest reading of the
live window this machine holds (this session's previous renders, then
`claude_usage_last.json` of the same account), with its reset time; else `0%`
with reset `?` when a known reset of that window has passed; else `?`. Fable
(`F:`) comes from the usage-endpoint cache instead, and past its reset time it
shows `0%` until the next refresh lands. Display only: the logged `rate_limits`
and caps stay as received, while the rendered `line` / `line_global` strings
carry what was shown, as they always have. Before 2026-09-17 a missing window
rendered `s:0%/5h  w:0%/0d`, which reads as "usage wiped"; until 2026-10-16 it
rendered `?` even right after a known reset. Why, and what was observed, in
`CLAUDETTE_DESIGN.md` (design notes, not published: "Unknown is not zero", "At a window's reset").

An **idle** session keeps whatever its last redraw showed, because Claude Code
redraws the line on events only, a window's reset among them. A number on an
idle line is as old as that redraw: a `0%` after a reset stays `0%` even if
another machine has started the new window since. `clock_dhms` shows how old the
line is. The context `?` was added on 2026-10-14 (why, in `CLAUDETTE_DESIGN.md`).

## `repos` — claudette's own two repos

One label per repo claudette spans, each with one arrow saying what it needs:

| Render | Means | Do |
|---|---|---|
| `↑my` | the personal `~/.claude` repo holds local work (uncommitted files or unpushed commits) | commit/push it |
| `↓claudette` | the plugin clone is behind its remote | `/claudette` pull |
| `⇅claudette` | parted histories after a rewrite | save any local change, then reset the clone to its remote — **not** a pull |
| `⚠claudette` | the remote REFUSED the credential (expired/revoked token) — staleness can no longer be observed | re-auth: `git -C <repo> ls-remote origin`, then `/claudette update` |
| *(nothing)* | both clean and current | — |

- **`my` is always first**, whatever fired: a segment whose items move around
  has to be re-read instead of recognised by position.
- **Order is also why `claudette` is last** — a version, if it is ever added,
  belongs to `claudette` and would read as `my`'s if it sat in the middle.
- **`my` needs the `my_root_repo_sync` feature on.** Without that consent
  claudette never looks at `~/.claude`, so the label never appears. In the flat
  layout the two paths are the same repo and it renders as `claudette` alone.
- **Both labels render even when you stand in one of the repos** (changed
  2026-08-13; it used to skip the cwd's repo). The git segment says the same
  thing, but it moves with the cwd — and the cwd is often an *agent's* `cd`,
  not yours. This segment is the fixed-position reflex read: its shape must
  not depend on where the session happens to stand, so saying a fact twice
  beats leaving a gap where your eye expects it.
- **Pink, not red.** Red is the git segment's, for the repo you are working
  *in*. For anyone but claudette's author the plugin is read-only, so "behind"
  is the normal state between upstream pushes and a warning there would be a
  false alarm nearly every time. Two exceptions are red, both because
  `/claudette update` cannot fix them: `⇅` (the obvious remedy is the wrong
  one) and `⚠` (a dead credential means "behind" is no longer observable).
- **Orchid (`38;5;177`), not the git segment's magenta.** The two segments sit
  two positions apart and shared one colour until 2026-08-12, which made them
  read as one thing — the failure that costs you is syncing the wrong repo.
  256-colour, not bright-magenta (`95`): that is a theme palette slot many
  themes render identical to magenta, so it would not have separated them.
- Behind wins over local work on the same repo — one arrow each, and pulling
  comes first anyway.
- "Behind" is free (the `git_upstreams` ls-remote cache). Local work needs a
  real `git status` (~37 ms/repo), TTL-cached 60 s in
  `logs/git_local_state.json`.

Example — clock first, no account, full timestamp:

```json
{ "statusline_segments": ["clock_ymdhms", "folder_2stems", "git", "model", "context", "usage"] }
```

## `statusline_custom` — keep your own status line

If you already have a status line you like, wrap it instead of losing it: set
`statusline_custom` to a Python file defining one function:

```python
def get_statusline(ctx):
    # ctx["segments"]  dict: segment name -> rendered ANSI string (may be "")
    # ctx["order"]     list: the configured segment order
    # ctx["separator"] str
    # ctx["line"]      str: claudette's fully rendered line
    # ctx["data"]      dict: the raw status-line JSON from Claude Code
    #                  (workspace, model, context_window, rate_limits, cost, …)
    # ctx["scoped_limits"]  list|None: model-scoped limits (e.g. Fable weekly)
    return ctx["line"].replace("\033[35m", "\033[34m")   # e.g. recolour
```

Contract:

- Called **in-process on every render** — keep it fast, no subprocesses.
- Return a non-empty string to replace the line; anything else (or any
  exception) falls back to claudette's rendered line. Your script can never
  break the status line.
- Claudette always renders from segments first, so `ctx["line"]` is available
  for light-touch wrapping (recolouring, prefixing) and `ctx["data"]` +
  `ctx["segments"]` for full rebuilds.
- The snapshot logging happens after your function, on claudette's data — a
  custom line changes what is displayed and what `status`/`stats` echo, never
  what the throttle/watchdog/done features read.
