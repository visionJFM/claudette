# Changelog

Each published version of claudette, newest first. The numbers follow
[Semantic Versioning](https://semver.org):
- the first number changes when an update needs more than `/claudette update`, such as a
  reinstall or a feature removed;
- the second when a feature, a verb or a dot-command is added;
- the third for fixes and wording.

## [1.0.1] - 2026-11-17

- An install can no longer push: its clone's push address refuses every push, since an
  install only receives updates.
- The install instructions run the install script through `scripts/run.sh`, which finds
  Python whether it is called `python3` or `python`, and say that the install itself is the
  reversible trial.
- `SECURITY.md` lists the usage chart's move of its background session's transcript.

## [1.0.0] - 2026-11-17

First public release.
