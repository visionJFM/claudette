# Throttle - Principles

Before IMPROVING this skill, read `THROTTLE_DESIGN.md` (design notes, not published: purpose, requirements,
design history, past mistakes). Not needed at runtime.

Token-budget governance for Claude Code sessions. Two layers:
1. **Hard hook** (`hook_throttle.py`, PreToolUse): mechanically pauses heavy
   tools (WebSearch, WebFetch, Agent, Task, Workflow) when account usage is over the
   curve. Always on by default; bypass with `/claudette:throttle off`.
2. **Soft planning** (this doc + CLAUDE.md trigger line): estimate before big fan-outs,
   plan long jobs as chunked scheduled runs.

## Units (pace = fresh tokens per hour)
- A job's pace budgets its OWN fresh tokens (input + output + cache_creation; cache
  READS excluded) per hour: `slow` = 100k/h, `med` = 300k/h, `fast` = 1M/h, or explicit
  `<N>k/h` / `<N>M/h`. Fresh tokens are directly measurable per subagent
  (`subagent_tokens`, approximately fresh) - no conversion, no model math, immune to
  parallel sessions.
- Deliberately NOT in %-of-window: the % is Anthropic's hidden cost function (per-model
  rates, cache discounts, allocation changes week to week). We never reproduce it; the
  caps guard consumes it directly instead (authoritative, account-wide, no-op above cap).
- `$X/h`: convert using the CURRENT session model's API price for fresh tokens (confirm
  the model first) and label the conversion (calc). A `%` pace request: map to the
  nearest token tier and say so in the plan.

## Curves (allowed cumulative usage; account-wide)
```
h = hours until five_hour.resets_at (decimal)   cap5 = 100 - 11*h   (45% at window start)
d = days  until seven_day.resets_at (decimal)   cap7 = 100 - 8*d    (44% at week start)
```
- Rationale: rolling-window quota does not roll over - unspent budget is wasted, so the
  cap is conservative early and permissive as the reset nears.
- Precomputed every status render into `claude_usage_last.json` under `caps`.
- Throttled when `used5 > m*cap5` OR `used7 > m*cap7` (each window vs its own curve).
- `m` = session multiplier: 1.0 interactive default; background jobs launch with
  `CLAUDE_THROTTLE_M=0.6` (or per-job) so they yield to interactive sessions first.

## Hook behavior (hook_throttle.py)
- Heavy tools only; everything else always passes. Overage clearing in <=55s: silent
  sleep. Larger: deny with instructions (do not retry heavy tools; local work or end
  turn) + a user message at most every 2 min: cap catch-up time and the bypass hint.
- **Fail-open**: snapshot missing or older than 30 min, parse errors, m<=0 => allow.
  Consequence: headless (`claude -p`) runs have no statusline, so the hook only bites
  there while another live session keeps the snapshot fresh. Headless jobs must
  self-guard (subagent preflight, below).
- Control file `logs/throttle_control.json` (single writer: /claudette:throttle skill):
```json
{ "default_m": 1.0,
  "global":   { "off": true, "m": 0.8, "expires": 1784240000 },
  "sessions": { "<session_id>": { "off": true, "m": 0.6, "expires": 0 } } }
```
  Precedence in the hook: live `sessions[id]` > env `CLAUDE_THROTTLE_M` > live `global`
  > `default_m`. Entries with past `expires` are ignored (auto re-arm).

## Pre-flight rule (soft, interactive sessions)
Before starting a fan-out or research sweep expected to cost >2% of the 5h window
(~2-3 parallel agents or more), state the estimate. If it would push usage past the
current cap, ask before proceeding: proceed / downscope / defer.

## Measuring spend
- **Ledger**: every Agent result reports `subagent_tokens`; the orchestrator appends
  `{ts, items, tokens, model}` per subagent to EXECUTION_STATE. That ledger IS the
  job's spend record. Never use account used% deltas for measurement: integer
  granularity hides sub-1% burns and parallel sessions pollute them.
- **Enforcement**: `tokens_per_firing <= pace_tokens_h * interval_h`. Adapt every
  firing: `chunk_size = floor(firing_budget / median_tokens_per_item)` from the
  ledger; clamp (<=2x growth per firing, <=25-30 items per subagent - split across
  several subagents beyond that; minimum 1). Costly items shrink the next chunk, easy
  ones grow it; log recomputations so drift is auditable.
