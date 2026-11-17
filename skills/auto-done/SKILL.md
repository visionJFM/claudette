---
name: auto-done
description: Turn the idle auto-done report on or off, for this session or globally, report its state, or change its mode. The auto_done feature runs claudette-done by itself in a session left idle while its prompt cache is still warm - ~50 minutes after the last turn (idle timer; the cron watchdog in modes cron and both) or right before Claude Code compacts the idle session (gate) - and, in its default mode, only on a 1-hour prompt cache. Use when auto-done fired at a bad moment, when a long-running or scheduled-work session should not be interrupted, to check whether it will run in this session, or to pick cron / idlecompact / both / auto.
---

# Auto-done — on / off / state / mode

A session left idle goes cold, and the `/claudette:done` you eventually run on it costs several
times what the same run costs warm. The `auto_done` feature closes that gap with three
mechanisms: `hook_idle_timer.py` waits in the background after each turn and wakes the session
~50 minutes later, with nothing in the conversation until then; `hook_compact_done.py` holds
back Claude Code's idle compaction once to run the report on the full conversation first; and
`hook_watchdog.py`, the older cron watchdog, arms a one-shot cron ~50 minutes after your last
prompt (modes `cron` and `both` only). You come back to a finished report per session instead
of paying to generate one.

**The prompt cache decides.** Every mechanism only makes sense while the cache is warm. In the default
mode (`auto`) they run only in a session on a **1-hour** prompt cache, read from what the API
reported in the transcript (`scripts/cache_ttl.py`) — never from what the model is told. On a
5-minute cache nothing runs: a report there would run cold, and Claude Code never compacts such a
session. The `prompt_cache_1h` feature sets `promptCacheTtl: "1h"` where the client chose 5
minutes on a subscription; the status line's `(60m)` shows the minutes left.

**The fired run is report-only.** It never commits, stages, pushes, or writes a memory file.
The one exception is the usage chart when `chart_autoupdate` is on: publishing it asks for no
approval, so the away run updates it like a typed `done` would (design notes, not published: `skills/chart/DESIGN.md`,
"Unattended updates, again"). You are away; it tells you what it found and you decide when you
return.

**The mode** — parameter `auto_done_watcher`, set with
`bash "$HOME/.claude/skills/claudette/scripts/run.sh" features_apply.py --param auto_done_watcher=<mode>`
(`=null` back to the default):

| Mode | Runs |
|---|---|
| `auto` (default) | the gate, and the idle timer for sessions the idle compaction will not reach; 1-hour cache only |
| `cron` | the cron watchdog alone, whatever the cache (the old `auto_done_scheduler`) |
| `idlecompact` | the gate alone (the old `compact_done`) |
| `both` | the cron watchdog and the gate, whatever the cache |

`auto` ran the cron watchdog instead of the idle timer until 2026-10-17; the timer is on trial
before the cron is retired. Whatever the mode, a report already written is kept through the
compaction and shown again after it. The switches below stop every mechanism, and a session's
own switch follows it through `/clear`.

## Fastest path: type the dot-command, don't invoke this skill

The `.autodone` dot-command answers these on prompt submit for **zero tokens** — no skill load,
no tool call, no turn (`.` lists every dot-command; `..autodone …` = same answer in context, for
the phone app). Prefer them:

| Type this | Effect |
|---|---|
| `.autodone` | report current state: the switches, the mode, and what this session's cache decides |
| `.autodone off` | disable for **this session** |
| `.autodone on` | re-enable this session |
| `.autodone off all` | disable **everywhere** (all sessions, all projects) |
| `.autodone on all` | re-enable everywhere |

This skill exists so the feature is **discoverable** — typing `/claudette` lists it next to the
other claudette commands. Reaching it through the slash command costs a skill load and a turn
that the dot-command does not; say so once and use the dot-command.

## When invoked anyway

Do the operation directly, then reply in one line. The switches are plain marker files —
presence means disabled, contents are only a timestamp (global:
`~/.claude/skills/claudette/logs/watchdog_off`; this session:
`.../logs/watchdog/<session_id>.off`). Drive them ONLY through the switch script — one command
per operation, run verbatim (rule in the claudette SKILL.md):

```bash
# state
bash "$HOME/.claude/skills/claudette/scripts/run.sh" watchdog_switch.py --state --session <sid>
# off / on (this session)
bash "$HOME/.claude/skills/claudette/scripts/run.sh" watchdog_switch.py --off --session <sid>
bash "$HOME/.claude/skills/claudette/scripts/run.sh" watchdog_switch.py --on --session <sid>
# off / on (global)
bash "$HOME/.claude/skills/claudette/scripts/run.sh" watchdog_switch.py --off --global
bash "$HOME/.claude/skills/claudette/scripts/run.sh" watchdog_switch.py --on --global
```

Never invent a third scope, and never disable globally when the user asked only for this
session — coming back to five sessions that all silently stopped reporting is the failure this
switch exists to prevent, not to cause.

## When to turn it off

- **A session running scheduled work.** Every mechanism already stands down when the session has
  a scheduled job of yours, so this is belt-and-braces — but if a long-running orchestrator keeps
  generating work, tell it `.autodone off` and run `/claudette:done` yourself at the end.
- **Anything unattended you do not want touched.** It only reads, but it does consume window.
- **It misbehaves.** Turn it off globally first, diagnose second. One word beats debugging a
  hook that is firing across five sessions.

## What it will not do

- **Survive the session.** Its timers and cron jobs live in the session; `/exit`, closing the
  terminal or rebooting ends them. Sessions that already died need a different tool.
- **Report on a young session, or on a headless one.** Fewer than 3 user turns and it gives no
  report. A `-p`/headless run is never watched: there the timer exits at once, since Claude Code
  would otherwise hold the run until it finished waiting.
- **Report twice for one prompt.** After a report, or a wake that produced none, it waits for
  your next prompt. (The cron's one exception: a stand-down because you were still active
  re-arms itself 50 min out, from the fire — otherwise a session abandoned right after a
  stand-down would never be watched again.)
- **Interrupt you.** The timer counts from the end of the last turn and leaves as soon as any
  turn comes after it; the cron fires only while the REPL is idle, and if you came back within
  45 minutes the fired job stands down without running anything.

Rationale, rejected alternatives, and the tick-interval economics: `claudette/docs/CLAUDETTE_DESIGN.md`
→ "Idle auto-done watchdog".
