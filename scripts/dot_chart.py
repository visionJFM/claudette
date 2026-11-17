#!/usr/bin/env python3
""""`.chart`" — where the usage chart is and whether this machine's readings are in it.

The link is the answer most of the time ("open my chart"), and a link is exactly the
kind of question that must not cost a turn. Refreshing the chart cannot happen here — a
hook has no tools, and only an agent turn can publish to an artifact — so this
command reports and points at `/claudette:chart`, the same way `.recap` points at
its own skill rather than pretending to run it.

It also answers the question the link alone raises — "is what I am looking at current?" —
so it reports the pushing door, and whether that door is even open: with
`chart_autoupdate` off, nothing pushes by itself and a chart that looks stale is
working as configured. Saying so beats letting the user wonder.

O(1) by contract: `usage_chart.py state` reads three small files and stats one more. A
dot-command answers inside the hook, on the user's keystroke, so it may never walk the
snapshot folder (tens of thousands of files).

Named `.chart` rather than `.usage`: `.status`/`.s` already answers "how much have I
used" from the live payload. `.chart` is also the namespace root for a second chart
(`CLAUDETTE_DESIGN.md`, "The word `.chart`, and the key that names claudette").
"""
import time

import dot_common as dc


def _state():
    # imported here, not at module load: the dispatcher imports this module only after
    # a match, and a prompt that is not ".chart" must not pay for either import
    try:
        import usage_chart
        return usage_chart.state()
    except Exception:
        return {}


def _autopush():
    """Is anything pushing this machine's readings by itself? None means "cannot tell",
    which is reported as itself rather than guessed either way."""
    try:
        import prefs
        return bool(prefs.effective("chart_autoupdate"))
    except Exception:
        return None


def _dur(seconds):
    """A span's length. Separate from _ago() because "9h of readings" and "9h ago" are
    different sentences and one formatter produced both, badly."""
    d = max(0, int(seconds or 0))
    if d < 5400:
        return "%dm" % round(d / 60.0)
    if d < 172800:
        return "%dh" % round(d / 3600.0)
    return "%dd" % round(d / 86400.0)


def _ago(seconds):
    if seconds is None:
        return "never"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return "%dm ago" % round(seconds / 60.0)
    if seconds < 172800:
        return "%dh ago" % round(seconds / 3600.0)
    return "%dd ago" % round(seconds / 86400.0)


def handle(data, tail, to_context):
    valid = not tail.strip()
    st = _state() if valid else {}
    now = st.get("now") or int(time.time())
    lines, hints = [], []

    if not valid:
        lines = ["%s.chart%s %s— usage chart: the link, and whether this machine's "
                 "readings are in it (no arguments)%s" % (dc.ACT, dc.R, dc.TIME, dc.R)]
    elif not st:
        lines = ["%s.chart%s %s— cannot read the chart state; try "
                 "/claudette:chart%s" % (dc.ACT, dc.R, dc.TIME, dc.R)]
    elif not st.get("url"):
        lines = ["%s.chart%s %s— no chart on this machine yet%s"
                 % (dc.ACT, dc.R, dc.TIME, dc.R)]
        hints = [("/claudette:chart", "find your chart (or create it) and cache "
                                            "the address here")]
    else:
        pushed = st.get("last_push_ts") or 0
        lines = ["%s.chart%s %s— %s%s" % (dc.ACT, dc.R, dc.TIME, st["url"], dc.R)]
        if not pushed:
            lines.append("   %sthis machine has never contributed to it%s"
                         % (dc.TIME, dc.R))
        else:
            # The chart is behind by whatever this machine has rendered since its last
            # push. The render timestamp is the honest proxy: one file, no scan.
            behind = st.get("stale_s")
            lines.append("   %slast update %s · %d readings in %d window(s)%s"
                         % (dc.TIME, _ago(now - pushed), st.get("last_push_rows") or 0,
                            st.get("last_push_windows") or 0, dc.R))
            if behind and behind > 900:
                lines.append("   %s%s of readings since that update are not in it yet%s"
                             % (dc.TIME, _dur(behind), dc.R))
        # Order: state, then how to change that state, then the one-off action, so a
        # status line and the line that changes it read together.
        auto = _autopush()
        if auto:
            lines.append("   %schart_autoupdate is ON%s %s— every /claudette:done "
                         "keeps it current%s" % (dc.OK, dc.R, dc.TIME, dc.R))
            hints = [("/claudette:chart", "update now, chart only")]
        elif auto is None:
            hints = [("/claudette prefs", "cannot read chart_autoupdate from here"),
                     ("/claudette:chart", "update the chart from this machine")]
        else:
            lines.append("   %schart_autoupdate is OFF%s %s— /claudette:done leaves "
                         "the chart alone%s" % (dc.RED, dc.R, dc.TIME, dc.R))
            hints = [("/claudette prefs", "turn on chart_autoupdate to update from every "
                                          "/claudette:done"),
                     ("/claudette:chart", "update the chart from this machine now")]

    lines += ["   %s%s%s %s— %s%s" % (dc.ACT, v, dc.R, dc.TIME, d, dc.R)
              for v, d in hints]
    if to_context:
        dc.emit_context_block(lines, ".chart", noisy=not valid)
    else:
        dc.emit_block(lines, noisy=not valid)
