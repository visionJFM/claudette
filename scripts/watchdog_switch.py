#!/usr/bin/env python3
"""The auto-done watchdog on/off switch — one static, grantable command per
operation (&&/||/redirect one-liners fail closed in the permission layer).

The switches are plain marker files: presence means disabled, contents are only
a timestamp. Global: logs/watchdog_off. Per session: logs/watchdog/<sid>.off.

  watchdog_switch.py --state [--session SID]
  watchdog_switch.py --off|--on --global
  watchdog_switch.py --off|--on --session SID
"""
import argparse
import os
import sys
import time

import paths

GLOBAL_OFF = paths.log("watchdog_off")


def session_off(sid):
    return paths.log("watchdog", sid + ".off")


def state(sid):
    if os.path.isfile(GLOBAL_OFF):
        return "disabled globally"
    if sid and os.path.isfile(session_off(sid)):
        return "disabled for this session"
    return "enabled"


def set_switch(off, glob, sid):
    """Flip one switch, return the confirmation line. Shared with the ".autodone"
    dot-command, which cannot let a print escape before its own JSON output."""
    marker = GLOBAL_OFF if glob else session_off(sid)
    label = "global" if glob else "session " + sid
    if off:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())) + "\n")
        return "watchdog disabled (%s)" % label
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    return "watchdog enabled (%s)" % label


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--state", action="store_true")
    g.add_argument("--off", action="store_true")
    g.add_argument("--on", action="store_true")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="glob", action="store_true")
    scope.add_argument("--session", metavar="SID")
    a = ap.parse_args(argv)

    if a.state:
        print(state(a.session))
        return 0

    if not a.glob and not a.session:
        ap.error("--off/--on need a scope: --global or --session SID")
    print(set_switch(a.off, a.glob, a.session))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
