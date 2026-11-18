# Changelog

Each published version of claudette, newest first. The numbers follow
[Semantic Versioning](https://semver.org):
- the first number changes when an update needs more than `/claudette update`, such as a
  reinstall or a feature removed;
- the second when a feature, a verb or a dot-command is added;
- the third for fixes and wording.

## [1.2.2] - 2026-11-18

- The README shows each feature under its own heading, with a screenshot.
- Skills you write are named with the owner prefix `my` until you set your own (it was the
  placeholder `username`).

## [1.2.1] - 2026-11-17

- The README shows a redrawn logo, at twice the size.

## [1.2.0] - 2026-11-17

- A reinstall replaces the files in place instead of moving the folder, so a program working
  in the folder (a session's hook, a terminal, an editor) no longer blocks it. What it
  replaces, the old history and edits included, goes to the backup folder, and its plan gives
  the exact undo command. A run stopped by a file held open completes when run again.
- The idle timer leaves the session's folder while it waits, so it no longer keeps a project
  folder from being renamed or deleted for up to an hour.

## [1.1.0] - 2026-11-17

- Updates of an install are screened before they land: nothing flagged, they land as before;
  a finding holds the update, unchanged on disk, until an agent has read the flagged changes,
  and asks you only when something is not clear. A development clone keeps its plain update.
- The install plan, the README and `SECURITY.md` say that a reinstall over an existing install
  runs the new scripts at once, through the status line and the hooks already loaded.

## [1.0.1] - 2026-11-17

- An install can no longer push: its clone's push address refuses every push, since an
  install only receives updates.
- The install instructions run the install script through `scripts/run.sh`, which finds
  Python whether it is called `python3` or `python`, and say that the install itself is the
  reversible trial.
- `SECURITY.md` lists the usage chart's move of its background session's transcript.

## [1.0.0] - 2026-11-17

First public release.
