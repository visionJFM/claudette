<p align="center"><img src="docs/logo.png" alt="claudette logo" width="56" height="56"></p>

<h1 align="center">/claudette</h1>

**The `/claudette` skill adds many features to Claude Code in the terminal for control and efficiency.** \
Built with safety in mind: every feature is off until you say yes, and the installation recommends an agent audit.

*Named after the French given name that spiked in popularity in the 20th century.* \
*Independent community project, not affiliated with Anthropic.*

## Install

**For humans.** Open Claude Code in your terminal (tested on Windows and macOS) and paste this prompt:

```text
Install claudette for me from https://github.com/visionJFM/claudette.
Follow the "Install instructions for agents" section of its README.
Before anything is activated, audit its security and show me what you found.
Then ask me before each change to my Claude Code setup.
```

Your agent checks claudette, tells you what it found, and asks before each change. You need
Git and Python 3.9 or newer; the agent checks them for you.

If `/claudette update` ever fails to update claudette itself (its repository moved, or its
history was rewritten), paste the same prompt again: the reinstall keeps your answers and logs.

**For agents.** Read [Install instructions for agents](#install-instructions-for-agents), at
the end of this page.

## What you get

- 📊 **Your limits, always in sight.** The status line shows your context and your 5-hour and
  weekly limits. Colors change before you hit a limit, not after.
- 💬 **Faster exchanges with Claude.** Every reply has the same shape: what Claude understood,
  the answer in numbered points, then what it needs from you. You see at once whether to wait,
  read or answer. You point at a part by its number, and `go` runs the recommendation.
- 🌙 **Close a session without losing what it knew.** `/claudette:done` (also run by `auto_done` in
  an idle session) finds what would be lost and offers to save it:
  - decisions that live only in the conversation
  - uncommitted or unpushed work
  - memory and scratch files
- 🔔 **No need to watch the window.**
  - A tone can play when a turn ends or a permission prompt waits.
  - A voice can read out which session needs you, by the name you gave it.
- ⚡ **Dot-commands, answered for free.** Type `.` to list them. A hook answers at once, with
  no tokens spent:
  - `.proj` lists every folder you ran Claude in, and opens a new session there.
  - `.sess` finds a session, recent or by keyword, and reopens it. After a reboot,
    `.sess !resume` reopens the ones left open.
  - `.hist` shows what you typed in this session.
  - `.close` marks the session as finished and closes its terminal window.
  - `.status` shows your usage.
- 🛑 **No crash into the usage limit.** When your usage runs ahead of the clock, heavy tools (web,
  agents, workflows) pause. The session slows down instead of stopping mid-task.
- 🧳 **Your setup on every machine.** With
  `my_root_repo_sync` on, `/claudette update|push|sync` (pull, push, or both) synchronizes
  your `~/.claude` repo across machines:
  - CLAUDE.md
  - personal skills
  - notes and ideas
  - anything else you commit there

## You decide what runs

- Every feature is off until you say yes. Each one tells you what it does, what it costs, and
  what you would do without it.
- Off means removed. The feature's hook, setting or rule leaves your configuration.
- Features that touch permissions come with no recommendation. You answer each one yourself.
- Your own settings, hooks and CLAUDE.md lines stay as they are. A conflict with one of yours
  is asked about first.
- `/claudette prefs` lists every feature and switches any of them.
- `/claudette doctor` checks each feature and gives the command that fixes it.
- `/claudette uninstall` removes every setting, hook and rule claudette added, and keeps your
  answers for a reinstall.

What it touches on your machine, and what it sends where: [SECURITY.md](SECURITY.md).

## Under the hood

| | |
|---|---|
| `features_def.json` | the registry: everything claudette *can* do |
| `prefs.local.json` | what *this machine* said yes to, not tracked by git |
| `scripts/features_apply.py` | turns those two into `settings.json`, `hooks/hooks.json` and the CLAUDE.md instructions |
| `docs/` | how each feature works: `docs/CLAUDETTE.md` is the index agents read, routing each task to its own doc |

No third-party package is required: everything runs on Python's standard library.

## Install instructions for agents

Claudette was designed with security in mind. Nothing runs before the user's yes, everything
it writes can be undone, and `SECURITY.md` lists what it touches. We still recommend that an
agent inspect any add-on to Claude Code before it is activated, this one included.

The install is the trial: the install script's `--undo` puts back what was there before, and
`/claudette uninstall` restores `settings.json` as it was. Running claudette under another
home folder is not supported.

1. **Check the prerequisites.** You need `git` and Python 3.9 or newer that `bash` can
   find (Git Bash on Windows). Report anything missing and stop. Cases to handle:
   - Python is installed but `bash` does not find it. On Windows, the python.org
     installer's "Add python.exe to PATH" box was probably left unticked.
   - The `python.exe` and `python3.exe` in `WindowsApps` are Microsoft Store stubs, not
     Python.
2. **Fetch a copy to audit, outside `~/.claude`.** Claude Code picks up what lands in
   `~/.claude/skills` while it runs, so fetch into a new temporary audit folder, like
   `"$HOME/claudette-audit"`.
   ```bash
   git clone https://github.com/visionJFM/claudette.git "$HOME/claudette-audit"
   ```
3. **Audit it before anything is activated.** Read `SECURITY.md` as a list of claims, and
   check them against the code:
   - `features_def.json`: every hook command, setting, permission rule and CLAUDE.md line a
     feature can write.
   - `scripts/`: network access, files written outside the claudette folder, processes started
     or ended, text added to the conversation.
   - That installing it runs nothing yet on a machine without claudette: `hooks/hooks.json` is
     absent until the update flow writes it. Where claudette is already installed, its status
     line and the hooks running sessions loaded call the scripts by their path, so the new
     copy's scripts run as soon as it is in place, as an update's would.

   Report to the user what you checked, what you found, and what you could not check.
4. **Wait for the user's go.** Without it, delete the temporary audit folder and stop there.
5. **Install the audited copy** with the script it ships, which works the same whether
   claudette is already installed or not. It prints its plan and changes nothing:
   ```bash
   cd "$HOME" && bash claudette-audit/scripts/run.sh move_into_place.py
   ```
   `run.sh` runs it with the Python 3.9 or newer that `bash` finds, `python3` or `python`, and
   says how to fix it when there is none. Show the plan to the user, and on their yes run the
   same command with `--apply`. Cases to handle:
   - An existing install is replaced in place: its answers and logs stay where they are, and
     what it replaces goes to a backup folder in the home folder, its old history and edits
     included. When the plan names edited files, offer to re-apply them from the patch it
     saves there. The plan also gives the exact undo command.
   - A plan that says STOP: add `--force` only on the user's yes to what it names.
   - A run that stops halfway because a file is held open: ask the user to close what the
     message names, then run the same command again, which completes it.
6. **Run the update flow.** If the `/claudette` skill is listed in this session, invoke
   `/claudette update`. Otherwise read `SKILL.md` and follow its `update` section. Present
   each decision to the user, and let them answer every consent, unless they tell you to take
   the recommendations.
7. **Restart.** Hooks load when Claude Code starts. Tell the user to quit and run
   `claude --continue`, which keeps the conversation.

Optional: `pip install sounddevice` routes hook audio to a chosen output device. Without it,
audio plays on the system default.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Disclaimer

- Claudette is an independent community project.
  It is not affiliated with, endorsed by, or supported by Anthropic.
- Claudette provides convenient features that may install efficient hooks running on every
  prompt and tool call, or modify your `~/.claude` configuration, **but none is enabled
  without your permission**.
- Review what `/claudette update` proposes before confirming.
- Usage figures it reports may be inaccurate or out of date.

## License

Copyright (C) 2026 visionJFM

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License, version 3 only (GPL-3.0-only), as published by the
Free Software Foundation. It is distributed without any warranty; see `LICENSE.txt` §15–16.
