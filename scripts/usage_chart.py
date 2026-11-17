#!/usr/bin/env python3
"""The usage chart's mechanical half: collect, document, render, remember.

The 5-hour and 7-day window percentages exist ONLY in the status-line payload the
status line writes to `logs/claude_usage_snapshots/`. Nothing else on the machine has
them and no telemetry carries them, so a chart of "how full is the week" can only be
built from those snapshots — per machine, which is why they have to be merged somewhere
that every machine can reach.

That somewhere is a published artifact: a ~1.5 KB loader page, the renderer as three
published files, a machine index (`data/index.json`, written only when a machine joins),
and one published FILE per machine, `data/<stem>.json`, that only that machine ever
writes — so two machines never collide, and the page unions the files when it draws.
The artifact's database is a read-only archive (see skills/chart/DESIGN.md). Only an
agent turn can publish (the Artifact tool), so this module does everything EXCEPT the
network: it builds the files and says what to publish.

    usage_chart.py addr [--set URL]     where the chart lives (cache, then discovery)
    usage_chart.py collect [--out F]    this machine's readings from the snapshots
    usage_chart.py prepare --out-dir D [--force] [--index F|new] [--merge F|none]
                                        what to publish, or why not to bother
    usage_chart.py pushed --ok          the publish landed (rides another command)
    usage_chart.py state                one JSON line for `.chart`, O(1)

Verbs print human lines on stdout; `addr` and `state` print JSON. Skill: /claudette:chart.
"""
import argparse, json, os, re, socket, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                                                  # noqa: E402

# The one host that opens the artifact after a publish, raising its "open the
# external website?" trust dialog. Automatic publishes are held there: see prepare(),
# and skills/chart/DESIGN.md "Held on the VS Code host, for now" for what lifts it.
VSCODE_HOST = "claude-vscode"

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(paths.ROOT, "skills", "chart")

BUCKET = 300          # one reading per 5 minutes (percentages are integers anyway)
WEEK = 7 * 86400
FMT = 2               # collect cache format; bump to discard caches predating a field
KEEP_WINDOWS = 5      # windows the page offers as tabs (view.js KEEP)
HORIZON_DAYS = KEEP_WINDOWS * 7 + 1
POOL = "Fable"
ASSETS = os.path.join(SKILL, "assets")
DATA_DIR = "data"
# The renderer: published beside index.html by every push, from the pushing machine's own
# copy. RENDERER is how the agent tells whether the live one is newer than ours — then it
# publishes nothing — so it must move with every change to these files and the loader:
# tests/test_usage_chart_files.py pins it to their hash, which is what stops a machine
# with an older copy from rolling the page back.
RENDERER = 7
RENDERER_FILES = ("chart.css", "view.js", "page.js")

# MARKER is the KEY. It is the one string a machine with no cached address matches on,
# and it names claudette on purpose: an account holds artifacts from anything, so a key
# that could be read as a generic label for "some usage chart" is not an identity.
# It is FROZEN: change it and every machine that has not cached the address stops finding
# the page — and the one failure this design cannot heal is a SECOND chart, splitting the
# history in two.
#
# WHERE the marker lives is IDENTITY_COLLECTION/IDENTITY_DOC in the artifact's own
# database, because that is the only place an agent can read it: no Artifact action
# returns a description, and a read returns the whole page, far too big to probe with.
# The identity document is one tiny `get`.
#
# TITLE is only the label published on the page and the gallery card. A title is prose
# and gets improved, so it is a HINT — it narrows the listing to the candidates worth
# probing — never the identity test. A title mismatch is worth a warning, never a
# rejection.
TITLE = "Usage Chart (as seen by claudette)"
MARKER = "claudette:usage-chart:v1"
IDENTITY_COLLECTION = "meta"      # `get meta/identity` -> {"marker": MARKER}: the probe
IDENTITY_DOC = "identity"         # a candidate without it is a clean NON-match, not an error

ADDR = paths.log("usage_chart_addr.json")     # this machine's copy of the address
CACHE = paths.log("usage_chart_cache.json")   # read-through mtime + kept readings
PUSHED = paths.log("usage_chart_pushed.json")  # what this machine last handed over
MINE = paths.log("usage_chart_mine.json")      # this machine's file as last built

