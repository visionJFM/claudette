# The update / install flow

Loaded by the `update` verb of the `/claudette` skill ONLY, and only when the update plan
demands a conversation (first contact, `needs_decision` or `copy_updates` non-empty) — `status`, `doctor`,
`push`, `throttle` and a routine no-decision update must not pay for this prose. It covers both
first contact (a machine that has never run claudette) and the update pass with new features;
they are the same flow, differing only in how much is unanswered.

Shorthands: `SC` = `$HOME/.claude/skills/claudette/scripts`, `RUN` = `bash "SC/run.sh"`.
Expand them inline, literally — never a variable form; SKILL.md's verbatim rule applies.

## First contact — say what this is before doing anything

If the plan said `first_contact: true`, open with two or three sentences of your own words
covering: claudette offers a set of Claude Code features, the same on every install, and each
machine picks which ones it runs; **nothing is switched on until the user answers for it**; everything it writes is
captured first and reversible (a full revert exists that keeps the answers). Then run the flow
below. Never open first contact with a raw diff or "Already up to date."

## The flow

**1 — pull: already done.** The caller reached this file through `RUN pull.sh` — pull,
apply and plan have all run. Never re-pull here; on local-change conflicts (visible in the pull
output), offer stash/pop before anything else.

**2 — read the machine as data:** `RUN features_apply.py --features-json --only=needs-decision`
— just the features to present (unanswered, substrate present, not aligned; deferred included).
Fields that drive everything below: `state`, `install_stage` (`core`/`extra`/`individual`),
`deferred`, `recommended`, and the four decision fields. The full registry (`--only=all`) is
only for questions about features already answered.

**3 — report, don't ask, for what needs no decision:**
- `unavailable` features (listed in the plan): never mention them as choices — their substrate
  is absent, a yes could do nothing (the script refuses `=on` for them anyway).
- aligned features: the apply that already ran answered them on silently — its output's
  `--- Aligned ---` section names them. One line: "already in place, keeping: X, Y".
- `deferred` features: offer them again, but do not re-explain unless asked.

**4 — present the rest in packs, smallest decisions first.** Order and label them
"pack N of M" so progress is visible:
1. **Core** (`install_stage: core`) — hooks plus what they require; the one restart covers all
   of them, which is why they travel together. An install that stops after this pack leaves a
   coherent machine (I7).
2. **Extras** (`install_stage: extra`) — live pickup, no restart; a pack that never happens
   costs only goodies. **Audio staging**: present the six audio features as ONE decision, and
   offer to preview before answering — tones AND the voice play without any hook installed:
   `RUN features_apply.py --preview-tone audible_turn_end`
   `RUN features_apply.py --preview-speak`
   (adjust and re-listen: `--param audible_turn_end_volume=30 --preview-tone audible_turn_end`,
   `--param audible_speak_rate=200 --preview-speak`).
   Approve the set, then apply once — the whole stage costs one restart, not several.
3. **Security posture** (`install_stage: individual`) — one feature per question, never
   bundled into a pack and never mixed into a numbered wall: a pack answer must not double as
   consent to loosen the machine. "Later" is a legal answer; suggest `=defer` so the doctor
   stops counting it as an oversight. Ask in the order the rows arrive: the JSON already puts
   `arms_enforcement` features (auto permission mode) last, so the grants they may need exist
   before enforcement arms — arming first can deny this flow's own next script call.
   If a claudette script call IS denied right after an enforcement-arming answer: do not retry
   and do not work around it — tell the user to restart (`quit`, then `claude --continue`) and
   rerun `/claudette update`; every answer is written as it is given, so the flow resumes where
   it stopped.

Each pack: numbered list with id, `name`, all four decision fields, recommendation. When a row
carries `recommend_note`, THAT prose is the recommendation — relay it in place of the bare
on/off: it belongs to a feature whose worth turns on something only the user knows (several
machines? an interest no registry can see), so state the condition and let them answer it.
Never resolve the condition yourself from a proxy — a synced root repo is not a second
machine. Confirm,
then write THAT pack with `--feat ... -y` before presenting the next, so a stop keeps progress.

**4b — a newer version of a user copy** (plan `copy_updates`, one entry each). A user copy is
a file a feature created once and the user owns from then on — the claudette turn style's
`~/.claude/claudette-turn-style.md`. No apply overwrites it; this step is the only way a new version
reaches it. `UC` = `RUN user_copy.py`. By the entry's `state`:
- `untouched` — show its `changes` lines and ask. Yes → `UC install <feature>`; no →
  `UC decline <feature>`, so only a later version asks again.
- `custom` — the user edited it. Get the base it started from: `UC base <feature> --out <file in
  your scratchpad>`; the new text is the entry's `source`. Compare base→copy (what they changed)
  with base→new (what the version changes) and explain each change of the version: what it
  improves, where it touches or overrides one of their edits, the risk either way. Propose the
  merged text, their edits kept unless they choose otherwise, and show its full diff against
  their copy before writing — a summary hides a dropped clause. Approved → write the merge to a
  file and `UC write <feature> --from <file>`; declined → `UC decline <feature>`. `base` exits
  3 when the base text is gone (no base version in the header, or not in this clone's history): compare their
  copy with the new version directly and say the base was unavailable.

**5 — restart only if the apply said so.** The message names the exact moves: `quit`, then
`claude --continue` — the conversation survives. Never tell the user to restart when nothing
needs it.

**6 — redundancy review** (judgment, never automated): compare user-owned CLAUDE.md lines
against the generated leaf (`~/.claude/claudette-instructions.generated.md`), list user lines a
claudette rule now covers (quote both), ask which to delete. Never edit the leaf itself.

**7 — health check:** `RUN features_apply.py --doctor`. Report each `warn` with its `fix:`
command; never run a fix unasked. Drift rows ("approved but not in effect") are the user's
changes until they say otherwise.

A `CLAUDE.md imports` row is the one warn to raise as an **offer**, not a filed finding — its
remedy is a decision about a file claudette does not own, and the machine cannot make it. Word it
yourself, obeying whatever else the context asks of a proposal to the user (numbering, house
concision, language) — what is fixed is the content, not the sentence:
- the fact: which import, and that its literal text is loading into every session in place of a file;
- both exits, as exits: create the file, or delete the `@` line;
- that the file earns its keep even with nothing in it — the machine-local overrides you have not
  written yet — and that a column-0 `<!-- -->` header saying so costs no context;
- that one word from them is enough, and that no claudette script will write it on its own.

Never sell it as *suppressing the warning*. A blank file created to silence a check is, six months
later, indistinguishable from a machine that was never set up — the overrides are the point, the
quiet is not. If they decline, drop it; the row will still be there next time, which is the whole
of the follow-up.

**8 — epilogue.** The apply already prints the state summary (on/off/unanswered, backups, undo,
uninstall). Add only what the machine still lacks a decision on, in one line, and stop — no
sales pitch for what was declined. On first contact also relay the apply's "Python for hooks
and status line: <path>" line, and any "also usable:" alternatives it named — that is the
interpreter every hook will run, chosen without asking when only one qualified. A "never mention X again" request is `--feat X=off` (or
`=defer` if they may want it later).

## Cheap undo is the safety story — tell it once

If the user hesitates at any pack: every settings key claudette touches is captured first
(per-feature undo records), switching off restores what was there before, `/claudette disable`
stops one surface or everything without touching answers (`--suspend <zone>`), and
`/claudette uninstall` reverts the whole machine while keeping the answers (`--uninstall`). That is why acting on the best guess is safe — with one exception:
anything security-shaped is never guessed, which is exactly why those features are asked
individually and default to nothing.
