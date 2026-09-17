---
name: probe
description: The Probe switch for this conversation - `on` (reads + writes), `read` (read-only), `off` (no Probe calls at all). Use when the researcher says to stop tracking, go quiet, turn Probe off or back on, or asks what state it is in. Only the researcher moves it - typed bare it toggles on <-> read; an agent invoking this skill never moves the switch.
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

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing `/probe
off`; one press leaves it for `read`.

FYI:

- Moving the switch never deletes anything and never backfills anything. An
  interval spent in `read` or `off` happened unrecorded.
- There are no one-off exceptions. If the researcher asks again for the same
  call, that is still the state they set — say so and name the switch. A repeat
  request is not permission.
- Never move the switch yourself. An agent that can move it can unblock its own
  writes.
- The state overrides your standing `CLAUDE.md` / `AGENTS.md` rules for this
  conversation. That is the point of it.

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

**Folder Defaults**

The hook already writes direct `/probe --folder` commands but semantic requests ("default this repo to read"), run it yourself: `probe session default read --folder PATH`.
- A relative path resolves against this session's directory, and `~` works. The folder must already exist.
- "this repo" is `git rev-parse --show-toplevel`. If that fails, ASK which folder - never fall back to the cwd.
- Never edit `.probe/config.json` by hand.
