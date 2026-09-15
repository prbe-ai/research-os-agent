---
name: probe
description: The Probe switch for THIS conversation, with three states — `on` (reads and writes, the default), `read` (search the team's history, record nothing new), and `off` (no Probe calls at all). Use it when the researcher says to stop tracking, that this session is not research, to go quiet, to stop recording, to turn Probe off or back on, or asks what state Probe is in. Typed bare by the researcher it ADVANCES one step — on → read → off → on. An agent loading this skill as a tool only reads the guidance and never moves the switch. Setting a default for future sessions or for a folder is here too.
---

# Probe: the switch

This skill is the switch and nothing else. `probe-research:track-work` is the
manual for recording work from a shell, and `probe-research:instrument-code` is
the same job from inside a script. This decides whether either of them may run.

## The three states

```
             /probe             /probe            /probe
       on ------------> read ------------> off ------------> on
            (stop              (stop             (resume
            writing)           reading)          everything)
```

| state | reads | writes | what you do |
|---|---|---|---|
| `on` | yes | yes | Record the work as it happens. The default. |
| `read` | yes | no | Keep searching prior work and keep reporting what you find. Create and modify nothing. |
| `off` | no | no | Make no Probe calls. Do not raise Probe again. |

Each press of the bare switch removes exactly one capability, so the cycle is
learnable after one lap.

**Nothing is ever deleted by moving the switch,** and nothing is backfilled by
moving it back. An interval spent in `read` or `off` happened unrecorded;
reconstructing it afterwards would be a worse lie than the gap.

## How it moves

A plugin hook watches this skill's activation and writes the state. Pass the
state you want:

```
/probe on       reads and writes
/probe read     search yes, record no
/probe off      nothing
/probe          advance one step
/probe status   print the state, change nothing
```

`status` writes NOTHING — a question never moves a switch.

**A bare invocation is the researcher's spelling, not yours.** Typed by them
(`/probe`, `$probe`, `/skill:probe` on pi) it advances one step. Invoked by an
AGENT with no argument — a tool call, a skill activation — it writes nothing at
all: that is how this guidance gets loaded mid-task, and reading the manual must
never move the switch. When you need the state changed, pass the state word.

**Never write the state yourself.** `probe session state` is not this skill's
tool. An agent that can move the switch can unblock its own writes, which would
make the opt-out meaningless.

**The hook tells you where it landed.** You do not have to guess, and you do not
have to read it back — the state arrives in your context the moment it moves. If
what you were told contradicts what the researcher plainly asked for, that is a
broken hook: say so in one line and report the state as it is, rather than
quietly repairing it.

To check without moving anything: `probe session status`, and read `state`.

## What each state obliges you to do

**`on`.** Normal. Record as the work happens. Say so in one line when the
switch lands here; do not open a project just to prove it worked.

**`read`.** Create no projects, experiments or runs; write no notes,
artifacts or visible entity Markdown. **Keep searching.** The team's history is
still the best source you have for prior rationale, incidents and constraints,
and a session that stops looking because it cannot write has lost the half that
was free. Report what you find as normal.

> Probe is set to `read` for this session — nothing further will be recorded.
> What was already recorded is untouched. I can still search prior work.

**`off`.** Make no Probe calls at all, reads included. Do not raise Probe
again — not as a reminder, not as a closing caveat.

`off` has a cost, and it is your job to surface it rather than hide it: there
may be prior work, decisions or incidents that bear on the task, and you cannot
see them and cannot know what you missed. **Never report "no prior work found"
from an `off` session** — that is a claim about the team's record made by
something that did not read it. Say you could not look.

> Probe is off for this session — no calls at all, so I will not be able to
> check prior work. `/probe read` restores searching.

**There is no one-off exception.** If the researcher asks again for the same
call, that is still `off` — say so and name the switch. Only moving the switch
changes what you may do, and moving it is theirs. An agent that treats a repeat
request as permission has turned the state into a suggestion.

All three states override the standing CLAUDE.md / AGENTS.md rules for this
conversation. That is the point of the switch.

## Who moves it, and which way

Starting is the AGENT's call — the standing rules make recording automatic.
Stopping is the RESEARCHER's. Never invert that by waiting to be told to record.

## Legacy spellings still work

`/track-work off`, `$track-work off` and the older
`toggle-research-tracking` / `research-tracking` names still move the switch,
and **`off` typed at any of them means `read`** — that is what it has
always done, since the switch never gated reads. The hard `off` is reached only
by naming it here. A resumed transcript, or muscle memory, keeps working and
keeps meaning what it meant.

The states were called `full` and `read-only` before they were called `on` and
`read`, and both spellings are accepted wherever a state is typed (`readonly`,
`read_only` and `ro` too). The LONGER names are also what still lands in the
config file and the session marker, deliberately: those files are read by every
other copy of the client on the machine — an older plugin, a vendored hook —
and a word this version invented reads to them as unrecognised, which resolves
to `on`. Renaming the words is free; renaming the bytes would turn somebody's
opt-out into recording.

## Defaults for future sessions

The switch is per-conversation. A default is what a NEW session starts at.

| intent | target / action |
|---|---|
| “this repo” | Resolve `git rev-parse --show-toplevel` from the current directory and use that absolute root. If it fails, ask which folder to use; do not substitute the cwd. |
| “this folder” or a named folder | Use that exact directory as an absolute path. |
| set a folder default | `probe session default on\|read\|off --folder PATH` |
| remove the folder override / inherit | `probe session default inherit --folder PATH` |
| inspect the folder default | `probe session default --folder PATH` |
| set or inspect the machine default | `probe session default [on\|read\|off]` |

Show the CLI's JSON after every action so the researcher sees both the exact
folder override and the effective inherited default. Never edit
`.probe/config.json` directly.

A per-session state always beats a default. Anything unrecognised in a config
file reads as `on`, never as a quieter state: a typo must not silently stop
recording someone's research, and it must not silently stop them searching
either.

The wizard sets the same machine default on its `Probe in new sessions` row —
the same three states, and the same key cycles them.
