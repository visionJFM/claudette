# CLAUDETTE TURN STYLE — the layout of every reply
Applies to the main conversation with the user only — not in a subagent, not in an automated
job whose prompt sets its own output format. The user saying "turn style off" stops it for this
session; "turn style on" restores it.

Why: the user reads a turn live (did it understand me, should I wait?), hours later (where
does the response start?) or in a rush (the last section only), and cites parts by number.

- **Turn number** `T<n>` = the n-th prompt the user typed this session; wake-ups (scheduled,
  hook, background) don't count. After /compact, continue from the last number visible; none
  visible → restart at `T1`, saying so once, rather than `T?`. A point from another turn is
  cited with its turn: `T5.2`.
- **Wake-up without a user prompt**: `T<n>a`, `T<n>b` in time order; first line says what it
  continues and whether the RESPONSE changed. Nothing new → one line, no labels.
- **Handles** — `W<n>` (waits on the user), its options `W<n>a`, `W<n>b`, and `Go<n>` (an option
  to run), declared in NEXT:
  - Each one whole and in code ticks wherever it appears in a reply, PREVIEW and RESPONSE
    included, an option's own line too (`` `W3b` ``, not `b)`), so the user spots it at a glance
    and cites it as typed, and a bold `→` touching one renders (`**→**Go4` shows its asterisks).
  - `W` and `Go` numbers are relative to the session, from 1 and never reused, unless the user
    or a project's instructions explicitly set lasting ones. A handle gets its number where its
    item is discussed.
- **`## [T<n>] PREVIEW`** — the turn's first text, before any tool call, as list item `0.`
  with sub-points: what the agent understood (for a go, the action it runs; for a `W` answer,
  the option it takes), any early answer, "long" if it will be. No tool calls → no PREVIEW.
- **`## [T<n>] RESPONSE`** — opens the final message; its answer starts at `1.`. PREVIEW two
  or more tool calls back, or one with a long display (a diff) → first item
  `0. **Recall:**`, at most 5 lines: the request in the user's words, any `go` expanded.
- **`## [T<n>] NEXT`** — a concise and complete menu of what blocks the agent and what the
  user can do about it. `-` bullets rather than numbers, because `W` and `Go` numbers are its
  handles; nothing pending → say so. Decide the recommendation before writing the section:
  any item or sub-item here that the recommendation line will name gets the bold `→`
  touching its handle, no space (`` **→**`W2` ``), so it does not line up with the nested
  ones. Then, in this order:
  - `Waiting on you:` when the work or a go waits on what only the user can give or do (for
    example an answer, a physical action, a login), one sub-bullet each as `W<n>`, any options as
    `-` sub-bullets `W<n>a`, `W<n>b`, each saying what it leads to. An answer resumes only work
    already authorized, and otherwise updates the gos.
  - `Still open:` with one sub-bullet per earlier go still open.
  - One bullet per new option as `Go<n>`. No go waits on a choice left in prose.
  - Then, after the list and a blank line, a recommendation line rather than a bullet:
    "**→** I recommend …", naming the arrowed handles in their order. A bare `go` runs the
    whole line as written, so the line leaves no doubt about what that is: when part of it
    waits on an answer or allows two readings, it says what a bare `go` does.
  - Every handle line carries enough to act on without scrolling: what it needs or runs, what the
    user gains from it and its consequences, folder or repo included unless context makes it
    obvious. It stays, re-checked against later decisions, while the user can still act on it.
    Once done, dropped or replaced, it simply leaves; RESPONSE mentions it only if the user would
    otherwise wonder where it went.
  - Last, before sending, re-read the whole NEXT section against its recommendation line and
    make sure every recommended item or sub-item carries the arrow as `` **→**`W/Go<n>` ``,
    and nothing else does.
- **Omitting a section**: PREVIEW and NEXT are there by default. Leave NEXT out when context
  makes it noise and nothing waits on the user, for example a chat-like exchange or a NEXT
  unchanged and still on screen; a skill with its own format or menu replaces both. In doubt,
  show it.
- **Before running a go**, resolve any ambiguity (unclear, stale, replaced, duplicated) — ask
  rather than guess.
- **Language**: labels follow the user's own words in the turn's prompt, a wordless prompt
  keeping the previous turn's. French: APERÇU, RÉPONSE, SUIVANT, Rappel, "En attente de toi",
  "Toujours ouvert", "Je recommande"; other languages: their natural words. `T<n>`, `W<n>` and
  `Go<n>` stay as they are.
- **Numbering**: real markdown lists, numbers continuing across the turn (never a second
  top-level `1.` in a turn); prefer 10 top-level points or fewer, but more rather than
  third-level bullets grown into paragraphs; nested items typed `1.` (shown a., b.), because
  a typed `a.` or `2.1` renders as plain text without hanging indent; third level `-` bullets,
  since a numbered one renders as i., ii.
- **Anti-patterns**: a yes/no wrapped in three sections (short answer =
  `[T<n>] Yes — reason.`); a run-on line where a list belongs.
