# Claudette — Claude Code Config Index

Claudette is a set of Claude Code features (status line, hooks, audio cues, dot-commands,
skills, CLAUDE.md rules) that every install offers alike, on Windows and macOS. Each machine
chooses which ones it runs: nothing is applied before someone answered yes for it on that
machine, and the answers stay there (`prefs.local.json`, untracked). It loads as a plugin from
`~/.claude/skills/claudette/` and merges into the existing `settings.json` rather than
replacing it.

What travels between machines is the user's own `~/.claude` root repo, which the status line calls
`my`, with its CLAUDE.md, personal skills and notes. Claudette touches it only with the
`my_root_repo_sync` feature on; `/claudette update` then pulls it and `/claudette push` pushes
it. An install only pulls the claudette clone itself.

**This file is an index: find the matching row, read ONLY the file or run ONLY the command it
names.** Each `CLAUDETTE_*.md` it names sits beside it in `docs/`; any other path starts at the
claudette folder (`~/.claude/skills/claudette/`). Much of what happens in a session (status line, sounds, denied tools, injected prompts)
is a claudette feature — when something has no other explanation, it is probably a row here.

## Something unexplained is happening
| You see / hear | It is | Go to |
|---|---|---|
| Status line segments — `c:20%`, `(60m)` after the context tokens, `w:44%(F:31%)`, `⚠↓N`, `(↓my ↓claudette)`, arrows, colors — or want it reordered/trimmed/wrapped | the `statusline` feature | `CLAUDETTE_STATUSLINE.md` |
| A tone after a turn, on a permission prompt, on failure; a spoken voice reading notifications | the `audible_*` features | `/claudette prefs` lists them; `--explain <id>` says why; volume/notes/device are params |
| A tool call denied or paused "for pacing" | the token throttle | `/claudette:throttle` skill (`off` bypasses instantly) |
| A prompt or report about "auto-done" appearing while away, ~50 min after going idle, a compaction "blocked by PreCompact hook", or `[claudette compact-done]` in context | the `auto_done` feature | `.autodone` (free) shows its state for this session; `/claudette:auto-done` skill; why: `CLAUDETTE_DESIGN.md` "auto_done" |
| An idle session pushing readings to a usage chart, a `done` line saying the chart is behind or handed to a background session, a short-lived background Claude session nobody started | the `chart_autoupdate` feature (the chart runner) | `/claudette:chart` skill (`.chart` = link + state, free); `chart_runner.py status` = the last background update |
| `nofetch` after `↓?` in the status line; a repo fetched in the background | the `git_auto_fetch` feature | `/claudette prefs` (`--explain git_auto_fetch` says how a repo opts in); write an opt-in with `overrides.py set` rather than by hand (`CLAUDETTE_REGISTRY.md`) |
| A warning that a repo is behind before editing it | the stale-repo check | rule is in CLAUDE.md's claudette block; cache contract in `CLAUDETTE_LOGS.md` |
| Any rule in `~/.claude/claudette-instructions.generated.md` (imported by CLAUDE.md; generated, never edit) | a feature named by the comment above it | `/claudette prefs` to change or switch it off |
| A number that needs tracing (usage %, limits, caches) | a file under `logs/` | `CLAUDETTE_LOGS.md` — schemas, single writers |
| A console or program window you did not open; a process nobody remembers starting | possibly launched by a Claude session | type `.proc`: what runs under each live session, and what outlived the call that started it, with that call's reason (free); `.proc :all` also lists what no call explains |
| Status line empty or hooks silent right after install/update | the interpreter the hooks run (absolute path, `python` param) | `/claudette doctor` — the `python (hooks)` row; design in `CLAUDETTE_DESIGN.md` "Absolute interpreter path" |

## Doing something
| Task | Go to |
|---|---|
| What runs on THIS machine; switch a feature on/off; tune a parameter (tones, volumes, statusline segments, owner token) | `/claudette prefs` |
| Install or update claudette; decide about new features | `/claudette update` |
| Which version runs here, what a version changed | `version` in `.claude-plugin/plugin.json`; `CHANGELOG.md` |
| An update that cannot update claudette itself (repo moved, history rewritten, an edited file in the way) | reinstall: `README.md`, "Detailed install instructions for agents" (`scripts/move_into_place.py` keeps answers and logs) |
| Something claudette-managed not working | `/claudette doctor` |
| Push the personal `~/.claude` repo (`my`); undo/disable/uninstall | `/claudette push` (needs `my_root_repo_sync`); `CLAUDETTE_UNDO.md` |
| **Author a skill** — any skill, not just claudette's: where it goes, naming (owner prefix), gitignore hygiene, per-machine subset | `CLAUDETTE_SKILLS.md` |
| **Add a feature or a hook** to claudette; retire one | `CLAUDETTE_REGISTRY.md` |
| **Capture an idea** about Claude workflow or claudette's evolution | `~/.claude/<owner>-ideas/` — read its `README.md` first. Substrate-gated: needs `my_root_repo_sync` on and the folder present; without them there is no ideas mechanism — do not invent a fallback (I4) |
| Session questions for zero tokens — prompt history, other sessions (list/search/resume), project folders (list/open a new session), processes sessions started, persistence gaps, usage, watchdog switch, a note on this moment (**dot-commands**) | type `.` — it lists them; `..` = the in-context/phone form |
| Leaving a session — "is everything persisted?" | `/claudette:done` (facts only, free: `.done`) |
| **The usage chart** — how full the week is across every machine: open it, refresh it, add a machine | `/claudette:chart`; `.chart` for the link alone, free |
| Historical cost/usage reporting | out of scope — transcript-reading tools do it; claudette acts on live limits only |

## Working on claudette itself
| Task | Go to |
|---|---|
| Code map, conventions (**no personal content in this repo — ever**), settings-merge contract, platform idiom | `CLAUDETTE_DEV.md` (developer notes, not published) |
| Log file contracts (schemas, frozen keys, single writers) | `CLAUDETTE_LOGS.md` |
| WHY it is built this way; changing behavior | `CLAUDETTE_DESIGN.md` (design notes, not published — as is every `*DESIGN*.md` cited in these docs and in code comments) — read before "improving"; most obvious alternatives were already rejected there |
| Measuring what a skill or a turn costs | `scripts/profile_skill.py` / `scripts/profile_turn.py` (documented in `CLAUDETTE_DEV.md`) |

When claudette gains a feature or an entry point (a skill, a verb, a doc, a symptom no row
explains), add its row here, since a capability this index cannot route is invisible to every
session that did not build it. A behavior inside a feature that already has a row (a
dot-command's options, what a segment means) goes in that feature's own doc rather than here,
so that this index stays short enough to read whole.
