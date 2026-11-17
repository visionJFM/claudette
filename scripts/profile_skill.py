#!/usr/bin/env python3
"""What does one skill invocation actually cost? Retroactive, from logs already on disk.

Sibling of profile_turn.py: that one splits a turn's WALL CLOCK, this one splits
its SPEND.

Two sources, both already written by the harness, both read-only here:
  ~/.claude/projects/<key>/<session>.jsonl   per-assistant-message `usage`, with
      the cache_creation 5m/1h split and the `model` -> exact token weights.
  claudette/logs/claude_usage_snapshots/status_<ms>_<pid>.json   `rate_limits.five_hour.used_percentage`
      at every status-line render -> brackets a run to a measured delta.

WHY BOTH. Tokens are the thing you can optimize; percent-of-5h-window is the thing
the user actually pays. Neither alone is enough:
  - tokens are exact but the subscription weighting formula is not published, so
    they cannot be converted to window percent;
  - window percent is what matters but has 1-POINT RESOLUTION on a ROLLING window
    that decays while you work, so a single run is mostly noise. It only becomes a
    number with a controlled A/B in an otherwise-idle window (see --help notes).
So this prints both and lets them disagree. The dollar column is API LIST PRICE,
a cross-run comparison unit only - it is NOT what a subscription seat is charged.

WHAT IT MAKES VISIBLE: cost is not dominated by the skill's own output. Weighted
into input-equivalents, cache write (1.25x at 5m TTL, 2.0x at 1h) and cache read
(0.1x, but multiplied by every round trip) both usually beat output. So the levers
are ROUND-TRIP COUNT and CACHE TEMPERATURE, not prose length. What output there is
is mostly extended thinking, not the report.

A round trip here is ONE API RESPONSE, deduped on `message.id` - see collect().
Counting transcript ROWS instead would inflate every figure.

ANTI-GOALS:
  - read-only; never writes a transcript, a status snapshot, or a log
  - never prints message CONTENT, only counts (output gets pasted into chats)
  - no claim that dollars == subscription usage; the two are printed apart
  - no new logging: if a number is not already on disk, it is not invented here

CLI:
    python profile_skill.py                        # every /claudette:done run found
    python profile_skill.py --skill claudette-done # any prompt substring (here: runs
                                                   # under the skill's former name)
    python profile_skill.py --all                  # group by leading /slash-command
    python profile_skill.py --since 2026-08-01
    python profile_skill.py --totals               # summary only, no per-run rows
"""
import bisect, datetime, glob, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

HOME = paths.HOME
PROJECTS = os.path.join(paths.CLAUDE_DIR, "projects")
STATUS = paths.USAGE_SNAPSHOTS

# API list price $/MTok, copied from the `claude-api` skill table.
# Used ONLY as a common unit across models. Unknown model -> falls back to opus
# rates and is flagged with '?' so the number is never silently wrong-priced.
PRICES = {
    "fable-5": (10.0, 50.0), "mythos-5": (10.0, 50.0),
    "opus-5": (5.0, 25.0), "opus-4-8": (5.0, 25.0), "opus-4-7": (5.0, 25.0),
    "opus-4-6": (5.0, 25.0), "opus-4-5": (5.0, 25.0), "opus-4-1": (15.0, 75.0),
    "sonnet-5": (3.0, 15.0), "sonnet-4-6": (3.0, 15.0), "sonnet-4-5": (3.0, 15.0),
    "haiku-4-5": (1.0, 5.0), "haiku-3-5": (0.8, 4.0),
}
CACHE_READ = 0.1        # x base input
CACHE_W_5M = 1.25       # x base input
CACHE_W_1H = 2.0        # x base input
SHORT = {"opus": "op", "sonnet": "so", "haiku": "hk", "fable": "fb", "mythos": "my"}


def price(model):
    key = (model or "").replace("claude-", "")
    key = re.sub(r"-2\d{7}$", "", key)          # drop a dated suffix
    if key in PRICES:
        return PRICES[key], False
    for k in PRICES:                            # prefix match for unseen variants
        if key.startswith(k):
            return PRICES[k], False
    return PRICES["opus-5"], True


def short(model):
    key = (model or "?").replace("claude-", "")
    m = re.match(r"([a-z]+)-(\d+)(?:-(\d+))?", key)
    if not m:
        return key[:6]
    return "%s%s%s" % (SHORT.get(m.group(1), m.group(1)[:2]), m.group(2), m.group(3) or "")


