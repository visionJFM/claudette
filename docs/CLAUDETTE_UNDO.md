# The reversal ladder

Loaded by the `undo`, `disable` and `uninstall` verbs of the `/claudette` skill ONLY — the
other verbs must not pay for this prose. Three intents share this one machinery: `disable` is
"stop doing this going forward", `undo` is "put back what claudette changed", `uninstall` is
"I'm leaving".

Shorthands: `SC` = `$HOME/.claude/skills/claudette/scripts`, `RUN` = `bash "SC/run.sh"`.
The bare `--flags` below are arguments to `RUN features_apply.py`; run commands verbatim
(rule in SKILL.md).

Three rungs. `undo` takes the smallest that fits; `disable` presents them as a menu, widest
reversible first; `uninstall` is rung 3 plus the printed delete step. Say which rung is in use.

1. **One feature**: `--feat <id>=off` restores what its undo record captured (the value that
   was there before claudette wrote, kept in `logs/feature_undo.json`). This rewrites the
   user's answer and removes the feature's config — to the user it is "off", not "disabled".
2. **One zone**: `--suspend <zone>` (audio, claude_md, settings, hooks, machinery) kills the
   zone's effect without touching any answer; `--resume <zone>` brings it back. A zone is an
   output surface — say "everything claudette does to ⟨that surface⟩", never "feature group":
   settings and claude_md cut across features.
3. **Everything**: `--suspend all` reverts the whole machine, answers kept — `--resume all`
   reinstalls. `--uninstall` is the same act plus a confirmation and the delete-the-clone step
   it prints and never runs; it belongs to the `uninstall` verb.

## Vocabulary — the user hears verbs, prompts show flags

| user hears | shell flag | what it does |
|---|---|---|
| disable / disabled | `--suspend` | reversible, answers kept |
| re-enable | `--resume` | brings a suspended zone back |
| off | `--feat <id>=off` | answer changed, config removed |
| uninstall | `--uninstall` | disable-everything + printed delete step |

"Kill switch" never reaches the user. The flags are deliberately NOT renamed to match: at
permission-prompt time, `--suspend` vs `=off` is what tells the user which rung they are
approving, and `--suspend` says "temporary" on its face. When a permission prompt will show a
raw flag, the confirmation names both: "disabling audio = `--suspend audio` — reversible,
answers kept".

The `.backup` copies (`settings.json.backup`, `CLAUDE.md.backup` in `$HOME/.claude`) exist to
debug the LAST apply — diff current-vs-backup on request — but they are not the undo store;
the records are.