- **Wall-clock is a third constraint**: verification depth tends to creep
  (tokens/item grew 8.6k -> 21k in one campaign), and throughput - not tokens - can
  become what binds a deadline. Fix with parallel disjoint-item subagents and a
  per-item effort budget, not by raising pace.
- **Guard = the real protection** (unchanged): skip the firing when account
  `used% > m*caps` for either window. Authoritative Anthropic numbers; pace and guard
  are never mixed - pace limits own burn, guard yields to everyone else.
- **Model awareness**: usage cost is weighted by model, so the chunk-run model is a
  throttle lever. Record it per ledger entry; a switch changes tokens/item and any
  $ math - the adaptive controller absorbs it, the ledger explains it. Do not trust a
  session's self-report of its model when provenance matters: verify via the
  transcript's per-message `model` field (a server-side fallback is silent).
- **Calibration file** (`logs/throttle_calibration.json`): PASSIVE
  observation log `[own_tokens, d_used5, iso]` for a future claudette estimator.
  No throttle decision may depend on it today.

## Long-job doctrine (`/claudette:throttle <task> [pace]`)
The orchestrator is a checkpoint file, not a session. Sessions are disposable; state
lives on disk. Never leave one session running for days.

### Limit domains and step types
Every step consumes one or more LIMIT DOMAINS; a step proceeds only when ALL its
domains have headroom and runs at the min across them (worst-of):
- **Account windows** (the guard): shared, used% vs caps, mild self-healing penalty
  (wait for reset). A premium model drains it faster at equal token pace - a
  planning lever (per-stage model + ETA), never per-model curves.
- **External resource rates** (a site/endpoint/IP): requests per time, opaque block
  signals, often leaky-bucket refill (a slow drip refills faster than it drains),
  penalties can be severe (e.g. a 24h IP ban). Limits are DISCOVERED, not
  documented: start at a moderate polite rate with jitter; on failure probe, then
  titrate safest-first, and only when the probes are useful work. Abort the pass on
  the FIRST block (further requests extend the penalty); recover probe-then-pass
  with backoff. Never evade a block by switching tools or identity. Learned safe
  rates are machine-global knowledge - record them in the relevant skill/design
  file, not in one job's INSIGHTS.
- **Wall-clock / deadline** (see PACE): composes the same way.
Steps have two natures: AGENT steps (judgment; token domains) and SCRIPT steps
(deterministic; resource domains, ~zero tokens). Automating the agent OUT of a
mechanical stage is the strongest lean move: a script stage runs as a dumb
zero-token loop branching on machine-readable outcomes (exit codes are the
contract); the agent handles stage boundaries and the exception list only.
Decompose a goal into buffered stages (script stage fills a folder, agent stage
consumes it) ONLY when it buys one of: tempo mismatch (~10x between domains),
penalty asymmetry (isolate the severe-penalty domain from a sprinting agent),
standalone artifact value, outage decoupling (the buffer absorbs a ban on one side
and a busy window on the other), or token elimination. Otherwise keep one combined
increment under worst-of throttling. A decomposed pipeline is JOB COMPOSITION - no
new machinery: each stage is its own job (own three files; ledger in its own unit,
requests for script stages, tokens for agent stages), chained by the buffer folder
(stage 1's DELIVERABLE is stage 2's input).

### Invariants (hold for EVERY job; not user-tunable)
1. ONE blocking touchpoint: the interview. Nothing after it may wait on the user;
   later changes are announced with a non-blocking adjust path.
2. Guard always on, generalized to EVERY limit domain a step touches (worst-of):
   for the account domain, skip the firing when used% > m*caps (either window);
   for a resource domain, block detection + its proven-safe rate. Pace bounds own
   fresh tokens/h; pace and guard are never mixed.
3. Lean orchestrator: never Read large data files or do chunk work inline; scan via
   scripts or subagents; each firing adds only a few hundred tokens of context.
4. Chunks are BOUNDED (<=25-30 items/subagent), ATTRIBUTABLE (one ledger entry per
   subagent), IDEMPOTENT (redoing a done item is harmless). Parallel subagents are
   allowed only on explicitly disjoint item sets.
