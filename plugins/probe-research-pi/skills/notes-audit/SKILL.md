---
name: notes-audit
description: Periodic team-note audit, and the canonical note-cleanup METHOD every other surface points at. Load this in a BACKGROUND agent when session start reports the audit due (or the researcher asks for one) — never inline with the user's work. Claim the stamp, delete what the evidence disproves, tighten what stays.
---

# Note cleanup

A note that lies costs more than a note that is long, and a note that outgrows
its budget stops arriving at all. Sections 1-4 are THE METHOD, and they are
carrier-neutral: the team note, a project's note, an experiment's, a run's. The
team-note lane starts at §5.

Every edit is versioned server-side and recoverable. That is the safety net this
procedure leans on — not a licence to be careless, because nobody reads version
history, so a wrong deletion is invisible in practice.

## 1. Read the whole document first

Read the CURRENT full document, fresh, immediately before your first edit — not
from your spawn context, and never from an excerpt, a pinned version or a
summary. An advisory that arrives beside a note tells you its version and its
fullness; it is not the note. Editing from anything but the stored bytes targets
text the document does not contain.

If you cannot read it in full, change nothing and say so.

## 2. Delete what the evidence disproves

Walk the document claim by claim. Check each against what you can actually
verify from here: the repositories on this machine, the live clusters, Probe
reads, files the note points at, dates that have come and gone.

When the evidence contradicts a claim, DELETE it and put what is true in its
place. No strike-through, no `SUPERSEDED` marker, no note about the edit.
Whether a claim is stale is your judgement, made on the evidence and context you
actually hold — there is no checklist that decides it for you.

DELETE, outright and completely, every existing `> **SUPERSEDED**` blockquote
region you find, marker line and struck lines together. Those are already-
retracted claims; this lane does not keep them.

ONE EXCEPTION, and it is the guard below applied to a marker: a region that
records a DECISION someone made and then reversed. The retracted claim is dead,
but "we chose this, then un-chose it, and here is why" is not a stale claim --
it is the thing that stops the same proposal coming back. Recognise it by the
prose around it arguing for its own preservation, or by the struck line being a
numbered decision rather than a fact. Leave those exactly as they are; a note
that says "keep this reversal" has already told you the answer.

**A completed instruction is not a dead section.** When a note says "X is broken,
so always do Y" and X is now fixed, the instruction is spent but the reason it
existed usually is not. Delete the part the evidence disproves and keep the
mechanism -- the same rule §3 applies when it tightens. Deleting the whole
passage throws away why anyone cared.

An entry that declares its own end and is past it is disproved by the calendar:
"expires 2026-09-07", "temp cap until Oct 1". Delete it. A bare date is NOT an
expiry — incident dates, deadlines met and historical results stay.

Two things stay untouched. A claim you cannot verify either way — customer and
commercial facts, legal status, incident history, decisions someone made — is
left exactly as written; "disproved" means checked and false, never unfamiliar.
And you may compress or reword, but never change what someone asserted.

**If the line that sent you here says deletion is disabled, delete NOTHING in
this section.**
Correct a disproved claim by editing it to say what is true, leave every
superseded region where it is, and say in your report what you would have
removed. That switch exists so the destructive half can be stopped in production
without waiting for a plugin release; an audit that deletes anyway makes it a
lie.

## 3. Tighten what stays

This is why you were dispatched: on the team note an audit is triggered by SIZE
and nothing else. On an entity note, tighten when the advisory beside it reports
the document filling up.

Shipped work becomes one line plus its PR number. Sections that say the same
thing merge. Detail that lives in a PR, a repo file, a dashboard or version
history gets a pointer, not a copy. Verbose retelling of a trap loses the
retelling and keeps the mechanism.

What survives: every trap's mechanism and trigger, every "never do X" with its
reason, and exact identifiers — paths, PR numbers, commands, config keys,
version numbers, cluster names, URLs. Those are what people copy.

Do not remove more than about a third of the document's BYTES in one pass. If it
needs more, cut what you confidently can and say so.

## 4. How you edit

Exact-span replacement, always: match the stored text EXACTLY and UNIQUELY, then
replace or delete that span. Never read-modify-rewrite a whole document to
change part of it — that is how the parts you did not think to repeat disappear.
Widen an ambiguous span with surrounding context until it is unique.

On an entity note that is `probe notes edit --old <span> [--new <text>]`; an
omitted `--new` deletes. Use `@file` or `-` for a multi-line span. **A 409 is not
a failure, it is the merge affordance**: it carries `match_count` and
`current_notes`, the whole current document. Someone else wrote while you were
thinking. Re-derive your edit against the body the 409 handed you and retry — do
not re-read, and do not give up on the edit.

On a FILE (the team note), use an editor that enforces the same uniqueness. Never
`sed -i` a shared, syncing file.

Write nothing about the audit into the document. No changelog, no "audited"
summary, no receipt. If a previous pass left one, delete it — it is not content.

---

## 5. The team-note lane

The team note is injected into every session on every machine, so a stale claim
there taxes the whole team. Work on the FILE at its synced path
(`~/.local/state/probe/team-note/probe-team-note.md` — ONE per machine, shared by
every agent on it). Never edit the rendered block inside `CLAUDE.md` /
`AGENTS.md`: it is overwritten on every sync and reaches nobody. Touch exactly
that one file — no Probe entities, runs or notes elsewhere.

As a spawned background agent, nothing you do may interrupt the session doing the
user's real work. Running inline (Codex, whose sandbox reaps detached
processes), finish this audit before taking up the user's request — it is one
small file.

### Claim the cycle

The stamp is a dated comment on the first line, in exactly this shape:

    <!-- audited 2026-09-03 -->

If it is TODAY's date, another session already took this cycle — STOP, change
nothing. Otherwise set it to today's date (adding the line if missing), save,
and continue. Writing the stamp first means a crashed audit costs one skipped
cycle, never a double audit.

TODAY'S ONLY, and the exact boundary matters: the line that dispatches you stays
silent on a note stamped today and fires from a stamp one day old. This guard
was once a day wider than that, so a note stamped the previous day dispatched an
auditor that then read this section and refused to work. Found by running a real
audit, which stopped and asked instead. The two must agree, and it is the
dispatcher that holds the rest of the picture.

The stamp is a RATE LIMIT, not a schedule. Nothing here runs on a calendar: an
audit is dispatched because the note has outgrown its render budget, and for no
other reason. A note that has gone stale without growing is corrected by whoever
next reads a claim their evidence contradicts — this note is injected into every
session on every machine, so that is a great many readers.

This is a best-effort cadence, not a lock: two sessions can read the same expired
stamp before either writes, and separate machines hold separate copies. That is
survivable — `probe notes sync` merges concurrent work the way git does — but it
means you may occasionally be the second auditor of a cycle. Nothing about that
is an error.

### Size

Over budget the render emits a POINTER instead of the note, so an oversized note
reaches NOBODY. `~/.local/state/probe/team-note/health.json` carries the real
per-surface numbers — read that file, never a remembered figure.

Two things about the arithmetic that are easy to get backwards. The render drops
each superseded region's struck lines but KEEPS its marker line, so deleting a
whole region frees more render bytes than the struck text alone. And the block
renders a few hundred bytes larger than the prose it carries. Aim to land about
10% under `available_bytes` so the next few appends do not immediately re-break
it.

### Close

Re-read the file to confirm your edits and the stamp survived the sync merge.
Report in one short paragraph: what you deleted and the evidence for each, what
you tightened, what you checked and kept, what you deliberately left alone, and
the note's size before and after. "Audited, nothing stale" is a real result.
