# Adding/removing a feature in the registry

Loaded when authoring `features_def.json` — adding a feature, a hook, or retiring one. The
day-to-day verbs must not pay for this prose.

A new entry in `features_def.json` needs `group`, `default` (recommended? true/false, or null
for "must be answered explicitly"), a `name`, the four decision fields — `what` (what it does), `benefit` (what the user gains),
`cost` (side effects, not dollars), `alternative` (the next best option without it) — all
written for someone deciding yes or no, `requires` if it reads another feature's
output, `params` for anything tunable, and its payloads. It reaches nobody until they answer for
it in `update` — which is the point, and why the four decision fields are the most important part.

**`recommend_note` (optional) — when `default` cannot answer honestly.** Some features are worth
it only under a condition the machine cannot see: `chart_autoupdate` earns nothing on a single
machine, and no file on disk knows how many machines there are. Write the condition as prose here
and the flow relays it in the recommendation slot instead of a bare on/off. Keep `default` at the
safe answer; this is not a second `null`. Two things it must never become: a rule the code
evaluates (claudette does not guess at facts the user can simply be asked), or a home for author
reasoning — it is addressed to the person deciding, like the other four.

**`recommend_by` (optional) — when the condition is a fact on this machine that nobody can be
asked.** `recommend_note` exists because claudette does not guess at what the user knows. Some
conditions the user cannot know either: whether Claude Code compacts idle sessions is switched on
from Claude Code's servers, so the only answer is what this machine has seen. Such a fact gets a
reader in `prefs._FACTS` that reads what its own writer left in `logs/` (never a scan: the
registry is read inside hooks and the status line), and the feature maps each observed state to
the recommendation shown in it:
```json
"recommend_by": {"fact": "idle_compaction",
                 "seen":   {"default": true,  "recommend_note": "..."},
                 "unseen": {"default": false, "recommend_note": "..."}}
```
`prefs.features()` applies it, so every reader sees the same recommendation; an unobserved fact,
or a state not listed, leaves the feature's own `default`. It changes only the recommendation:
`--accept-defaults` and the update review answer unanswered features only, so a machine that
already answered keeps its answer, and `/claudette prefs` shows the new recommendation beside it.
`validate_registry` refuses an unknown fact or other keys in a state.

**Config-writing vs script features — only the second kind can have per-project exceptions.**
A config-writing feature's output is one user-wide file: `settings.json`, `hooks.json`, the
generated CLAUDE.md leaf. It applies to every project at once, and an exception for one project
would mean claudette writing into that project's own `.claude/` config — often a shared repo,
someone else's. Don't: Claude Code's own project settings already cover that case. A script
feature decides at the moment it runs, in the repo it runs for, so it can read a project layer:
`<repo>/.claudette.json` (committed, shared by every clone) then `<repo>/.claudette.local.json`
(one machine, kept out of git via `.git/info/exclude`), over the machine's `prefs.local.json`
and the registry default. `scripts/overrides.py` is the one resolver; a script calls
`overrides.py resolve <param> --repo <root>`, never re-implements the merge.

A param opts in under the feature's `param_rules`, beside `params`:
```json
"param_rules": {"leak_scan_classes": {"project": true, "merge": "set",
                "members": ["secret", "host", "user"], "shared_may_remove": ["host", "user"]}}
```
- `project: true` — without it a project file's value is refused as "machine-level only".
- `merge: "set"` — layers send `{"add": [...], "remove": [...]}`, never a whole list, so a member
  added to the registry later reaches every repo. Without `merge`, the most specific layer's
  value replaces the rest.
- `shared_may_remove` — for a guard: what the committed file may take away. Everything else is
  removable from the local file only, so one commit cannot disarm every clone. Adding is never
  bounded: a stricter project is always allowed.
- A param whose default is `true`/`false` takes only `true`/`false` from any layer — `"true"` or
  `1` is refused, not read as truthy. A scalar reports which layer set it (`FROM`).
- `overrides.py set <param> <json> --repo <root> --local|--shared --note <why>` writes one
  override (and the `.git/info/exclude` line for the local file) — what Claude runs when a user
  asks for an override; never hand-edit it for them.
- A feature answer is the machine veto over its own params: a project file cannot switch a
  feature on or off, only tune it while it is on. "Off on this machine whatever the repos say" is
  the feature switch, never a param at the machine layer — a more specific layer overrides that.
- Every project override needs a `notes` entry for its param, or it is not applied.
- The file's `features` key is reserved: no feature supports per-project on/off yet, and one
  that does must be a script feature for the same reason as above.

