---
name: probe
description: The skill for setting the Probe plugin state - `on` (the daemon records and reads), `read` (the daemon reads), `off` (no Probe).
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
```

`on (inline)`: the agent took Probe over (`probe session inline`) and reads and
writes it itself until `probe session daemon` hands it back to the daemon. Any
`/probe` move ends it.

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the daemon is used at all is set in
`probe wizard`, not by this switch.

## SAY WHERE IT LANDED:

> Probe is set to `on (daemon)` for this session - the daemon records and reads.

> Probe is set to `read only (daemon)` for this session - the daemon keeps
> reading the team's work, nothing further will be recorded.

> Probe is off for this session - the daemon neither records nor reads, so I
> will not be able to check prior work. `/probe [read/on]` to turn back on.

Moving the switch never deletes anything and never backfills anything. In the
`off` state, say you could not look; never report that no prior work exists.
