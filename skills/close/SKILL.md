---
name: close
description: Close this session for good - mark it finished, so `.sess` shows it closed (✓) and never offers to reopen it, then end the claude process and the console window or tab it runs in. Works from the phone or the desktop app too, where typing quit or exit leaves no trace. Faster, with no agent turn: type `.close` (when claudette's dot-commands are on). Use only when the user invokes /claudette:close; never on your own.
disable-model-invocation: true
---

# /claudette:close

The user is done with this session. Invoking this command already wrote the closing trace
(the command itself is filed in the transcript), so do nothing else first: no summary, no
done report, no question.

Run exactly this, as your only tool call:

```
bash "$HOME/.claude/skills/claudette/scripts/run.sh" close.py
```

Then write ONE short line in the user's language and stop:
- output starts with `ENDING:` → say the session is marked finished and closes in a few
  seconds (with its console, when the output says so).
- output starts with `NOT ENDED:` → say it is marked finished but stays open, and why, in
  the output's words; they close it by hand. Quote the `(chain: ...)` line as is: it is
  what diagnoses a refusal on another machine.
- anything else (an error) → say it is marked finished, quote the error, and that they
  close it by hand.

When the output has a `TIP:` line, end your line with it, translated: it is printed only
where the free `.close` would have worked.

Why the process ends a few seconds later and not now: CLAUDETTE_DESIGN.md (design notes, not
published), "Reopening after
a reboot".
