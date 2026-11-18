# Authoring skills — placement, naming, hygiene

Read this when creating **any** skill — claudette's, the owner's, or a project's — or when
deciding which skills a machine should load. Importing a vendor skill needs only the naming
bullet about upstream names.

## Adding a skill
**Ask where it goes before writing it** — two questions settle it:

| Who else ever needs it? | Bound to one project's files/paths? | Goes to |
|---|---|---|
| any user | — | `skills/claudette-<name>` |
| just you, on any of your machines | no | `skills/<owner>-<name>` |
| just you | **yes** | `<project>/.claude/skills/<name>` — **no owner prefix**, the directory already namespaces it (`Empire:plans-fetch`) |
| just you, this machine only | no | `skills/local-<name>` — untracked by construction (absent from the `.gitignore` allowlist) |

Recommend a row, don't just ask; the common answer is `<owner>-`. Then create
`skills/<owner>-<subject>-<action>/SKILL.md` following the pattern in `skills/throttle/` inside this repo.
Frontmatter `name:` MUST equal the directory name.

**Naming** — kebab-case, ≤4 segments, owner prefix mandatory. Subject is optional; action is a verb
phrase saying what it does. Examples below use the placeholder owner `vader`.
- `claudette-*` — generic, useful to any user, no personal content; ships in the claudette repo.
  Subject may be omitted: `claudette:throttle`.
- `<owner>-*` — everything else. Owner token = `prefs.local.json` → `owner` (short, lowercase,
  no spaces or dashes; initials work; `my` until the user sets one).
- **Prefix = who publishes it, not what it is about.** Your personal expenses skill for the Empire
  is `vader-empire-expenses` — yours, about them. `empire-expenses` is the same subject published
  *by* the Empire for every employee, and waits on an Empire-shared repo to live in. Org-published
  and employee-generic is the legitimate `empire-*` case: `empire-rules-check`.
- **Never encode machine scope** (home/work) in a name — that is sparse-checkout, see
  "Per-machine skill subset" below. Names are slash commands; renaming for a new laptop is not a thing.
- Default new skills to `<owner>-`; promote to `claudette-` deliberately (a rename changes the
  slash command, so decide early).
- Imported/vendor skills keep their upstream name. The prefix is
  also the tracking rule: `.gitignore` un-ignores only `skills/claudette*/` and `skills/<owner>-*/`,
  so a fresh import is untracked by construction and never joins a push by accident.
- `local-*` — machine-only tier, same axis as the other two, just the shortest reach. Not
  allowlisted, so it never syncs anywhere.

**Machine-local scratch inside a skill** goes in a `local/` folder at any depth
(`skills/vader-starmap-scan/local/probe/`); runtime state goes in `logs/`. Who ignores them depends on the
tier: `<owner>-*` skills are covered by `skills/<owner>-*/**/{logs,local}/` in the host repo's `.gitignore`;
claudette's own subskills are covered by the plugin repo's `.gitignore`. Never a bare `**/local/` glob — it would swallow a real `local/` source directory in an
imported skill.

**A per-skill `.gitignore` is conditional, and lists only that skill's own folders.** Two rules,
both violated by copying a neighbouring skill's file as boilerplate:
- No scratch dirs → **no `.gitignore`**. A pure-prompt skill (one `SKILL.md`, writes nothing into
  its own folder, e.g. `claudette:done`) gets none. Add one the day it starts writing.
- Name only what that skill actually has (`local/`, `logs/`, `temp/`, a `.venv/` it really creates).
  Never restate the never-trackable backstop (`.env`, `*.pem`, `id_rsa*`, OS droppings) per skill:
  it lives once at repo root with `**/` globs, last match wins, and covers every skill already —
  duplicating it there is dead config that reads as if the skill needed it.
- `local/` means **never synced; this machine's copy is the only copy**. It does NOT mean
  expendable — back up anything irreplaceable (a rate-limit ban record, annotated ground truth).
- Tokens/secrets belong here, never in a tracked-shaped file. `local-*` and `local/` are two
  independent barriers, and neither is a security boundary on its own: once a secret reaches git
  history the fix is rotation, not a revert.
- A folder that scripts or the user delete and recreate (`temp/`, `out/`) must NOT rely on anything
  stored inside it — put its rule in the nearest tracked `.gitignore`, anchored to the exact path.

## Per-machine skill subset
Some skills should not load on every machine (context budget, clutter, or they are simply none of
that machine's business). Handle it with **sparse-checkout** — per-clone, untracked, no rename, no
second repo. Excluding one personal family on a work machine:
```bash
git -C ~/.claude sparse-checkout set --no-cone '/*' '!/skills/vader-family-*/'
```
`/*` takes everything, `!` lines subtract (quote them — bash history-expands a bare `!`). Patterns
are cumulative: re-`set` the whole list to add another. Undo with `sparse-checkout disable`.
- New `vader-family-hunt` pushed from the other machine → stays absent here, the glob covers it.
- Excluded paths get `skip-worktree`, so `git add`/`push-diff.sh` never see them as deletions.
- Config lives in `.git/info/sparse-checkout` — **a fresh clone must re-run the command**.
- **Working tree only.** Blobs stay in `.git` and `git log -p` still shows them. This trims what a
  session loads; it is NOT confidentiality. If a machine must not *contain* something, use a
  separate repo instead.