def _ts(row):
    t = row.get("timestamp")
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def prompt_text(row):
    """The user's own text, '' for tool results (same rule as profile_turn.py)."""
    c = (row.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_turn_start(row):
    # Sidechain rows are a subagent's own conversation: their tokens count toward
    # the run, but they must never be mistaken for the user starting a new turn.
    return (row.get("type") == "user" and not row.get("isSidechain")
            and prompt_text(row).strip())


def turn_groups(rows):
    """[(i_first, i_last, text)] - one entry per real prompt.

    Claude Code injects a slash command's expansion and a skill's SKILL.md body as
    ADDITIONAL user rows right after the typed one. Each must not count as its own
    turn: every group but the last has zero assistant rows after it, so a run
    measured from the wrong row reads as costing nothing.
    """
    out = []
    for i, r in enumerate(rows):
        if not is_turn_start(r):
            continue
        txt = prompt_text(r)
        if out and out[-1][1] == i - 1:
            first, _, prev = out[-1]
            out[-1] = (first, i, prev + "\n" + txt)
        else:
            out.append((i, i, txt))
    return out


# ---------------------------------------------------------------- status index

class Status(object):
    """Nearest status snapshot before/after a timestamp. Lazy: there are many files,
    only the handful bracketing a run are ever opened."""

    def __init__(self):
        self.idx = []
        for p in glob.glob(os.path.join(STATUS, "status_*.json")):
            m = re.match(r"status_(\d+)_", os.path.basename(p))
            if m:
                self.idx.append((int(m.group(1)) / 1000.0, p))
        self.idx.sort()
        self.times = [t for t, _ in self.idx]

    def near(self, ts, after):
        """(t, pct5h, resets_at, session, cost_usd) or None. Scans up to 8
        neighbours: a render can be missing rate_limits (cold start, API hiccup)."""
        i = bisect.bisect_left(self.times, ts)
        rng = range(i, min(len(self.idx), i + 8)) if after else range(i - 1, max(-1, i - 9), -1)
        for j in rng:
            if j < 0:
                continue
            try:
                with open(self.idx[j][1], encoding="utf-8") as f:
                    d = json.load(f)
            except (IOError, ValueError):
                continue
            rl = ((d.get("rate_limits") or {}).get("five_hour")) or {}
            if "used_percentage" in rl:
                return (self.idx[j][0], float(rl["used_percentage"]), rl.get("resets_at"),
                        d.get("session_id"), d.get("cost_usd"))
        return None


# ------------------------------------------------------------------- scanning

APPROVALS = ("commit", "yes", "yep", "y ", "go", "ok", "okay", "do it", "all", "skip",
             "proceed", "push", "sure", "correct", "right", "agreed", "i suggest")
EPISODE_CAP = 6          # follow-ups considered; a 7th means the boundary rule failed


def is_continuation(txt):
    """Does this prompt answer the done report, or start something new?

    Boundary heuristic, and it IS a heuristic - which is why episode mode prints
    the three-way bracket and echoes the prompts instead of reporting one number.
    Timing does not work (a long gap can still be a continuation) and neither does
    length alone (a short question such as "What is the build step?" is a new
    topic). What holds: a continuation REFERENCES THE REPORT'S NUMBERED ITEMS -
    "1 p", "1 3 (and fix the readme)", "2. use the docs ... 4 skip 1", "about 3:".
    """
    t = " ".join((txt or "").split())
    if not t:
        return False
    low = t.lower()
    if low.startswith("<command-name>") or low.startswith("<local-command"):
        return False                                   # a new slash command is a new turn
    if re.match(r"^(about\s+)?\d+[\s.,):]", t):        # "1 p", "3.", "about 3:"
        return True
    if re.match(r"^[\d\s,]+$", t):                     # "1 3"
        return True
    if any(low.startswith(a) for a in APPROVALS):
        return True
    if len(t) <= 25:
        return True
    return bool(re.search(r"\b\d+\s*[.)]\s", t[:80]))  # "2. use the docs ... 4 skip 1"


def episode(rows, groups, gi, run):
    """Attach episode totals: the invoke plus the turns that ACT on its report.

    Three numbers, because the true boundary is unknowable from the transcript:
      run['equiv']      invoke only          - hard lower bound
      run['ep_equiv']   invoke + heuristic   - best guess
      run['eos_equiv']  invoke to end of session - hard upper bound
    """
    ep_eq, ep_usd, ep_n, ep_wr = run["equiv"], run["usd"], 0, 0
    eos_eq, eos_usd = run["equiv"], run["usd"]
    follow, still = [], True
    for j in range(gi + 1, len(groups)):
        first, last, txt = groups[j]
        sub = collect(rows, last, _ts(rows[first]) or run["t0"])
        if not sub:
            continue
        eos_eq += sub["equiv"]
        eos_usd += sub["usd"]
        if still and is_continuation(txt) and len(follow) < EPISODE_CAP:
            ep_eq += sub["equiv"]
            ep_usd += sub["usd"]
            ep_n += 1
            ep_wr += writes(rows, last)
            follow.append((" ".join(re.sub(r"<[^>]+>", " ", txt).split())[:54], sub))
        else:
            still = False
    run.update(ep_equiv=ep_eq, ep_usd=ep_usd, ep_n=ep_n, ep_wr=ep_wr,
               eos_equiv=eos_eq, eos_usd=eos_usd, follow=follow)


def writes(rows, i):
    """Write-tool calls in one turn. The invoke turn has none; every write
    claudette-done causes happens in the turns that answer it - which is why
    episode mode exists."""
    n = 0
    for r in rows[i + 1:]:
        if is_turn_start(r):
            break
        if r.get("type") != "assistant":
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" \
                    and b.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                n += 1
    return n


def scan(match=None, group_all=False, since=None, want_episode=False):
    runs = []
    for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        except (IOError, ValueError):
            continue
        proj = os.path.basename(os.path.dirname(f))
        groups = turn_groups(rows)
        for gi, (first, last, txt) in enumerate(groups):
            if group_all:
                # A slash invocation reaches the transcript expanded, as
                # <command-name>/foo</command-name>; a typed mention does not.
                m = (re.search(r"<command-name>\s*/([a-z0-9][a-z0-9-]*)", txt, re.I)
                     or re.match(r"\s*/([a-z0-9][a-z0-9-]*)", txt, re.I))
                if not m:
                    continue
                label = m.group(1)
            else:
                if match.lower() not in txt.lower():
                    continue
                label = match
            t0 = _ts(rows[first])
            if t0 is None or (since and t0 < since):
                continue
            run = collect(rows, last, t0)
            if run:
                run.update(label=label, proj=proj[:18], t0=t0,
                           sess=os.path.basename(f)[:8], wr=writes(rows, last))
                if want_episode:
                    episode(rows, groups, gi, run)
                runs.append(run)
    runs.sort(key=lambda x: x["t0"])
    return runs


def collect(rows, i, t0):
    """Aggregate usage over every assistant RESPONSE until the next real user turn.

    ONE API response is written to the transcript as one row PER CONTENT BLOCK
    (text / thinking / each tool_use), and the SAME `usage` object is repeated on
    every one of them. Summing per row therefore multiplies output, cache read and
    cache write by that response's block count, worse on tool-heavy turns. Dedupe on
    `message.id`: it is the response identity, and the row count is not.
    """
    per = {}                       # model -> counters
    seen = set()                   # message.id of responses already counted
    trips = side = blocks = 0
    t1 = t0
    for r in rows[i + 1:]:
        if is_turn_start(r):
            break
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        blocks += 1
        mid = msg.get("id")
        # No id (a transcript is not a contract): count the row, which only ever
        # over-counts.
        if mid is not None:
            if mid in seen:
                e = _ts(r)
                if e:
                    t1 = max(t1, e)
                continue
            seen.add(mid)
        model = msg.get("model") or "?"
        c = per.setdefault(model, dict(inp=0, out=0, w5=0, w1=0, rd=0, n=0))
        c["inp"] += u.get("input_tokens", 0)
        c["out"] += u.get("output_tokens", 0)
        c["rd"] += u.get("cache_read_input_tokens", 0)
        cc = u.get("cache_creation") or {}
        w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
        if w5 is None and w1 is None:
            # older transcripts: only the flat total. Assume 5m (the cheaper of
            # the two) so an unknown never inflates the estimate.
            w5 = u.get("cache_creation_input_tokens", 0)
            w1 = 0
        c["w5"] += w5 or 0
        c["w1"] += w1 or 0
        c["n"] += 1
        trips += 1
        if r.get("isSidechain"):
            side += 1
        e = _ts(r)
        if e:
            t1 = max(t1, e)
    if not per:
        return None
    equiv = usd = 0.0
    guessed = False
    tot = dict(inp=0, out=0, w5=0, w1=0, rd=0)
    for model, c in per.items():
        (pin, pout), g = price(model)
        guessed = guessed or g
        e = (c["inp"] + c["rd"] * CACHE_READ + c["w5"] * CACHE_W_5M
             + c["w1"] * CACHE_W_1H + c["out"] * (pout / pin))
        equiv += e
        usd += e * pin / 1e6
        for k in tot:
            tot[k] += c[k]
    return dict(per=per, trips=trips, side=side, blocks=blocks, dur=int(t1 - t0), t1=t1,
                equiv=equiv, usd=usd, guessed=guessed,
                models="+".join(sorted(short(m) for m in per)), **tot)


def bracket(run, st):
    """Attach the measured 5h-window delta, with every reason it might be junk."""
    b, a = st.near(run["t0"], False), st.near(run["t1"] + 30, True)
    run["d5h"] = run["dcost"] = None
    run["flag"] = ""
    if not (b and a):
        run["flag"] = "nocov"
        return
    run["gap"] = int((run["t0"] - b[0]) + (a[0] - run["t1"]))
    if b[2] and a[2] and b[2] != a[2]:
        run["flag"] = "reset"          # window rolled over mid-run
        return
    run["d5h"] = a[1] - b[1]
    if b[3] and b[3] == a[3] and b[4] is not None and a[4] is not None:
        run["dcost"] = a[4] - b[4]     # only meaningful within one session
    if run["gap"] > 300:
        run["flag"] = "wide"           # nearest render is far -> other work included


# -------------------------------------------------------------------- output

def k(n):
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fk" % (n / 1e3)
    return str(int(n))


def report(runs, totals_only=False, ep=False):
    if not runs:
        print("no matching runs found under %s" % PROJECTS)
        return 1
    if not totals_only and not ep:
        print("%-12s %-18s %-7s %3s %5s %7s %8s %8s %7s %6s %6s %s"
              % ("when", "proj", "model", "trp", "dur", "out", "cacheW", "cacheR",
                 "equivK", "list$", "d5h", "flag"))
        for r in runs:
            d5 = "  -  " if r["d5h"] is None else "%+5.1f" % r["d5h"]
            print("%-12s %-18s %-7s %3d %5d %7s %8s %8s %7s %6.2f %6s %s"
                  % (datetime.datetime.fromtimestamp(r["t0"]).strftime("%m-%d %H:%M"),
                     r["proj"], r["models"] + ("?" if r["guessed"] else ""),
                     r["trips"], r["dur"], k(r["out"]), k(r["w5"] + r["w1"]), k(r["rd"]),
                     k(r["equiv"]), r["usd"], d5, r["flag"]))
        print("")
    elif not totals_only:
        print("%-12s %-18s %3s %3s %8s %8s %8s   %s"
              % ("when", "proj", "trp", "wr", "invoke", "episode", "to-EOS", "follow-ups"))
        for r in runs:
            print("%-12s %-18s %3d %3d %8s %8s %8s   %s"
                  % (datetime.datetime.fromtimestamp(r["t0"]).strftime("%m-%d %H:%M"),
                     r["proj"], r["trips"], r["wr"], k(r["equiv"]),
                     k(r["ep_equiv"]), k(r["eos_equiv"]),
                     "%d turn%s" % (r["ep_n"], "" if r["ep_n"] == 1 else "s")
                     + (", %d write%s" % (r["ep_wr"], "" if r["ep_wr"] == 1 else "s")
                        if r["ep_wr"] else "")))
            for txt, sub in r["follow"]:
                print("%42s+ %7s  %s" % ("", k(sub["equiv"]), txt))
        print("")

    n = len(runs)
    med = lambda key: sorted(r[key] for r in runs)[n // 2]
    print("n=%d runs   %s -> %s"
          % (n, datetime.datetime.fromtimestamp(runs[0]["t0"]).strftime("%Y-%m-%d"),
             datetime.datetime.fromtimestamp(runs[-1]["t0"]).strftime("%Y-%m-%d")))
    for key, name in (("trips", "round trips"), ("dur", "duration s"), ("out", "output tok"),
                      ("rd", "cache read"), ("equiv", "input-equiv"), ("usd", "list $")):
        vals = sorted(r[key] for r in runs)
        f = (lambda v: "%.2f" % v) if key == "usd" else k
        print("  %-12s median %8s   min %8s   max %8s" % (name, f(med(key)), f(vals[0]), f(vals[-1])))

    # The point of the whole tool: which term is worth optimizing.
    print("\n  where the weighted cost goes (median run, input-token-equivalents):")
    mid = sorted(runs, key=lambda r: r["equiv"])[n // 2]
    (pin, pout), _ = price(sorted(mid["per"], key=lambda m: -mid["per"][m]["n"])[0])
    terms = (("cache write", mid["w5"] * CACHE_W_5M + mid["w1"] * CACHE_W_1H),
             ("cache read", mid["rd"] * CACHE_READ),
             ("output", mid["out"] * (pout / pin)),
             ("fresh input", mid["inp"]))
    tot = sum(v for _, v in terms) or 1.0
    for name, v in sorted(terms, key=lambda x: -x[1]):
        print("    %-12s %8s  %4.0f%%  %s" % (name, k(v), 100 * v / tot, "#" * int(30 * v / tot)))
    print("    per-round-trip marginal cost: %s equiv  (context re-read, %d trips)"
          % (k(mid["rd"] * CACHE_READ / max(mid["trips"], 1)), mid["trips"]))

    if ep:
        for key, name in (("ep_equiv", "episode equiv"), ("eos_equiv", "to-EOS equiv")):
            vals = sorted(r[key] for r in runs)
            print("  %-14s median %8s   min %8s   max %8s"
                  % (name, k(vals[n // 2]), k(vals[0]), k(vals[-1])))
        inv, epq = med("equiv") or 1.0, med("ep_equiv")
        print("  -> the work built ON TOP of the report is %.1fx the report itself (median);"
              % (epq / inv))
        print("     %d of %d runs wrote a file, NONE of them in the invoke turn."
              % (sum(1 for r in runs if r["ep_wr"]), n))
        print("     invoke <= truth <= to-EOS. The middle column is a heuristic - check the")
        print("     echoed follow-ups above before quoting it.")

    ok = [r for r in runs if r["d5h"] is not None and not r["flag"]]
    print("\n  measured 5h-window delta (%d of %d runs bracketed cleanly):" % (len(ok), n))
    if ok:
        v = sorted(r["d5h"] for r in ok)
        print("    %s   median %+.1f pt" % (" ".join("%+.0f" % x for x in v), v[len(v) // 2]))
    print("    CAUTION: used_percentage is integer-resolution on a ROLLING window that")
    print("    decays during the run, and other activity lands inside the bracket. Treat")
    print("    these as an order of magnitude, not a per-call figure. A real number needs")
    print("    an A/B in an idle window: note 5h%, run the skill alone, note 5h% again.")
    print("    Multiply by your plan factor: a lower-tier seat burns several times the points a higher-tier seat does.")
    return 0


def main(argv):
    match, group_all, totals_only, since, ep = "claudette:done", False, False, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--skill" and i + 1 < len(argv):
            match = argv[i + 1]; i += 1
        elif a == "--all":
            group_all = True
        elif a == "--totals":
            totals_only = True
        elif a == "--episode":
            ep = True
        elif a == "--since" and i + 1 < len(argv):
            since = datetime.datetime.strptime(argv[i + 1], "%Y-%m-%d").timestamp(); i += 1
        else:
            sys.stderr.write(__doc__); return 2
        i += 1

    runs = scan(match, group_all, since, ep)
    st = Status()
    if not st.idx:
        sys.stderr.write("warning: no status snapshots under %s - no 5h deltas\n" % STATUS)
    for r in runs:
        bracket(r, st)

    if group_all:
        rc = 0
        for label in sorted({r["label"] for r in runs}):
            sub = [r for r in runs if r["label"] == label]
            print("\n=== /%s ===" % label)
            rc |= report(sub, totals_only=True, ep=ep)
        return rc
    print("skill: %s" % match)
    return report(runs, totals_only, ep=ep)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
