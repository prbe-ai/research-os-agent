---
name: probe
description: The skill for setting the Probe plugin state - `on` (the daemon records and reads), `read` (the daemon reads), `off` (no Probe), `inline` (the agent reads and writes Probe itself).
---
# Probe

This skill only changes the state of Probe for this session. The Probe daemon
records this session and reads the team's work for you; you record nothing
yourself.

Typing `/probe …` moves the switch itself, before you read this; you never move it.
To see where it landed, run `probe session status`.

```
/probe on      on (daemon): the daemon records and reads     /probe          advance one step
/probe read    read only (daemon): reads, records nothing    /probe status   print it, change nothing
/probe off     off: no daemon, no Probe calls
/probe inline  on (inline): no daemon; you read and write Probe yourself
```

`/probe inline` is typed by the researcher only. The daemon stops, and you read
and write Probe yourself, recording by the two skills the notice names; any
other `/probe` move hands Probe back to the daemon.

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the daemon is used at all is set in
`probe wizard`, not by this switch.

## SAY WHERE IT LANDED:

> Probe is set to `on (daemon)` for this session - the daemon records and reads.

> Probe is set to `read only (daemon)` for this session - the daemon keeps
> reading the team's work, nothing further will be recorded.

> Probe is off for this session - the daemon neither records nor reads, so I
> will not be able to check prior work. `/probe [read/on]` to turn back on.

> Probe is set to `on (inline)` for this session - the daemon is off, and I read
> and write Probe myself.

Moving the switch never deletes anything and never backfills anything. In the
`off` state, say you could not look; never report that no prior work exists.
