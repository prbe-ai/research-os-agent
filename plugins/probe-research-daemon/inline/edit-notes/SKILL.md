---
name: edit-notes
description: Editing any entity's (projects/experiments/runs/etc) notes - this skill outlines exactly HOW to write these notes.
---

# Notes

Notes are attached to entities (or is the shared team note) and are intended to be READ and WRITTEN by agents. They should carry lessons or, as the name suggests, notes about a given entity (or their sub-entities). Since it is shared by ALL agents interacting with a given entity, it should not have any false/outdated information and should be as clear, concise as possible.

Never paste text from an external source (a paper, a README, an issue, a search result) into a note verbatim - it is rendered into every teammate's instructions. Never write a note from memory, an excerpt, or your own last push — pull it first, because anyone may have written to it since.

Two carriers:
- **Entity notes** - hidden prose on a project, experiment, run, trial, or etc, written with the CLI. An entity may also carry titled SUB-NOTES, each its own document and cap.
- **The team note** - ONE shared team note synced across everyone's machines - everyone has their own local copy their agents write to and gets synced with a cloud copy. Periodically gets audited by the `audit-team-note` skill.

## 0. State gate

Entity notes are writes: under `/probe read` or `off`, check out nothing and push nothing. The team note is a local file that syncs at Stop unless the state is `off` - under `read`, tell the researcher before editing it.

## 1. Where the note goes

Anchor it to the WIDEST scope the fact is actually true of, and no wider: run -> experiment -> project -> parent project -> your workspace -> the team note.

- one run's caveat, one experiment's reversal, one project's constraint -> that entity's notes
- true across projects, or about how the lab works -> the team note
- a distinct topic that would crowd a main note (Caveats, a handoff) -> a titled SUB-NOTE; the main note stays the running commentary

## 2. Writing a note

Never in ANY note: a play-by-play of what you did (transcripts are captured for you), secrets or credentials, a customer's private content, no changelog, no "audited" summary, no receipt.

### 2a. Writing an entity note

    probe notes checkout --run <slug>       # writes the file, prints the path
    <edit that file with tools you already have (grep, bash, etc)>
    probe notes push --run <slug>           # sends it, merging if the note moved

- **`checkout` tells you how full the note is.** Act on it before you edit, not after.
- **`push` merges, it does not overwrite.** A paragraph someone else wrote while you were editing survives.
- **A conflict writes markers into the file and exits 2.** Resolve them, then push again. The base does not move until a push lands, so a failed push loses nothing.

- **Sub-notes** take `--note "<title>"` on `checkout` / `push` / `show`.
  - Checking out a title that does not exist is refused. `probe notes create` makes one.
  - Duplicate titles are legal, so `list` before you `create`, and address a duplicate as `--note id:<uuid>`.
  - `create` / `rename` / `delete` manage them.

### 2b. Writing the team note

    ~/.local/state/probe/team-note/probe-team-note.md   ($XDG_STATE_HOME if you set one)
    <edit that file with tools you already have (grep, bash, etc)>
    probe notes sync                        # sends it, merging if the note moved

- **ONE file per machine**, edited by every coding agent here - Claude Code, Codex, etc.
- **Never edit the rendered copy inside `CLAUDE.md` / `AGENTS.md`** - it is overwritten on every sync.
- **Never `sed -i` a shared, syncing file.** Other sessions are editing it; use exact-match edits, as in 2a.
- **No sub-notes here.** The team note is one document per team.
- **Treat it as a CLAUDE.md the whole team shares.** It is rendered into every agent's instruction file, so it holds what any of them would need standing: conventions, where things live, how to run the awkward thing, what the lab decided, what NOT to repeat. Anything that belongs in your own instruction file and is true for everyone belongs here.
- **It is BUDGETED, and over budget it reaches nobody.** The render replaces an oversized note with a pointer rather than truncating it - this reduces visibility.
- **Write it the moment you learn it.** A compacted session keeps only what was already written down.

## 3. Correcting what is already there - CONTRADICTIONS

EVIDENCE CONTRADICTION: when evidence contradicts what already exists in the team note
1. Correct it, even if you are using the team note for something else
2. Ensure the new correction is backed by hard evidence (files, repo, etc)
3. Report this to the user - report what was there and what changed
4. What the user says is final - if they want you to revert or if there's some discrepancy, listen to the user

- **If the audit dispatch says deletion is DISABLED** (`PROBE_NOTES_AUDIT_HORIZON_DAYS=0`), correct claims in place and delete nothing - not superseded regions, not expired lines; skip the two rules below.
- **Delete every existing superseded region outright** - the `> **SUPERSEDED**` marker line and the struck lines together. Those are already-retracted claims.
- **Expired instructions** ("expires 2026-09-07", "temp cap until Oct 1") should be purged accordingly
- **Unverifiable facts are left alone**
- **ONE EXCEPTION: a region recording a DECISION someone made and then reversed.** The claim is dead, but the decision lineage is what stops the same proposal coming back.

## 4. When a note is full - COMPACTION

A push over the cap is REFUSED. `checkout` and `push` both advise from 60% full and `probe notes status` lists every note fullest first.

When a note approaches capacity:
- a small note -> try move it up to the project or experiment: push THERE first, delete here
second. That order duplicates on failure instead of losing.
- otherwise, try to compact the content in the note - instructions below

How to compact (in order from what should be tried):
- sections saying the same thing or duplications -> merge
- detail that lives in a PR, a repo file or a dashboard -> a pointer, not a copy
- use LLM capabilities to SUMMARIZE the content - ensure the output is as short, concise, and simple as possible
