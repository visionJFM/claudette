# Security

Claudette changes how Claude Code behaves on your machine. This page says what it touches, so
you can judge it before installing. Everything here can be checked in `scripts/`.

## What it changes

- Nothing is applied before you answer. `/claudette update` describes each feature (what,
  benefit, cost, alternative) and applies only the ones you accept.
- Accepted features are written into `~/.claude/settings.json`, `hooks/hooks.json` and one
  generated instruction file imported by `~/.claude/CLAUDE.md`. Switching a feature off
  removes what it wrote. `/claudette uninstall` removes all of it; your answers are kept so a
  reinstall does not ask again.
- One file is yours to keep: the reply layout feature gives you your own copy of its rules,
  `~/.claude/claudette-turn-style.md`. Switching the feature off or uninstalling leaves it in
  place.
- A fresh clone runs no hook and changes no setting: `hooks/hooks.json` is not part of the
  repo, and only `/claudette update` writes it, after your yes. Over an existing install, the
  status line and the hooks running sessions already loaded call claudette's scripts by path,
  so a reinstall's scripts run at once, as an update's do.
- The install's own git clone gets a push address that refuses every push: an install only
  receives updates.
- An update of an install is screened before it lands, by the screen of the version already
  installed: the difference is checked for changes to the update path itself, to what a
  feature already switched on applies, and for code that newly reaches the network,
  credentials or other processes, runs code it builds, or deletes trees, and for files that
  are not text. A PNG, JPEG or GIF image passes when its first bytes match its extension;
  code that starts naming an image is flagged instead. Nothing flagged: the update lands. A
  finding: nothing changes until an agent has read the flagged changes, and you are asked
  when it is not sure. The screen catches the shapes it lists; it is not an
  audit.
- The usage chart's update (an optional feature) runs one background Claude Code session,
  removes it when it is done, and moves that session's transcript from `~/.claude/projects`
  to `logs/chart_runner_last.jsonl`, so that it does not list among your sessions in
  `/resume`. No other transcript is touched.
- Hooks are Python scripts of this repo that Claude Code runs on its own events (a prompt
  submitted, a tool about to run, a turn ending). Some of them add text to the conversation
  that the model reads: the reply layout, a pacing notice, the idle report. Some may deny or
  pause a tool call (usage pacing, the push guard). Each belongs to a feature you can turn off.
- The dot-commands open or close windows only when you ask.
  - `.new`, and an action such as `.sess !r` or `.proj !new` (an argument starting with `!`),
    open new Claude Code windows.
  - `.close` (or `/claudette:close`) ends the current session with its window.
- Features that touch permission settings carry no recommendation and are never accepted in
  bulk.

## Network

Claudette has no telemetry and no server of its own. It installs no package. The network is
reached in these cases:

| What | Where | When |
|---|---|---|
| Model-scoped usage limits | `api.anthropic.com` (the usage endpoint Claude Code's own `/usage` screen reads), with the access token Claude Code already stored on this machine. The token is read, never written, refreshed or sent elsewhere | The status line needs them; at most once a minute. Stop it with an empty file `logs/claude_limits_off` |
| Git | The remotes of your own repos (`fetch`, `pull`, `ls-remote`), with your own git credentials | `/claudette update`, the behind-your-remote check, the optional auto-fetch feature |
| Usage chart (optional feature) | A private page on your own `claude.ai` account, published by a Claude session on this machine. It carries this machine's usage percentages and its host name | Only when the chart feature is on |
| The chart page itself | Its own data files on `claude.ai`, and fonts from `fonts.googleapis.com` and `fonts.gstatic.com`, loaded by your browser | Only when you open the usage chart page |

Speech and tones are produced locally by the operating system's own voice and audio.

## What it reads

Everything below stays on this machine, except what the Network table lists.

- Claude Code's settings.
- Its account file, for the account's email shown on the status line.
- Its stored access token (see Network). On macOS the first read asks through a Keychain
  prompt, announced beforehand.
- The transcripts of your sessions, to count turns and read the prompt cache's age.
- Claude Code's prompt history (`~/.claude/history.jsonl`) and the session transcripts, for
  the dot-commands. This is what lets them list every folder you ran Claude in, find a past
  session, and open a new session in any of those folders.
- The list of running processes, for `.proc` and to find the window a session runs in: ids,
  names, start times and command lines, as Task Manager shows them. Never another process's
  environment, which holds its secrets.

## What it stores

None of this is tracked by git, or sent anywhere except what the Network table lists.

- `logs/` in this folder, which git ignores and `docs/CLAUDETTE_LOGS.md` describes file by file:
  - usage readings with percentages, token counts, session id and the account's email;
  - per-session status lines;
  - the decisions of the idle report;
  - a log of each update;
  - the transcript of the usage chart's last background session;
  - the command of a session it opens in a new Windows Terminal tab, when that command holds
    quotes, read once by that tab and deleted after a day.
- This machine's answers, in `prefs.local.json`.
- Build output, in `generated/`.
- A `.backup` beside each configuration file it rewrites.
- One line in a repo's `.git/info/exclude`, when you set a per-project override there.
- On a reinstall over an existing claudette folder, a backup in
  `~/claudette-backup-<date>-<time>`: the folder's old git history, its edits, and the files
  the reinstall replaced or took out. Delete it when the new install works.

The folder of usage readings grows without limit. Deleting it loses only history.

## Reporting a vulnerability

Use GitHub's private reporting on this repository: **Security** tab, **Report a
vulnerability**. Please do not open a public issue for something exploitable. There is no
bounty; reports are read by one person, on a best-effort basis.
