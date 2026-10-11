---
name: audit-team-note
description: Periodic team note audit - should be used in conjunction with edit-notes skill
---
The team note is injected into every session on every machine, so a stale claim taxes the whole team, and a note past its render budget reaches NOBODY at all.

**The mechanism to edit the note is in the `edit-notes` skill.** - make sure to
use it.

**FOR THIS AUDIT, ALWAYS USE A SUBAGENT OR SOME ASYNC MECHANISM IF POSSIBLE** -
we do not want this audit to be inline with the user's main work.

## Triggers & Instructions

THIS SKILL SHOULD ONLY BE TRIGGERED BY A USER-interactable SESSION, not a
headless session.

- **Over its render budget** - the truth pass AND the tightening.
- **Overdue but inside its budget** - overdue means nobody has audited it in a
    week (`PROBE_NOTES_AUDIT_INTERVAL_DAYS`, 7 by default; 0 turns the weekly
    half off). The truth pass only. Leave the length alone; compacting a note
    nobody is struggling to read is what the split exists to avoid. A note with
    no stamp at all, or a malformed or future-dated one, counts as overdue.
- **No dispatch line** (someone asked you directly) - the truth pass only,
    unless the health file puts the block at or over 80% of its budget. Guessing
    "both" is the expensive wrong answer.

If deletion is disabled, `edit-notes` says what to do.

## Local File Copy

Get the team note document path from `probe notes sync`: its JSON `document`
field IS the file, and the same call reconciles with the server before you touch
anything. There is ONE local copy per machine, shared by every agent on it.
Never edit the rendered block inside `CLAUDE.md` / `AGENTS.md`: it is
overwritten on every sync and reaches nobody.

## Note Claiming

The stamp is a dated comment on the first line, exactly:

    <!-- audited 2026-09-03 -->

If it is TODAY's date, another session took this cycle - STOP. Otherwise set it
to today (use `date -I` - adding the line if missing), save, and continue
writing the rest of the note.

NOTE: Best-effort, not a lock - two sessions can read the same expired stamp
before either writes, and separate machines hold separate copies. `probe notes
sync` merges concurrent work the way git does.

## Size

Over budget the render emits a POINTER instead of the note, so an oversized note
reaches NOBODY. `health.json`, beside the document in that same directory,
carries the real per-surface numbers - read that file, never a remembered
figure.

Two things are easy to get backwards:

- A superseded region's struck lines are ALREADY dropped from the block; only
  its marker line still costs bytes. Deleting the whole region frees more than
  deleting the struck text.
- `block_bytes` measures the RENDERED block, not your prose. Judge your cuts
  against that number, not against the length of the file.

Land about 10% under `available_bytes` - it differs per harness - so the next
few appends do not re-break it.

## Close

Re-read the file to confirm your edits and the stamp survived the sync merge.
Report: what you deleted and the evidence, what you tightened, what you checked
and kept, what you left alone, and the size before and after. If no changes made
(nothing stale for ex), that is an ok result.
