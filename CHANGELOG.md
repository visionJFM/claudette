# Changelog

Each published version of claudette, newest first. The numbers follow
[Semantic Versioning](https://semver.org):
- the first number changes when an update needs more than `/claudette update`, such as a
  reinstall or a feature removed;
- the second when a feature, a verb or a dot-command is added;
- the third for fixes and wording.

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