**Removing one is TWO steps and the second is not optional.** Delete the entry from
`features_def.json`, then add its id to `_RETIRED_FEATURES` in `scripts/features_apply.py` (and
its params to `_RETIRED_PARAMS`), with a dated comment saying why. The registry says what
claudette can do *now*; the retirement ledger is what makes every OTHER machine clean up on its
next `update` — nobody learns of a removal before then. It drops the orphaned answer from
`prefs.local.json`, and `reconcile()` reverts the payload from that machine's own undo record.
Delete the entry alone and every other machine keeps the setting written plus a stale answer
nothing can drop (`--feat <id>=null` refuses: no such feature). `_RETIRED_FEATURE_SETTINGS` is
only for features retired BEFORE undo records existed — see `CLAUDETTE_DESIGN.md` (design notes, not published). Never
hand-edit `prefs.local.json`, `settings.json` or the payload target to finish a removal: that
fixes one machine and leaves the rest broken. `doctor` catches the omission ("removed from the
registry without being retired").

**Retiring a HOOK is THREE steps: leave the script behind as a no-op shim for one update cycle.**
Claude Code reads `hooks.json` once, at session start, so on a machine that had the hook on, the
pull that deletes the script lands *under* a session still pointing at it. A missing script makes
the interpreter exit 2, and exit 2 from a PreToolUse hook BLOCKS the tool — so every Bash call
after the pull fails and the update cannot finish. Restarting does not recover it: apply never
ran, so `hooks.json` on disk still names the deleted path, and the next session is blocked from
its first call, with a hand-edited `hooks.json` the only way out. A script whose whole body is
`sys.exit(0)` costs nothing and turns that into a no-op: the stale hook allows, the update
completes, apply rewrites `hooks.json` without the entry, the next start is clean. Delete the
shim in a later release, not the one that retires the feature. `scripts/hook_push_guard.py`
(retired 2026-09-16) is the worked example.

## Adding a hook
A hook is a feature with a `hooks` payload — everything above applies, plus:
1. Create the script as `scripts/hook_<name>.py`. Get every path from `paths.py`; never build one.
2. Add a feature entry in `features_def.json` with a `hooks` payload — no marker needed; the
   `{S}/hook_<name>.py` path in the command IS the ownership signal. The command starts with
   `{PY}` — the interpreter, rendered as this machine's absolute path (never `python`,
   never `$(command -v ...)`: hooks run in a shell whose PATH nothing here controls).
3. Give it a `default` (is it recommended?), a `group`, and `requires` if it reads another
   feature's output. If it has tunables, declare them under `params` and reference them in the
   command as `{P:<name>}` — the user overrides them in `prefs.local.json`.
   If the hook only matters to processes started after the apply — a SessionStart hook aimed at
   `claude -p` runs, say — set `"hooks_restart": false`: the apply then asks for no restart when
   only such hooks change, and the feature leaves the core install pack.
4. Run `/claudette update`. The new feature appears in the numbered list with its description
   and recommendation; nobody gets it silently. In the plugin layout an accepted entry lands in
   the generated `hooks/hooks.json`, in a flat layout in `settings.json` — same registry.
5. If audio is needed, read the device with `prefs.param("audio_device")`.

**Never answer a worry the reader does not have.** A "never X" in a decision field puts X
in the reader's head and invites them to wonder whether it was on the table — and it is
usually there because the AUTHOR was reasoning about an internal invariant, not because a
user asked. The chart feature shipped with "It never creates a chart, never touches a repo,
and never runs on its own": one clause was defending the automatic path's own safety rule,
one was answering nobody, and the first was **false to a first-time reader**, whose chart
does get created — by the verb, on their say-so (owner, 2026-09-21). Say what the feature
does and what it costs; leave the invariants to the design doc, which is written for whoever
changes the code.

**Write the description for someone deciding.** It is the only thing between the feature and a
yes/no, so say what it does, when it helps, and what it costs — not how it is implemented.

**Scriptless hooks**: a hook whose whole body is one portable shell command (no branching, no state, no JSON parsing) may skip step 1 and inline the command, appending `# claudette:<feature>` to fingerprint it — that tag is the ownership signal in place of a script path. Saves a Python spawn per event. The boundary is one line: anything with logic goes in a `hook_*.py`, where it is reviewable, diffable and testable. Live example: `hooks.Stop` → `# claudette:turn-timestamp`, which prints a `systemMessage` turn-end stamp (terminal only — hook `systemMessage`s do not reach the remote app chat).

Multiple entries per event are fine and all run; but identity is a **set, scoped per event**, so two entries under the same event may not have the same fingerprint — same-identity duplicates are collapsed as singletons. Two Stop hooks both calling `hook_play_wav.py` collide; script + tag do not (the three `audible_speak_*` Notification entries share a script and are told apart by their `# claudette:<feature>` tags).
