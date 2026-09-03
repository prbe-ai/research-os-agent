---
name: notes-audit
description: Periodic team-note audit. Load this in a BACKGROUND agent when session start reports the audit due (or the researcher asks for one) — never inline with the user's work. Claim the stamp, strike claims the evidence contradicts, remove only expired strikes and lapsed expiries, shrink only when the size condition fired.
---

# Team-note audit

The team note is injected into every session on every machine, so a stale claim
there taxes the whole team and an oversized note stops arriving at all (over
budget it degrades to a pointer). This audit keeps it TRUE first and SMALL only
when needed. As a spawned background agent, nothing you do may interrupt the
session doing the user's actual work; running inline (Codex, whose sandbox
reaps detached processes), finish this audit before taking up the user's
request — it is one small file.

Work on the team-note FILE at its synced path (`~/.claude/probe-team-note.md`;
`~/.codex/probe-team-note.md` on Codex; `~/.pi/agent/probe-team-note.md` on pi).
Never edit the rendered block inside CLAUDE.md / AGENTS.md — it is overwritten
on every sync and reaches nobody. Every edit is versioned server-side, so a
wrong strike or removal is recoverable; that is the safety net this procedure
leans on. Do not create Probe entities, runs, or notes elsewhere: this skill
touches exactly one file.

## 1. Claim, or abort

Read the file fresh NOW (not from your spawn context). Its audit stamp is a
dated comment near the top, in exactly this shape:

    <!-- audited 2026-09-03 -->

If the stamp is today's or yesterday's date, another session already claimed
this cycle — STOP, change nothing. Otherwise claim it: set the stamp to today's
date (add the comment as the first line if it is missing), save, and continue.
The file syncs itself; writing the stamp first means a crashed audit costs one
skipped cycle, never a double audit.

## 2. Truth pass — strike, never delete

Walk the note claim by claim and check each against what you can actually
verify: Probe reads (runs, experiments, statuses), the repositories on this
machine, dates that have come and gone. When the evidence contradicts a claim,
strike it in place with the standard marker — a blockquote whose first line
carries `**SUPERSEDED**`, the old claim struck inside it, the correction after:

    > **SUPERSEDED** 2026-09-03 · superseded by the vNext pipeline
    > ~~Retrieval latency is capped by the per-source scan.~~

    The scan was removed in #1234; latency now tracks the gatherer stall-cut.

Judgment only ever STRIKES. Deleting unstruck text, rewording someone's claim,
or "tidying" prose you merely dislike is not auditing — a claim you cannot
verify either way (customer facts, legal status, incident history, decisions)
is left exactly as written. Struck regions vanish from the rendered context
automatically, so a strike is already a win; it does not need to become a
deletion today.

## 3. Removal — only the provably dead

Exactly two categories may be deleted outright, nothing else:

- **Expired strikes:** a superseded region whose marker date is older than the
  removal horizon the trigger line stated (default 7 days; horizon 0 means
  remove nothing). The strike sat visible that long and nobody objected;
  version history keeps the bytes.
- **Lapsed expiries:** entries that declare their own end and are past it —
  "expires 2026-09-07", "temp cap until Oct 1" after that date. A date alone is
  not an expiry: incident dates, deadlines met, and historical results stay.

## 4. Shrink — only when the trigger said size

Skip this section entirely unless the trigger line named the render budget.
When it did: compress shipped work to one line plus its PR number, merge
sections that say the same thing, and keep what will bite the next session —
detail that lives in a PR, the repo, or version history gets a pointer, not a
copy. Edit the file directly (read it immediately before writing; preserve
sections you are not compressing).

## 5. Close

Re-read the file once to confirm your edits and the stamp survived the sync
merge, then report in one short paragraph: claims struck (with reasons),
regions removed, whether you shrank, and the note's rough size. If you changed
nothing, say so — "audited, nothing stale" is a real result.