5. Calibrate in production shape before scheduling: a subagent executing PROMPT,
   never inline work in the orchestrator (measures 10x wrong, learned 2026-07-16).
6. Everything resumable from disk: a kill or reboot loses at most the in-flight chunk.
7. In-session scheduling only (CronCreate). Never the OS scheduler.
8. Verify content, not liveness: a live URL or image can show the WRONG product;
   check what it depicts against the item's own fields.
9. Completed per-item marks are authoritative and never redone, regardless of how
   the job was discovered.
10. Report slips and coverage caps; never silently miss a deadline or drop scope.

### Slots (bound at the interview; bindings recorded in EXECUTION_STATE)
Named variables the doctrine references without fixing their implementation. Each has
a DEFAULT - the interview confirms deviations; it does not open-question everything.

- **DELIVERABLE** - where results land: existing project data (an enrichment campaign: fields
  in the database rows), new sidecar files (an audit campaign: one report per module), or
  an action. Includes the editing policy: what must never be modified (e.g. source
  code read-only, findings-only). Default: follow the project's own conventions -
  results belong to the project, not the skill. In a staged pipeline, a stage's
  DELIVERABLE (e.g. a buffer folder of fetched files) is the next stage's input.
- **EXECUTION_STATE** - campaign state that is not a result: config, counters, spend
  ledger, stop flag, per-item attempts/blockers. Default: `<job>_status.json` in the
  RELEVANT working folder, registered name -> path in
  `~/.claude/skills/claudette/logs/throttle_registry.json`, with `"generator": "throttle"`
  inside as grep fallback; project-less jobs use
  `~/.claude/skills/claudette/logs/throttle_jobs/`. Per-item fields may embed inside
  DELIVERABLE instead (e.g. a `_validation` object per record, stripped at build).
  Required: goal/done-bar text, counters, pace+deadline, chunk rule, model, `stop`
  flag (checked before every chunk), ledger. No skill-branded folders - the mark in
  a project belongs to the job.
- **PROMPT** - the chunk-run instructions subagents execute: `<job>_prompt.md` beside
  EXECUTION_STATE. Encodes the subagent side of the firing loop (below) so any fresh
  context can run a chunk correctly.
- **INSIGHTS** - cross-run memory: `<job>_insights.md`. Runs read it before working
  and append NEW findings only (methods that work, sources that block, per-source URL
  patterns, cost surprises). This is what makes disposable fresh-context runs viable.
- **DONE_BAR** - what "done" means per item and for the job, including the grey zone:
  attempts cap (default 2, then terminal); searched-but-not-found counts as done with
  a note (default yes); what escalates to the user vs auto-fix (default: changes that
  redefine an item's IDENTITY are flagged for review; corroborated value fixes are
  applied); error policy (default breadth-first, mark failures, second pass at end).
- **PACE** - tier (default med 300k/h; state the default in the plan), optional
  DEADLINE, and the chunk-run model. Pace is a CEILING, deadline is a TARGET:
  chunk_size = max(deadline-driven items/firing, pace-driven), never above the pace
  ceiling; if the ceiling binds, the deadline slips and you say so (invariant 10).
  Recommend a cheaper model tier for mechanical bulk work (fetch/verify/fill),
  session/premium tier for judgment-heavy chunks; pass it as the subagent model
  override and calibrate under the same override.
- **LIFECYCLE** - one-shot (the completing run leaves a one-line summary per project
  convention, then deletes the three job files + registry entry) vs recurring (files
  are a permanent named process; a rephrased future prompt must match via the
  registry and resume/refresh oldest-first, never re-plan from scratch). Includes
  git-tracking of job files (tracked = cross-machine resume + history; untracked =
  gitignore `<job>_*` AND never `git add -A` in run commits).
- **YIELD** - partial-yield cadence (default: validate/build + commit + push every
  chunk so a stop loses nothing) and completion/stall notification.

