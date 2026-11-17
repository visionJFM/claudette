---
name: throttle
description: Token-budget throttle. Instant bypass (/claudette:throttle off), re-arm (on), status, default multiplier, or plan a long paced background job (/claudette:throttle <task> [slow|med|fast|N%]).
argument-hint: off [--all] [--for 2h] | on | status | m 0.6 | <task description> [slow|med|fast|N%|--rate $X/h]
---

# /claudette:throttle

Controls the token throttle system. Read `THROTTLE.md` in this folder for principles
(curves, units, hook behavior, control file schema) before anything beyond `off|on|status`.

Paths:
- Control file: `~/.claude/skills/claudette/logs/throttle_control.json` (this skill is its ONLY writer)
- Usage snapshot: `~/.claude/skills/claudette/logs/claude_usage_last.json` (read-only here)
- Job state: three job-named files in the RELEVANT working folder - `<job>_status.json`
  (config + counters + ledger, carries `"generator": "throttle"`), `<job>_prompt.md`
  (chunk-run instructions), `<job>_insights.md`. No skill-branded folders: the mark in a
  project belongs to the job, not the skill. Plain files, not temp: durability IS the
  feature (reboot/kill -> resume with completed-work marks and insights intact).
- Registry: `~/.claude/skills/claudette/logs/throttle_registry.json` maps job name -> path
  (single writer: this skill). Fallback discovery: grep for `"generator": "throttle"`.
  Project-less tasks put their files in `~/.claude/skills/claudette/logs/throttle_jobs/`.

Parse `$ARGUMENTS` and dispatch:

## `off [--all] [--for <duration>]`
Bypass the hook. Write into the control file:
```json
{ "global": { "off": true, "expires": <epoch> } }
```
- `expires` default: the CURRENT 5h window reset (`rate_limits.five_hour.resets_at` from
  the snapshot). Fall back to now+2h if snapshot missing. `--for 2h` overrides.
- Merge into the existing file, preserve other keys (`default_m`, `sessions`).
- v1 note: `off` is global (all sessions including background jobs). `--all` is therefore
  implicit; accept and ignore the flag.
- Confirm to the user: what was disabled and until when (local time).

## `on`
Re-arm: remove `global` and all `sessions` entries from the control file (keep `default_m`).
Confirm.

## `m <value>`
Set `default_m` in the control file (e.g. `m 0.8`). `m 1.0` is the normal curve;
lower yields earlier. Confirm with the resulting caps at current h/d.

## `status`
Read snapshot + control file, report concisely:
- 5h: used% vs cap (`caps.five_hour`), h_remaining; 7d: used% vs cap, d_remaining
- effective m / off state (with expiry), snapshot age (flag if > 30 min: hook is dormant)
- jobs listed in the registry (flag entries whose `_status.json` is missing as stale)

## `resume <job>`
A job's `_status.json` exists (via registry, or generator-grep) but its session (and
cron) is gone - e.g. after a reboot or an accidental session kill. Read it + THROTTLE.md,
regenerate the job's `_prompt.md` if it predates current doctrine, re-register the
in-session cron with the job's chunk/interval, confirm counters to the user.
Do not re-run the interview; re-calibrate only if the ledger was lost.
Resume REQUIRES an actual `_status.json`. Git-history traces or per-item marks alone
are NOT a resume: honor completed marks (never redo) but take the new-task path below,
interview included - pace, deadline and done-bar were never on disk.

## `<task description> [pace]`
A long-running background job. FIRST check the registry for an existing job covering
this task (rephrasings of a recurring job must match): if found, say so and switch to
`resume` (include restart-from-scratch as an interview option only if the user's wording
suggests it). Per-item marks in the project data (e.g. `_validation`) are completed
work: never redo stamped items either way.
Otherwise load `THROTTLE.md` and follow "Long-job doctrine" exactly:
discover -> interview (bind the slots) -> estimate -> calibrate -> schedule -> report.
Do not skip calibration.
Pace keywords (fresh tokens/h): slow=100k, med=300k, fast=1M; asap=no pacing (still
cap-guarded); explicit `<N>k/h`|`<N>M/h`; `$X/h` or `N%` mapped per THROTTLE.md Units.
No pace given -> default med (300k/h); state the chosen default in the plan.
Confirm the session model at planning time; record it in the job file.
