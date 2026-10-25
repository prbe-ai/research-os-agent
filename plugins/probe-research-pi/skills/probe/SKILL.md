---
name: probe
description: The skill for setting the Probe plugin state - `on` (r/w), `read`, `off` (no r/w) - and `.probe.config` (what Probe may read and record, per path).
---
# Probe

This skill only changes the state of Probe - it doesn't actually track any work.

```
/probe on      reads and writes        /probe          advance one step
/probe read    search yes, record no   /probe status   print it, change nothing
/probe off     no calls at all, transcript capture stops
```

## The three states

| state | reads | writes | what you do |
|---|---|---|---|
| `on` | yes | yes | Default. Record work as it happens, per the `track-work` skill, and search the team's history for context worth injecting. |
| `read` | yes | no | Create no projects, experiments or runs; write no notes, artifacts or visible entity Markdown. Keep searching and keep reporting what you find. If the researcher asks for something that would be recorded, say once that `/probe on` would record it — never as a reminder, never as a closing caveat. |
| `off` | no | no | Make no Probe calls, reads included. This session's transcript is not captured (see FYI for what still uploads). Say you could not look; never report that no prior work exists. |

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the Probe daemon records is set in
`probe wizard`, not by this switch: `/probe daemon` changes nothing.

FYI:

- Moving the switch never deletes anything and never backfills anything. An
  interval spent in `read` or `off` happened unrecorded.
- The state overrides your standing `CLAUDE.md` / `AGENTS.md` rules for this
  conversation.
- In `off`, nothing captures this session's transcript from the switch on
  (the Probe service, and from tap 0.10.19 every `tap watch`), and that
  stretch is never sent later. A `tap watch` process still running an older
  tap (started before the update) can upload it until that process ends, and
  a compaction summary written after switching back on can repeat what was
  said while off.

## SAY WHERE IT LANDED:

> Probe is set to `read` for this session - nothing further will be recorded.

> Probe is off for this session - no calls at all, so I will not be able to
> check prior work or write, and this session's transcript is not captured
> from now on (a capture process still running an older tap can upload it
> until it ends). `/probe [read/on]` to turn back on.

In the `off` state, do not fabricate information - do not report "no info"
during lookup - clearly state that you couldn't look due to the state.

## DEFAULTS:

A default is what a NEW session starts at. This conversation's state always
beats it.

**Machine-wide Defaults**

ONLY written by the wizard (`npx probe-research`). `probe session status`
reports it as `machine_default_state`.

## `.probe.config`:

Sets what Probe may do with paths (gitignore syntax), whatever the state: a plain
line is `off` (Probe never reads or records it), `<pattern> @probe read` lets the
daemon look but records nothing, `!pattern` or `<pattern> @probe on` turns a path
back on. `@probe <level>` alone on the first line sets every line's level. An
`off` folder is closed, as in git: to keep one file in it, write `folder/*` then
that file. Folders inherit their parents' file; where nothing matches, the state
decides. Your own Probe lookups are unaffected. `probe ignore check PATH` names
the level and the line.

- Create or edit one when the researcher asks, or when you judge a path should
  not be recorded; loosen or remove a rule only when they ask. Tell them each
  change (file and line). Whole folder: `*` in its own file. Only some repos:
  `/*` then `!/repo-a/` in the parent's.
- One above where this session started is followed unless the researcher says
  otherwise. When a notice asks, put that to them once and record their answer
  as it says; never choose.
