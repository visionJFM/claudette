---
name: chart
description: One chart of how much of the Claude 5-hour and 7-day allowance every machine has spent, merged across all of them. Use when the user asks how full their week is, wants the usage chart refreshed or opened, adds a machine, or asks where the chart is. Each machine publishes its own readings as one file of the chart's artifact, so no machine ever overwrites another's.
---

# chart

The window percentages exist **only** in the status-line payload Claude Code hands
claudette; no telemetry carries them (checked against the OTLP export: eight metrics,
not one quota). So claudette's own render snapshots are the only record — per machine,
full of holes where you were working somewhere else. This skill merges them.

**Zero-token first**: `.chart` prints the link and whether this machine is behind,
answered by the hook (`..chart` for the phone). Only a *refresh* needs this skill.

## How the store works, in one paragraph

The published artifact is a ~1.5 KB loader page (`index.html`, whose `chart-index` names
the renderer version), the renderer as three published files (`chart.css`, `view.js`,
`page.js`), a machine index (`data/index.json`, the machines that have a file), and **one
published file per machine**, `data/<stem>.json` — `{v, m, tz, updated, docs:[{m, w, tz,
rows[[ts, acct, fable|null, h5|null, h5_resets_at|null]]}]}`, every window that machine
knows. Only that machine ever writes its file, and a publish keeps every file it does not
name, so two machines never collide and nothing is ever deleted. The page fetches the index
and the files when it opens and unions them with the artifact's **database**, now a
read-only archive: the flow before this one wrote there, and it asked for approval every
session (`DESIGN.md` — design notes, not published — "The store is published files"). **Publishing asks for no approval.**
It needs a plain read of the live page first — cheap, because the page is tiny. Every
publish sends this machine's own loader and renderer with its file: identical bytes from
every machine on the same version, never a rollback because of the version check below.

## Refresh (the verb this skill exists for)

The prompt hook puts `[claudette chart] url=… own=data/<machine>.json` in context. **First
message**, four independent calls — one round trip:

```bash
C=~/.claude/skills/claudette/scripts/usage_chart.py      # or: run.sh usage_chart.py …
python $C prepare --force --out-dir "<this session's scratchpad>/chart"
```

