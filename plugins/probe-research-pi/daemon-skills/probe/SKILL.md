---
name: probe
description: The skill for setting the Probe plugin state - `on` (the daemon records and reads), `read` (the daemon reads), `off` (no Probe), `inline` (the agent reads and writes Probe itself).
---
# Probe

This skill only changes the state of Probe for this session. The Probe daemon
records this session and reads the team's work for you; you record nothing
yourself.

Typing `/probe …` moves the switch itself, before you read this. Run
`npx -y probe-research session status` first: if their `/probe` already moved it, do nothing more
than say its state in one line, and only when no `Probe:` line told them this turn.
You move this chat's switch only when the researcher asks you to, never on your
own: `npx -y probe-research session state <on|read|off|inline> --directed`
(`--directed` says they asked; a refusal without it is the reminder).
In auto mode (a machine setting) the main agent sets this chat's state by the
work; a typed `/probe` takes this chat back.
When the researcher asks to change what new chats start as, run
`npx -y probe-research session default <on|read|off>` (add `--directed` for `read`
or `off`, once they said yes) and say it applies to new chats only.

```
/probe on      on (daemon): the daemon records and reads     /probe          advance one step
/probe read    read only (daemon): reads, files nothing      /probe status   print it, change nothing
/probe off     off: no daemon, no Probe calls, transcript capture stops
/probe inline  on (inline): no daemon; you read and write Probe yourself
/probe auto    back to auto mode: the agent sets it by the work
```

`/probe inline` is the researcher's: you go inline only when they ask
(`npx -y probe-research session state inline --directed`), unless the machine is
in auto mode, where the main agent may run `probe session state inline` itself.
The daemon stops, and you read and write Probe yourself, recording by the two
skills the notice names; any other `/probe` move hands Probe back to the daemon.
A session another program launched cannot start inline (no environment variable
sets it): once it has started, the researcher asks the agent in that session. A
session with no person in it (a worker, `claude -p`) is moved from a terminal
with `probe session state inline --session <id>`.

If the team's free daemon allowance runs out, Probe automatically hands this
session to inline mode and tells you why. Record and read by the inline notice;
if this session was read-only, keep it read-only and record nothing. The saved
recorder preference stays unchanged. `/probe on` tries the daemon again.

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the daemon is used at all is the Who records
setting, not this switch.

## SAY WHERE IT LANDED:

> Probe is set to `on (daemon)` for this session - the daemon records and reads.

> Probe is set to `read only (daemon)` for this session - the daemon keeps
> reading the team's work and files nothing; the conversation itself is still
> captured.

> Probe is off for this session - the daemon neither records nor reads, so I
> will not be able to check prior work, and this session's transcript is not
> captured from now on (a capture process still running an older tap can
> upload it until it ends). `/probe [read/on]` to turn back on.

> Probe is set to `on (inline)` for this session - the daemon is off, and I read
> and write Probe myself.

Moving the switch never deletes anything and never backfills anything. In the
`off` state, say you could not look; never report that no prior work exists.
In `off`, nothing captures the transcript from the switch on (the Probe service,
and from tap 0.10.19 every `tap watch`). A `tap watch` process still running an
older tap (started before the update) can upload it until that process ends, and
a compaction summary written after switching back on can repeat what was said
while off.
