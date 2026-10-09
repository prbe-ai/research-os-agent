---
name: probe
description: The skill for setting the Probe plugin state - `on` (r/w), `read`, `off` (no r/w) - and `.probe.config` (paths Probe never records).
---
# Probe

This skill only changes the state of Probe - it doesn't actually track any work.

```
/probe on      reads and writes        /probe          advance one step
/probe read    search yes, record no   /probe status   print it, change nothing
/probe off     no calls at all
```

## The three states

| state | reads | writes | what you do |
|---|---|---|---|
| `on` | yes | yes | Default. Record work as it happens, per the `track-work` skill, and search the team's history for context worth injecting. |
| `read` | yes | no | Create no projects, experiments or runs; write no notes, artifacts or visible entity Markdown. Keep searching and keep reporting what you find. If the researcher asks for something that would be recorded, say once that `/probe on` would record it — never as a reminder, never as a closing caveat. |
| `off` | no | no | Make no Probe calls, reads included. Say you could not look; never report that no prior work exists. |

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the Probe daemon records is set in
`probe wizard`, not by this switch: `/probe daemon` changes nothing.

FYI:

- Moving the switch never deletes anything and never backfills anything. An
  interval spent in `read` or `off` happened unrecorded.
- The state overrides your standing `CLAUDE.md` / `AGENTS.md` rules for this
  conversation.

## SAY WHERE IT LANDED:

> Probe is set to `read` for this session - nothing further will be recorded.

> Probe is off for this session - no calls at all, so I will not be able to
> check prior work or write. `/probe [read/on]` to turn back on.

In the `off` state, do not fabricate information - do not report "no info"
during lookup - clearly state that you couldn't look due to the state.

## DEFAULTS:

A default is what a NEW session starts at. This conversation's state always
beats it.

**Machine-wide Defaults**

ONLY written by the wizard (`npx probe-research`). `probe session status`
reports it as `machine_default_state`.

## `.probe.config`:

Lists paths Probe never records (gitignore syntax), whatever the state; reads
are unaffected. Folders inherit their parents' file; where nothing matches, the
state decides. `probe ignore check PATH` names the excluding line.

- Create or edit one when the researcher asks, or when you judge a path should
  not be recorded; loosen or remove a rule only when they ask. Tell them each
  change (file and line). Whole folder: `*` in its own file. Only some repos:
  `/*` then `!/repo-a/` in the parent's.
- One above where this session started is followed unless the researcher says
  otherwise. When a notice asks, put that to them once and record their answer
  as it says; never choose.