plus, all on that url: `Artifact action:"read"` (plain, no `path` — only that satisfies the
read a publish requires), `Artifact action:"list" scope:"files"`, and `Artifact action:"read"`
with `path` = the `own` file — it lets the publish through when another session on this
machine published that file since this one last did, and errors harmlessly when the machine
has no file yet. (No url in context → `python $C addr` first; exit 3 → see "Where the
address comes from".)

- `publish` null with `skipped` → say the reason and stop.
- `publish` set → **one** `Artifact` publish of exactly its `url`, `file_path` and `files`.
  No `capabilities` (the page keeps its `db` for the archive), no `icon`. **Unless** the
  page you read carries a `chart-index` `renderer` newer than prepare's: publish nothing,
  and say this machine's claudette is older than the chart's and should be updated.
- `publish` null with `needs` (a machine joining the index, or an install that lost its
  local copy — once each): `Artifact read` with `path` each file `next` names that the
  listing shows (the `own` file is already read), rerun `prepare --force` with the flags
  `next` gives (`--index <saved>|new`, `--merge <saved>|none`), and publish that plan.
  Skipping the merge would overwrite the machine's history with what an empty install has;
  skipping the index publishes a file the page never fetches.
- A refused publish that names only the `own` file: read it again by `path` and publish the
  same plan once more, without merging — `prepare` built it from everything this machine
  recorded. A refusal naming any other file: plain `read` of the page again and redo the
  renderer check before that one retry. Refused twice → report and stop; never `force`.

Then `python $C pushed --ok` **and** `Artifact action:"open"` on the url, in ONE message —
a person who typed the verb gets the page in front of them. Report one line: which windows
the file carries, how many readings, and the link. **Never say whether an approval prompt
appeared** — a tool result reads the same either way, and you cannot see the prompt.

**Only this verb opens.** A chart update that rides a `claudette:done` never opens the page.

- **`--force` is for a person who typed the verb.** Left alone, `prepare` updates only
  when something the chart draws has moved (a new 5-hour window or an unreported week
  counts), which is what keeps an automatic update from firing on a session that changed
  nothing.
- **First run on a machine** stats the whole snapshot folder (~2 s warm, up to ~100 s
  cold) and caches its read-through mtime in `logs/usage_chart_cache.json`.
- The file is built from the local copy (`logs/usage_chart_mine.json`) **plus** the fresh
  snapshots, so a snapshot folder that lost something never shrinks what the chart shows.

## Changing the renderer

Edit `assets/index.html`, `chart.css`, `view.js` or `page.js`, bump `RENDERER` in
`usage_chart.py`, add its hash to `RENDERER_HASHES` in `tests/test_usage_chart_files.py` (tests, not published)
(`--hash` prints it), and run the suite — it runs both node tests on a real bundle, because
the chart renders only inside claude.ai and a broken renderer would publish a blank card.
The next typed refresh or `done` from a machine carrying it upgrades the page; a machine
with an older copy publishes nothing until it is updated.

## Where the address comes from, and the one rule about it

`usage_chart.py addr` answers from `logs/usage_chart_addr.json` — machine-local, because
there is no synced per-user store anywhere in claudette and a shared repo must never
carry one user's artifact address. Exit 3 means nothing is cached, and then **the
account is the sync channel**:

1. `Artifact action:"list" scope:"mine"`. The listing gives title, url and updated-at and
   **nothing else** — so the title is what narrows the list to the candidates worth a
   probe, never the test. A title mismatch is worth saying out loud; it is not a reason to
   reject a match or to create a chart.
2. Probe each candidate: `ArtifactData action:"get"`, `collection:"meta"`,
   `doc_id:"identity"` — the chart is the one whose document carries the marker
   (`claudette:usage-chart:v1`, frozen; `python $C addr` prints the whole probe under
   `identity`). A candidate that answers **not-found, or errors because it has no `db`
   capability, is a clean non-match** — move on, that is not a reason to stop.
3. `python $C addr --set <url>`.

**Never create a second chart.** If the listing shows no match, say so and ask: two
charts split the history in two, and that is the one failure this design cannot heal.
An automatic run (auto-done) must never create one — only append.

**A user may own a personal chart of their own** with the same purpose and a different
design. That is exactly what the marker is for: a chart without it is not this chart,
however close its name looks. Two or more artifacts carrying the marker should never
happen; if a listing shows that, stop and ask.

## First chart on this account (`init`)

Only with the user's go, and only after a listing showed no match:

```bash
python $C prepare --force --out-dir $W --index new --merge none
```

Publish its `file_path` and `files` with `title` = what `addr` prints, a one-sentence
`description`, and `capabilities: {"db": {}}` — the identity document lives in the
database, and it is what every other machine finds the chart by. Then `addr --set` the new
url, `pushed --ok`, write the identity document — `ArtifactData action:"set"`,
`collection:"meta"`, `doc_id:"identity"`, `data:{"marker": "claudette:usage-chart:v1"}` —
and offer to pin it. That one database write asks for approval; it happens once in the
chart's life.

## Adding a machine

Run the refresh there. It finds the chart from the account, joins the index, publishes its
own file, and appears in the sources table. No credential, no pref, no address to copy.

## What the page shows

Tab (which week) then chart at the top; numbers, tables and prose below. One window at
full width, the previous ones behind tabs, oldest to newest, opening on This Week. Under the main line, each 5-hour window as a rising step: it climbs from its own
opening (a hairline) and then **holds — never falls**; the next hairline is the restart
from zero. A reading below the one before it inside the same window is a stale cache on
some machine, not a drop, so the step plots the running maximum while the raw reading
stays in the file and in the tooltip. Blue is the whole account's 7-day window,
amber the model pool's own allowance, red dashed the claudette throttle
(`100 − 8 × days left`, red because it is a limit), green the 5-hour steps with a darker
green hairline where each one opens. Under both, **one lane per machine**: a bar wherever
that machine had readings less than an hour apart, its name above the rightmost end of its
data, on a thin line of the same colour wherever that machine's logs cover the window — a
gap on the line is known idle time, a gap off it is no data. Presence, not spend — and the only place two machines working the same hour both
show, since the lines above keep one reading per 5-minute bucket.

## Automatic pushes

`chart_autoupdate` (a claudette feature, **off** by default, `/claudette prefs`):
when on, a `/claudette:done` **the user typed** updates the chart with this machine's
allowance percentages. Describe it to the user in those terms: what reaches the artifact is
the percentages their own status line already shows, the machine's name and the reading
timestamps, and **"sending data from your machine" is the wrong sentence for that** (owner,
2026-09-21).

**The idle auto-done run publishes too** (2026-09-23). It never discovers or creates a
chart. Why this flipped twice, and the risks the owner accepted: `DESIGN.md`, "Unattended
updates, again".

**Neither of them publishes from the conversation** (2026-10-19): the done recipe runs
`chart_runner.py launch`, which hands the publish to a short-lived background Claude session
and returns at once, so the report shows one line and nothing else. Only this skill's verb
still publishes in-session, because a person typed it and gets the page opened.
`chart_runner.py status` shows the last background update. Why and how: `DESIGN.md`, "The
chart runner".

## Reference

`DESIGN.md` — why the store is published files and not the database, why one file per
machine, what the two node tests guarantee, how the address is discovered. Read it before
changing the file shape, the merge rules or the retention.
