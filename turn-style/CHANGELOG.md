# Claudette turn style — changelog

One line per published version of `STYLE.md`, newest first. `/claudette update` shows the
lines newer than your copy before offering a version. Format, read by `scripts/user_copy.py`:
`- v<N> · <first 12 hex of the text's sha256> · <date> — <what changed and why>`.

- v3 · f5f7cd62782a · 2026-11-09 — After /compact with no turn number visible, restart at `T1` and say so once, rather than heading replies `T?` (seen for 28 turns after an auto-compaction whose summary kept no number).
- v2 · feac521932d6 · 2026-10-19 — Handles always in code ticks, options written whole as `W<n>a`; NEXT opens with its goal, its arrows are decided before it is written and re-checked before sending; each handle line says what the user gains from it and its consequences; a done or dropped handle simply leaves; numbers relative to the session; `T5.2` cites a point of another turn.
- v1 · 774c7c396648 · 2026-10-06 — First published version: PREVIEW / RESPONSE / NEXT, turn numbers, `W<n>` and `Go<n>` handles, a bold `→` on what a bare `go` runs — the whole recommend line.
