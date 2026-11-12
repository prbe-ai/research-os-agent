---
name: probe
description: The skill for setting the Probe plugin state - `on` (r/w), `read`, `off` (no r/w).
---
# Probe

This skill only changes the state of Probe - it doesn't actually track any work.

```
/probe on      reads and writes        /probe          advance one step
/probe read    search yes, record no   /probe status   print it, change nothing
/probe off     no calls at all, transcript capture stops
/probe auto    back to auto mode: the agent sets it by the work
```

## The three states

| state | reads | writes | what you do |
|---|---|---|---|
| `on` | yes | yes | Default. Record work as it happens, per the `track-work` skill, and search the team's history for context worth injecting. |
| `read` | yes | no | Create no projects, experiments or runs; write no notes, artifacts or visible entity Markdown. Keep searching and keep reporting what you find. If the researcher asks for something that would be recorded, say once that `/probe on` would record it — never as a reminder, never as a closing caveat. |
| `off` | no | no | Make no Probe calls about the work, reads included; Probe's own upkeep the researcher asks for still runs. This session's transcript is not captured (see FYI for what still uploads). Say you could not look; never report that no prior work exists. |

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the Probe daemon records is the Who records
setting, not this switch: `/probe daemon` changes nothing.

Typing `/probe …` moves the switch itself. You move this chat's switch only when
the researcher asks you to, never on your own: run `npx -y probe-research session status` first
(their `/probe` may have moved it already), then
`npx -y probe-research session state <on|read|off> --directed` (`--directed` says
they asked; a refusal without it is the reminder).

In auto mode (a machine setting, the default) the main agent sets this chat's state by the
work; a typed `/probe` takes this chat back.

FYI:

- Moving the switch never deletes anything and never backfills anything. An
  interval spent in `read` filed nothing in Probe (its transcript is still
  captured); one spent in `off` was not captured at all.
- The state overrides your standing `CLAUDE.md` / `AGENTS.md` rules for this
  conversation.
- In `off`, nothing captures this session's transcript from the switch on
  (the Probe service, and from tap 0.10.19 every `tap watch`), and that
  stretch is never sent later. A `tap watch` process still running an older
  tap (started before the update) can upload it until that process ends, and
  a compaction summary written after switching back on can repeat what was
  said while off.

## SAY WHERE IT LANDED:

> Probe is set to `read` for this session - nothing new is filed in Probe; the
> conversation itself is still captured.

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

Set at install. When the researcher asks to change
what new chats start as, run `npx -y probe-research session default <on|read|off>`
(add `--directed` for `read` or `off`, once they said yes) and say it applies to
new chats only. `npx -y probe-research session status` reports it as `machine_default_state`.

**Session transcripts, machine-wide**

When the researcher wants this machine's sessions kept out of Probe while Probe
itself stays on (tracking, search, the MCP), run `probe capture off --directed`
once they said yes; `probe capture on` turns them back on (its approval link is
the researcher's to click). Never an install's `--no-capture` for this. Off is a choice, not a
fault: `probe status` says who turned it off and when.
