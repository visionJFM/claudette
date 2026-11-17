# The push flow

Loaded by the `push` verb of the `/claudette` skill ONLY (and by `sync`, whose second half is a
full push) — the other verbs must not pay for this prose.

This flow pushes one repo: the user's personal `~/.claude` repo, which the status line calls
`my`, and only with the `my_root_repo_sync` feature on. The claudette clone is not pushed here:
an install only pulls it, so a local change in it is reported rather than pushed.

Shorthands: `SC` = `$HOME/.claude/skills/claudette/scripts`, `RUN` = `bash "SC/run.sh"`.
Run commands verbatim (rule in SKILL.md).

1. **Scope first.** With `my_root_repo_sync` off (check `--features-json`), say in one line
   that claudette pushes nothing on this machine, and stop. With it on, probe write access:
   ```
   git -C "$HOME/.claude" push --dry-run
   ```
   A rejection (read-only deploy key, 403, no permission) means this machine *consumes* that
   repo: say so in one line and do not suggest `push` again in this session. A `~/.claude`
   that is not a git repo is skipped the same way.
2. Status — if the repo is behind, pull before pushing:
   ```
   bash "$HOME/.claude/skills/claudette/scripts/run.sh" status.sh
   ```
3. Preview what a push would contain (staging dry-run + the large-blob / secret-shaped content
   guard):
   ```
   bash "$HOME/.claude/skills/claudette/scripts/run.sh" push-diff.sh "$HOME/.claude"
   ```
   It ends with `COVERAGE <repo>: ran …` then `RESULT <repo>: clean|flagged|unknown`.
   Read the verdict, and check the repo it names is `~/.claude`. `unknown` (no upstream, or a
   scan cap) is **not** an all-clear — report it as "could not determine", never as clean. If
   COVERAGE says a class was skipped or unavailable, say so alongside the verdict: `clean` then
   means clean *of what ran*.
   A class that is noise in one repo every single time can be removed there, and only there, in
   that repo's `.claudette.json` (committed, every clone) or `.claudette.local.json` (this
   machine): `{"params": {"leak_scan_classes": {"remove": ["host"]}}, "notes":
   {"leak_scan_classes": "<why>"}}` — classes `secret`, `host`, `user`; the shared file cannot
   remove `secret`. Propose it only where the class is *expected* content — machine names in notes
   about your own machines. Never for a credential hit. `CONFIG` lines are overrides that were
   refused: relay them.
   A `SECRET?` hit means **rotate the credential** — git history keeps it even if the commit is
   reverted. A `HOST?` / `USER?` hit is this machine's hostname / OS username inside a blob: redact
   it before the commit leaves, and if it already left, bring that to the user rather than quietly
   amending. Never work around the guard.
   This is the preview *before* you commit; step 4 pushes what you commit here, and the
   `claude_md_push_leak_scan` rule applies to that push like any other — with `<repo_root>` passed,
   and with your own read of what is leaving, which the script's output does not contain.
4. Show which files will be staged, ask for confirmation, then commit and push with a short
   message (no co-authors). Run the push through the run logger, so what left this machine is
   recorded like the pull and the apply are (`logs/config_runs/` — see `CLAUDETTE_LOGS.md`):
   ```
   bash "$HOME/.claude/skills/claudette/scripts/run.sh" log_run.sh push -- \
     git -C "$HOME/.claude" push
   ```
   One `log_run.sh` call per command; each appends a `== push ==` section. Its exit status is the
   command's, so a rejected push still reports as failed.