# The auto-update trigger, in the units the chart draws: percent of movement, not a
# count of readings, because a short burst of parallel agents is few readings and can be
# tens of points of the 5-hour window. An extra round trip costs far more than a small
# push is worth (see DESIGN.md "What the auto-update costs" and "The trigger is
# movement, not volume").
MIN_MOVE = 1            # percent of movement worth an update — a constant, not a knob


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, separators=(",", ":"))
        return True
    except Exception:
        return False                       # state that cannot be written is not an error


def file_stem(name):
    """A machine name as a published file name. The page builds the URL from it, so it
    must never need escaping: everything outside [A-Za-z0-9_.-] becomes `_`."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:60] or "unknown"


def hint(url):
    """The `[claudette chart]` line a typed /claudette:chart gets before its first message:
    the address, so the page read rides beside `prepare`, and this machine's own file, so
    a read of it by path rides there too. That
    read is what lets the publish through when another session on this machine published
    the file after this session last did: the Artifact tool refuses to replace a file the
    session saw at an older version, and a listing does not update what it saw (DESIGN.md,
    "Sessions on one machine")."""
    return "[claudette chart] url=%s own=data/%s.json" % (url, file_stem(socket.gethostname()))


def tz_offset_min(now=None):
    lt = time.localtime(now if now is not None else time.time())
    off = -(time.altzone if (time.daylight and lt.tm_isdst) else time.timezone)
    return off // 60


# ---------------------------------------------------------------- addr

def cmd_addr(a):
    """Where the chart lives: this machine's cache, else nothing.

    There is deliberately no synced config for this. `prefs.local.json` and `logs/` are
    machine-local by construction, and a shared repo must never carry one user's
    artifact address — so the ACCOUNT is the sync channel: on exit 3 the agent lists its
    own artifacts, probes the candidates for the identity document, and calls `--set`.
    A new machine needs no configuration at all.
    """
    if a.set:
        if not a.set.startswith("https://claude.ai/"):
            sys.exit("not a claude.ai artifact URL: %s" % a.set)
        _write_json(ADDR, {"url": a.set, "title": TITLE, "marker": MARKER,
                           "cached_at": int(time.time())})
        print(json.dumps({"url": a.set, "from": "set", "cache": paths.tilde(ADDR, "~")}))
        return 0
    probe = {"collection": IDENTITY_COLLECTION, "doc_id": IDENTITY_DOC,
             "expect": {"marker": MARKER}}
    u = (_read_json(ADDR) or {}).get("url")
    if u:
        print(json.dumps({"url": u, "from": "cache", "title": TITLE, "marker": MARKER,
                          "identity": probe}))
        return 0
    print(json.dumps({"url": None, "from": "none", "title": TITLE, "marker": MARKER,
                      "identity": probe,
                      "next": "list your own artifacts; TITLE only narrows the candidates; "
                              "read `identity` off each with ArtifactData get — exactly one "
                              "carrying the marker is the chart (none: ask, never create; "
                              "two: stop) — then --set"}))
    return 3


# ---------------------------------------------------------------- collect

def load_cache(days, pool):
    c = _read_json(CACHE)
    if (c.get("fmt") != FMT or c.get("pool") != pool
            or c.get("bucket") != BUCKET or c.get("days", 0) < days):
        return {}, 0.0                                  # different question: start over
    floor = time.time() - days * 86400
    buckets = {}
    for k, pt in (c.get("buckets") or {}).items():
        reset, bkt = k.split(":")
        if pt[0] >= floor:
            buckets[(int(reset), int(bkt))] = pt
    return buckets, float(c.get("read_through") or 0.0)


def collect(days, pool, use_cache=True):
    """Readings from the snapshot folder. Only files whose mtime falls in the horizon
    are opened: the folder holds tens of thousands of files and grows without bound, so
    a full scan costs minutes and stat-then-filter costs seconds.
    Never widen this to a full scan."""
    snaps = paths.USAGE_SNAPSHOTS
    if not os.path.isdir(snaps):
        sys.exit("no %s — this machine has never rendered a status line"
                 % paths.tilde(snaps, "~"))

    horizon = max(time.time() - days * 86400, 0)
    buckets, read_through = load_cache(days, pool) if use_cache else ({}, 0.0)
    cached = len(buckets)
    # re-read the last minute either way: a file written while the previous run was
    # walking the folder would otherwise be skipped forever
    floor = max(horizon, read_through - 60)
    scanned, opened, bad, newest = 0, 0, 0, read_through
    with os.scandir(snaps) as it:
        for e in it:
            scanned += 1
            if not e.name.endswith(".json"):
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            if mt < floor:
                continue
            newest = max(newest, mt)
            opened += 1
            try:
                with open(e.path, "rb") as f:
                    j = json.loads(f.read().decode("utf-8", "replace"))
            except Exception:
                bad += 1
                continue
            rl = j.get("rate_limits") or {}
            sd, fh = rl.get("seven_day") or {}, rl.get("five_hour") or {}
            pct, reset = sd.get("used_percentage"), sd.get("resets_at")
            h5 = fh.get("used_percentage")
            h5 = int(round(h5)) if isinstance(h5, (int, float)) else None
            h5r = fh.get("resets_at") or None
            ts = int(j.get("ts") or 0)
            if pct is None or not reset or not ts:
                continue                    # unmeasured render, or an unplaceable window
            fab = None
            for s in (j.get("scoped_limits") or []):
                if s.get("name") == pool and s.get("pct") is not None:
                    fab = int(s["pct"])
            key = (int(reset), ts // BUCKET)
            prev = buckets.get(key)
            # within one window usage only rises, so the max is the latest truth; it
            # also makes the result independent of which run saw which file
            if prev is None or int(round(pct)) > prev[1]:
                buckets[key] = [ts, int(round(pct)), fab, h5, h5r]
            else:
                if fab is not None and (prev[2] is None or fab > prev[2]):
                    prev[2] = fab
                # the 5h window turns over inside a 7d bucket, so take the NEWER
                # reading rather than the larger one: a reset is a real fall
                if h5 is not None and h5r and (prev[4] is None or h5r >= prev[4]):
                    prev[3], prev[4] = h5, h5r

    if use_cache:
        _write_json(CACHE, {"fmt": FMT, "pool": pool, "bucket": BUCKET, "days": days,
                            "read_through": newest,
                            "buckets": {"%d:%d" % k: v for k, v in buckets.items()}})

    windows = {}
    for (reset, _), pt in buckets.items():
        if pt[0] >= horizon:
            windows.setdefault(reset, []).append(pt)
    out = {"v": 1,
           "source": {"id": socket.gethostname(), "host": socket.gethostname(),
                      "collected_at": int(time.time()), "days": days},
           "windows": [{"resets_at": r, "points": sorted(windows[r])}
                       for r in sorted(windows)]}
    out["source"]["points"] = sum(len(w["points"]) for w in out["windows"])
    return out, scanned, opened, bad, cached


def cmd_collect(a):
    t0 = time.time()
    data, scanned, opened, bad, cached = collect(a.days, a.pool, not a.no_cache)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, separators=(",", ":"))
    print("machine %s: %d window(s), %d readings (%s)"
          % (data["source"]["id"], len(data["windows"]), data["source"]["points"],
             ", ".join("%s: %d" % (time.strftime("%b %d",
                                                 time.localtime(w["resets_at"] - WEEK)),
                                   len(w["points"])) for w in data["windows"]) or "none"))
    print("scanned %d files, opened %d (%d readings from the cache), unreadable %d, "
          "%.1fs -> %s" % (scanned, opened, cached, bad, time.time() - t0, a.out))
    return 0


# ---------------------------------------------------------------- readings

def clean_row(r, start, reset):
    """One reading, or None. Anything read back — a published file, an old record — is
    data, not a trusted input, even though we wrote it."""
    try:
        ts, acct = int(r[0]), int(r[1])
        fab = None if len(r) < 3 or r[2] is None else int(r[2])
        h5 = None if len(r) < 4 or r[3] is None else int(r[3])
        h5r = None if len(r) < 5 or r[4] is None else int(r[4])
    except Exception:
        return None
    if not (start - 3600 <= ts <= reset + 3600):
        return None
    if not (0 <= acct <= 100) or (fab is not None and not 0 <= fab <= 100):
        return None
    if h5 is not None and not 0 <= h5 <= 100:
        h5, h5r = None, None
    return [ts, acct, fab, h5, h5r]


def union(rows, more, start, reset):
    keyed = {}
    for r in list(rows) + list(more):
        c = clean_row(r, start, reset)
        if c is None:
            continue
        k = c[0] // BUCKET
        cur = keyed.get(k)
        if cur is None:
            keyed[k] = c
            continue
        if c[1] > cur[1]:
            cur[0], cur[1] = c[0], c[1]
        if c[2] is not None and (cur[2] is None or c[2] > cur[2]):
            cur[2] = c[2]
        if c[3] is not None and c[4] and (cur[4] is None or c[4] > cur[4]
                                          or (c[4] == cur[4] and c[3] > (cur[3] or 0))):
            cur[3], cur[4] = c[3], c[4]
    return sorted(keyed.values())


def file_docs(obj, me):
    """The window documents inside a machine file, this machine's only. A file that is
    not one (wrong shape, another machine's) contributes nothing rather than an error."""
    if not isinstance(obj, dict) or not isinstance(obj.get("docs"), list):
        return {}
    out = {}
    for d in obj["docs"]:
        if isinstance(d, dict) and d.get("m") == me and isinstance(d.get("rows"), list):
            try:
                out[int(d["w"])] = d["rows"]
            except (KeyError, TypeError, ValueError):
                continue
    return out


def build_file(me, tz, collected, *bases, now=None):
    """This machine's file: every window it knows, from any source, unioned and pruned.

    The sources are the fresh collection and the file as this machine last built it —
    plus, once per install, the file the chart already holds (`--merge`). The local copy
    is what makes a whole-file rewrite safe: the snapshot folder alone would silently drop
    whatever it no longer has, and a reinstalled machine has none of it."""
    now = int(now or time.time())
    floor = now - HORIZON_DAYS * 86400
    windows = {}
    for base in bases:
        for reset, rows in base.items():
            windows.setdefault(reset, []).extend(rows)
    for w in collected.get("windows", []):
        windows.setdefault(int(w["resets_at"]), []).extend(w.get("points", []))
    docs = []
    for reset in sorted(windows):
        if reset < floor:
            continue                       # nothing in it the page can still show
        rows = union([], windows[reset], reset - WEEK, reset)
        if rows:
            # `updated` per document too: the page reads it as the moment this machine's
            # logs were last read whole, the end of the coverage its roll lane draws
            docs.append({"m": me, "w": reset, "tz": tz, "updated": now, "rows": rows})
    return {"v": 1, "m": me, "tz": tz, "updated": now, "docs": docs}


# ---------------------------------------------------------------- the trigger

def marks(rows):
    """What the chart would DRAW from these rows: the numbers, not their count.

    `h5n` counts the distinct 5-hour windows, because a new step is a visible change even
    when its peak is lower than an earlier one in the same week.
    """
    fab = [r[2] for r in rows if r[2] is not None]
    h5 = [r[3] for r in rows if r[3] is not None]
    return {"acct": max(r[1] for r in rows),
            "fable": max(fab) if fab else None,
            "h5": max(h5) if h5 else 0,
            "h5n": len({r[4] for r in rows if r[4]}),
            "n": len(rows)}


def movement(now_marks, then):
    """How far the drawn numbers moved, in points, or None when nothing did.

    A window nobody has reported yet counts as movement whatever it holds: the chart has
    no line there at all.
    """
    if not then:
        return {"reason": "this week is not in the chart yet", "points": now_marks["acct"]}
    d = {"account": now_marks["acct"] - int(then.get("acct") or 0),
         POOL: (now_marks["fable"] or 0) - (then.get("fable") or 0),
         "5h peak": now_marks["h5"] - int(then.get("h5") or 0)}
    best = max(d, key=lambda k: d[k])
    if now_marks["h5n"] > int(then.get("h5n") or 0):
        n = now_marks["h5n"] - int(then.get("h5n") or 0)
        return {"reason": "%d new 5-hour window(s)" % n, "points": max(d[best], 0)}
    if d[best] > 0:
        return {"reason": "%s +%d%%" % (best, d[best]), "points": d[best]}
    return None


# ---------------------------------------------------------------- prepare / pushed

STEM_OK = re.compile(r"^[A-Za-z0-9_.-]+$")


def index_machines(obj):
    """The machine stems a `data/index.json` lists — only names this module could have
    minted, so a tampered or foreign file cannot make the page fetch an odd path."""
    ms = obj.get("machines") if isinstance(obj, dict) else None
    return sorted({str(s) for s in (ms or []) if STEM_OK.match(str(s))})


def render_index(idx):
    with open(os.path.join(ASSETS, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return html.replace("__INDEX__", json.dumps(idx, separators=(",", ":")))


def bundle(out_dir, stem, mine, index=None):
    """Write everything one publish sends into `out_dir`: the loader, the renderer, this
    machine's file, and — only when this machine is joining — the machine index.

    The loader and the renderer go out on EVERY publish, from this machine's own copy.
    Nothing read back from the live page is ever republished: a read returns the page
    inside the host's wrapper, and publishing that would nest one wrapper per push. The
    same bytes from every machine on the same RENDERER make the resend a no-op; the
    agent's version check (the live `renderer` must not be newer than ours) makes it
    never a rollback. They are passed by path, so they cost no tokens.
    """
    os.makedirs(os.path.join(out_dir, DATA_DIR), exist_ok=True)
    page = os.path.join(out_dir, "index.html")
    with open(page, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_index({"renderer": RENDERER}))
    files = {}
    for name in RENDERER_FILES:
        dst = os.path.join(out_dir, name)
        with open(os.path.join(ASSETS, name), encoding="utf-8") as s, \
                open(dst, "w", encoding="utf-8", newline="\n") as d:
            d.write(s.read())
        files[name] = dst
    data = os.path.join(out_dir, DATA_DIR, stem + ".json")
    with open(data, "w", encoding="utf-8", newline="\n") as f:
        json.dump(mine, f, separators=(",", ":"))
    files["%s/%s.json" % (DATA_DIR, stem)] = data
    if index is not None:
        p = os.path.join(out_dir, DATA_DIR, "index.json")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"v": 1, "machines": index}, f, separators=(",", ":"))
        files["%s/index.json" % DATA_DIR] = p
    return page.replace("\\", "/"), {k: v.replace("\\", "/") for k, v in files.items()}


def prepare(out_dir, min_move=MIN_MOVE, force=False, merge=None, index=None):
    """Everything the agent needs to publish, in one call, or a reason not to bother.

    The update is ONE published file, `data/<stem>.json`, that only this machine ever
    writes — so no two machines collide and nothing needs deleting — sent with this
    machine's copy of the loader and renderer (`bundle`). Publishing raises no approval
    prompt, where a database write does (DESIGN.md, "The store is published files").
    Its price is a plain read of the live page first, which the agent makes in the same
    message as this call.

    `out_dir` must be the session's scratchpad: the Artifact tool publishes only files
    under the working directory or the scratchpad, and ~/.claude is neither in most
    sessions.

    Two things need a read by path first, so they return `publish: null` and a `next`:
    - this machine is not yet known to be in `data/index.json` (`index`: the live index
      as read, or "new" when the chart has none), because the page only fetches what the
      index lists and a machine missing from it is published but invisible;
    - this install has no local copy of its file (`merge`: the live one, when the chart
      has it), because a whole-file rewrite from an empty install would erase it.
    Both happen once per machine (or per reinstall), never in steady state.

    `force` means a person typed the verb: publish even if nothing moved — and it is
    also what lets an update through from the VS Code host, which auto-opens what it
    publishes.
    """
    addr = _read_json(ADDR)
    url = addr.get("url")
    rec = _read_json(PUSHED)
    now = int(time.time())
    if not url:
        return {"publish": None, "url": None,
                "skipped": "no chart address cached on this machine — run "
                           "/claudette:chart once; never create a chart automatically"}
    if not force and os.environ.get("CLAUDE_CODE_ENTRYPOINT") == VSCODE_HOST:
        # Held until VS Code records usage itself (skills/chart/DESIGN.md, "Held on the
        # VS Code host, for now"). Only a typed /claudette:chart gets through, by way of
        # its --force. Nothing is lost: the readings stay in the snapshot folder and go
        # out on the next run from a terminal.
        return {"publish": None, "url": url,
                "skipped": "held on VS Code, which records no usage of its own: the next "
                           "report from a terminal sends the readings, or type "
                           "/claudette:chart"}
    if not out_dir:
        return {"publish": None, "url": url,
                "skipped": "no --out-dir: pass this session's scratchpad (the only place "
                           "the Artifact tool publishes from in every session)"}

    data, _scanned, _opened, _bad, _cached = collect(HORIZON_DAYS, POOL, True)
    me = data["source"]["id"]
    stem = file_stem(me)
    tz = tz_offset_min()
    first_run = not os.path.isfile(MINE)
    listed = rec.get("listed") == url
    bases = [file_docs(_read_json(MINE), me)]
    if merge and merge != "none":
        got = file_docs(_read_json(merge), me)
        if not got:
            return {"publish": None, "url": url,
                    "skipped": "--merge %s holds none of %s's windows — not merged, nothing "
                               "published" % (merge, me)}
        bases.append(got)
    machines = None
    if index is not None:
        machines = sorted(set(index_machines(_read_json(index)) if index != "new" else [])
                          | {stem})
    mine = build_file(me, tz, data, *bases, now=now)
    if not mine["docs"]:
        return {"publish": None, "url": url, "skipped": "no readings on this machine yet"}

    reported = rec.get("reported") or {}  # window -> the marks last handed over
    moves, marks_by_window = [], {}
    for d in mine["docs"]:
        m = marks(d["rows"])
        marks_by_window[str(d["w"])] = m
        mv = movement(m, reported.get(str(d["w"])))
        if mv and (mv["points"] >= min_move or "5-hour" in mv["reason"]):
            moves.append("%s: %s" % (time.strftime("%b %d", time.localtime(d["w"] - WEEK)),
                                     mv["reason"]))
    rows_total = sum(len(d["rows"]) for d in mine["docs"])

    pending = bool(rec.get("pending"))
    if pending and not force and not moves and (now - int(rec.get("ts") or 0)) < 900:
        # An update is out and unconfirmed. Re-sending the same numbers because
        # `pushed --ok` was skipped would spend a publish on nothing, and consecutive
        # report replies re-run this within seconds. After 15 minutes a lost publish is
        # likelier than a missed confirmation, so retry.
        return {"publish": None, "url": url, "rows": rows_total,
                "skipped": "an update went out %dm ago and was never confirmed; nothing "
                           "has moved since" % ((now - int(rec.get("ts") or 0)) // 60)}
    if not force and not pending and not moves:
        near = [m for m in [movement(marks_by_window[str(d["w"])], reported.get(str(d["w"])))
                            for d in mine["docs"]] if m]
        return {"publish": None, "url": url, "rows": rows_total,
                "skipped": ("nothing the chart shows has changed by %d%% since the "
                            "last update" % min_move
                            + ("" if not near else
                               " (closest: %s)" % max(near, key=lambda m: m["points"])["reason"]))}

    need = []
    if not listed and machines is None:
        need.append("index")
    if first_run and not merge and machines is None:
        need.append("merge")
    if need:
        # Nothing written, nothing recorded: the next call, with what was read, does it.
        reads = ["data/index.json"] + (["data/%s.json" % stem] if "merge" in need else [])
        return {"publish": None, "url": url, "stem": stem, "needs": need,
                "next": ("the listing from this message decides: `Artifact read` with "
                         "`path` each of %s that it shows, then rerun `prepare --force "
                         "--out-dir <same> --index <saved index.json, or: new if the listing "
                         "has none>%s`"
                         % (", ".join(reads),
                            " --merge <saved %s.json, or: none if the listing has none>" % stem
                            if "merge" in need else ""))}

    page, files = bundle(out_dir, stem, mine, machines)
    _write_json(MINE, mine)         # the superset the next run builds on, sent or not
    _write_json(PUSHED, dict(
        {k: v for k, v in rec.items() if k not in ("live", "attempted", "retiring", "strays")},
        **{"flow": 2, "ts": now, "machine": me, "rows": rows_total,
           "windows": {str(d["w"]): len(d["rows"]) for d in mine["docs"]},
           # a window that ages out of the horizon leaves the record with it, or it poisons
           # every later comparison
           "reported": {k: v for k, v in reported.items() if k in marks_by_window},
           "attempted_marks": marks_by_window, "pending": True,
           "attempted_index": url if machines is not None else None}))
    return {"publish": {"url": url, "file_path": page, "files": files},
            "renderer": RENDERER, "stem": stem,
            "rows": rows_total, "windows": len(mine["docs"]),
            "moved": moves or (["retry of an unconfirmed update"] if pending else ["forced"]),
            "joins_index": machines,
            "note": ("publish exactly this, unless the live page's chart-index renderer is "
                     "NEWER than %d: then publish nothing — this machine's claudette is "
                     "older than the chart's" % RENDERER)}


def cmd_prepare(a):
    out = prepare(a.out_dir, force=a.force, merge=a.merge, index=a.index)
    print(json.dumps(out))
    return 0


def cmd_pushed(a):
    """`--ok`: the publish landed. Promotes the marks that were sent, so the trigger
    compares against what the chart now shows; records that this machine is in the
    index when that publish carried it; clears `pending`. Ride it along with whatever
    command the run makes next — on its own it is a round trip for one boolean, and a
    missed one costs one re-send (and, on a joining run, one more index read)."""
    rec = _read_json(PUSHED)
    reported = dict(rec.get("reported") or {})
    reported.update(rec.get("attempted_marks") or {})
    for gone in ("live", "attempted", "retiring", "strays"):   # the database flow's keys
        rec.pop(gone, None)
    if rec.get("attempted_index"):
        rec["listed"] = rec["attempted_index"]
    rec.update({"pending": False, "confirmed_at": int(time.time()),
                "reported": reported, "attempted_marks": {}, "attempted_index": None})
    _write_json(PUSHED, rec)
    print("confirmed: %d window(s), %d rows in the chart"
          % (len(reported), sum(int(v.get("n") or 0) for v in reported.values())))
    return 0


def state():
    """What `.chart` reports. O(1) on purpose — a dot-command answers inside the hook,
    on the user's keystroke, so it may never walk the snapshot folder (tens of
    thousands of files). Three small reads and one stat; no subprocess, because
    `dot_chart` imports this module rather than shelling out to it."""
    now = int(time.time())
    addr = _read_json(ADDR)
    pushed = _read_json(PUSHED)
    last_render = 0
    try:
        last_render = int(os.path.getmtime(paths.USAGE_LAST))
    except OSError:
        pass
    st = {"url": addr.get("url"), "title": TITLE,
          "last_push_ts": pushed.get("ts") or 0,
          "last_push_rows": pushed.get("rows") or 0,
          "last_push_windows": len(pushed.get("windows") or {}),
          "last_render_ts": last_render,
          "stale_s": ((last_render - pushed["ts"]) if (last_render and pushed.get("ts"))
                      else None),
          "now": now}
    return st


def cmd_state(a):
    print(json.dumps(state()))
    return 0


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("addr", help="where the chart lives")
    p.add_argument("--set", metavar="URL", help="cache this address on this machine")
    p.set_defaults(fn=cmd_addr)

    p = sub.add_parser("collect", help="this machine's readings from the snapshots")
    p.add_argument("--days", type=int, default=HORIZON_DAYS)
    p.add_argument("--pool", default=POOL, help="scoped pool to carry (default %s)" % POOL)
    p.add_argument("--out", default="points.json")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("prepare", help="this machine's file, or why not to bother")
    p.add_argument("--out-dir", help="this session's scratchpad (required to publish)")
    p.add_argument("--force", action="store_true", help="update even if nothing moved")
    p.add_argument("--merge", metavar="FILE",
                   help="this machine's file as the chart holds it, or 'none' (new install)")
    p.add_argument("--index", metavar="FILE",
                   help="the chart's data/index.json as read, or 'new' (joining the index)")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("pushed", help="record that the publish landed")
    p.add_argument("--ok", action="store_true", required=True)
    p.set_defaults(fn=cmd_pushed)

    p = sub.add_parser("state", help="one JSON line for the .chart dot-command")
    p.set_defaults(fn=cmd_state)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