### Phases
1. **Discover** - check the registry, then grep for `"generator": "throttle"`.
   RESUME requires an actual EXECUTION_STATE file; it skips the interview (answers
   are on disk) and skips calibration only if the ledger survived. Git-history
   traces alone are NOT a resume: honor completed per-item marks (invariant 9) but
   run the interview - PACE, DEADLINE and DONE_BAR were never on disk (learned
   2026-07-17: a fresh session promoted git traces to "resume", skipped the
   interview, and missed the user's 8h deadline expectation).
2. **Interview** - ONE AskUserQuestion round binding the slots. Batch everything;
   state the defaults you chose; skip questions whose answers are obvious from
   context. The user may leave right after answering. Run the triage scan (phase 3)
   cheaply BEFORE the interview when decomposition could change the questions - a
   staged-pipeline recommendation belongs in the interview, not after it.
3. **Estimate and triage** - item count, then per LIMIT DOMAIN: which stages a
   script can own vs which need an agent, which domains each stage touches, and
   items x per-item cost in each. Trivial in every domain (e.g. ten videos) => no
   harness, just do the task. Large in some domain => apply the decomposition rule
   (see Limit domains) and STATE the call in the plan. For agent stages: guessed
   tokens/item, total, wall-clock at PACE, runs needed. Stay lean (invariant 3).
4. **Calibrate** - write PROMPT, spawn ONE subagent on a normal-size chunk under the
   chosen model override, watch its result, measure tokens/item from
   `subagent_tokens`, seed INSIGHTS with the distilled method, recompute the plan.
   Also required on a resume whose ledger was lost.
5. **Schedule** - interval from PACE: `chunk_cost <= pace * interval`, per-run
   overhead (boot + reads; assume 0.3-0.5%) under ~15% of the run budget. Prompt
   cache lives ~1h (drops to ~5 min under usage overage): from a fat session
   (>150k ctx) pick interval <= 1h (halve the chunk) or tell the user a fresh
   session would orchestrate cheaper; count this overhead as job spend. Register
   the cron immediately - do NOT ask approval (invariant 1) - then ANNOUNCE the
   plan (cost, ETA, schedule, state locations, stop lever) and the adjust path:
   "adjust the pace/schedule" anytime, or edit EXECUTION_STATE (pace, chunk, stop);
   the next firing picks changes up. Cron auto-expires after 7 days and dies with
   the session - by design; `/claudette:throttle resume <job>` in any new session continues.
   The user can `/background` the orchestrating session to free the terminal.
6. **Firing loop** -
   Orchestrator side (encode in the cron prompt): skip if the previous firing is
   still running; spawn subagent(s) on disjoint item sets; append one ledger entry
   per subagent; add the calibration observation; recompute chunk_size (median
   tokens/item, <=2x growth); RECOUNT counters from per-item marks (parallel agents
   race counters); watch wall-clock vs DEADLINE; relay a one-line status.
   Subagent side (encoded in PROMPT): read EXECUTION_STATE, exit if `stop` or done;
   guard - read `claude_usage_last.json`, snapshot older than 30 min => treat used% as
   `used + 10*age_h`; if over caps exit quietly ("skipped, window busy"); read
   INSIGHTS; process the chunk within an effort budget (~8-12 tool calls/item - one
   good primary source beats three cross-checks; cross-verify only on conflict or
   implausibility); update per-item state; append NEW insights only; YIELD per
   policy; reply <=4 lines.
   Parallel repo caution: shared checkouts race (observed 2026-07-17 - one agent's
   commit swept up its sibling's in-progress edits); prefer worktrees, else
   pull-rebase-push on disjoint keys and verify completeness after the firing.
   Last run: mark done, unregister the schedule, LIFECYCLE cleanup, write the
   summary, notify.

## Known limitations
- claude.ai/code web sessions: no local hooks, outside this system entirely.
- VS Code / desktop: hooks run, but no statusline renders there; snapshot freshness
  depends on a terminal session being open somewhere. Stale => hook dormant (by design).
- `/claudette:throttle off` requires a user message; mid-turn denials surface first, then you type it.
- Rate limits are account-wide: the snapshot is valid across parallel sessions, but
  `cost_usd`/`ctx_*`/`session_id` in it belong to whichever session rendered last.
- Cross-item defects (duplicates, systemic field drift) are invisible to item-at-a-time
  chunking; if they matter, add a dedicated cross-item pass to the plan.
