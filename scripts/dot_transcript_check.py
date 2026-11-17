#!/usr/bin/env python3
"""Regression check for dot_common.classify() against real transcripts.

WHY THIS EXISTS. `classify()` is a whitelist over a file format nobody here controls,
and Claude Code changes it without announcement; a dropped entry shows no symptom except
a listing that looks complete.

So the alarm is the "unknown" bucket: a `user` entry that is plain text, not a tool
result, not a harness injection, and that no rule recognises. A nonzero count means the
format moved and something a person typed is being dropped - read the samples, then
teach `classify()`. EXCEPT transcripts from clients older than promptSource itself
(first seen in 2.1.161): their typed prompts carry no promptSource at all and always
read as unknown. That is a known gap, not a regression; an unknown from a client
>= 2.1.161 is one.

    python dot_transcript_check.py                 # every transcript, table + verdict
    python dot_transcript_check.py --limit 40      # the 40 most recent only
    python dot_transcript_check.py --path <file>   # one transcript

Exit status is the point: 0 clean, 1 unknown entries found. Run it after a Claude Code
upgrade, or when a listing looks like it is missing something.

SECOND ALARM: every `.note` in logs/notes_typed.jsonl must be in its transcript.
`.note` stores by having the client record the blocked prompt, which is the same kind
of unannounced format fact; a missing note means the client stopped. Except a note
typed mid-turn: the client records none of those since 2.1.295, so they are counted
apart and only the others raise the alarm. The same check, on the newest notes only,
is a `/claudette doctor` row.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dot_common as dc

SAMPLES = 8


def transcripts(limit, path):
    if path:
        return [path]
    root = os.path.expanduser("~/.claude/projects")
    found = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    found.sort(key=lambda p: os.path.getmtime(p))
    return found[-limit:] if limit else found


def shape(d, txt):
    """The fields classify() actually discriminates on - so a new row in this table is
    a new entry shape, which is exactly what needs a human to look."""
    c = (d.get("message") or {}).get("content")
    blocks = (",".join(sorted({b.get("type") for b in c if isinstance(b, dict)}))
              if isinstance(c, list) else "str")
    return (d.get("type"), d.get("subtype"), d.get("promptSource"), bool(d.get("isMeta")),
            blocks, "toolUseResult" in d,
            bool(dc.CMD_AT_START.match(txt)), bool(dc.BASH_ECHO.match(txt)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--path")
    ap.add_argument("--quiet", action="store_true", help="verdict only, no table")
    a = ap.parse_args()

    files = transcripts(a.limit, a.path)
    kinds = collections.Counter()
    shapes = collections.Counter()
    unknown = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(d, dict) or d.get("type") == "assistant":
                        continue
                    kind, txt = dc.classify(d)
                    kinds[kind] += 1
                    shapes[shape(d, txt)] += 1
                    if kind == "unknown" and len(unknown) < SAMPLES:
                        unknown.append((os.path.basename(p)[:8],
                                        re.sub(r"\s+", " ", txt)[:110]))
        except Exception as e:
            print("skip %s: %s" % (p, e))

    print("%d transcript(s)" % len(files))
    if not a.quiet:
        print("\nentry shapes (a NEW row is a format change worth reading):")
        print("  %-9s %-16s %-22s %-6s %-12s %-5s %-5s %s"
              % ("type", "subtype", "promptSource", "isMeta", "blocks", "tur", "cmd", "bash"))
        for k, v in shapes.most_common():
            print("  %6d  %-9s %-16s %-22r %-6s %-12s %-5s %-5s %s"
                  % ((v,) + tuple(str(x) if not isinstance(x, str) else x
                                  for x in (k[0], k[1]))
                     + (k[2],) + tuple(str(x) for x in k[3:])))
    print("\nclassified: " + "  ".join("%s=%d" % (k, kinds[k])
                                       for k in sorted(kinds)))
    n = kinds.get("unknown", 0)
    rc = 0
    if n:
        print("\n!! %d UNKNOWN entr(ies) - human-shaped and unrecognised." % n)
        print("   Something a person typed is being dropped. Samples:")
        for f, t in unknown:
            print("     %s :: %s" % (f, t))
        rc = 1
    else:
        print("\nOk, 0 unknown - every entry is accounted for.")

    import dot_note
    checked, lost, unchecked = dot_note.verify(last=None)
    mid = [r for r in lost if r.get("mid_turn")]
    lost = [r for r in lost if not r.get("mid_turn")]
    print("\nnotes: %d checked against their transcripts, %d missing, %d typed mid-turn "
          "(the client records none since 2.1.295: in the log only), %d unchecked "
          "(transcript gone)" % (checked, len(lost), len(mid), unchecked))
    if lost:
        print("!! notes not found in their transcripts - missing, or recorded in a shape "
              "dot_note.notes_in() does not read yet. Samples:")
        for r in lost[:SAMPLES]:
            print("     %s %s :: %s" % (str(r.get("ts"))[:19], str(r.get("session_id"))[:8],
                                        dot_note.norm(r.get("text"))[:90]))
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
