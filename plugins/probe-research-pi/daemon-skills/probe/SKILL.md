---
name: probe
description: The skill for setting the Probe plugin state - `on` (the daemon records and reads), `read` (the daemon reads), `off` (no Probe), `inline` (the agent reads and writes Probe itself) - and `.probe.config` (what Probe may read and record, per path).
---
# Probe

This skill only changes the state of Probe for this session. The Probe daemon
records this session and reads the team's work for you; you record nothing
yourself.

Typing `/probe …` moves the switch itself, before you read this; you never move it.
To see where it landed, run `probe session status`.

```
/probe on      on (daemon): the daemon records and reads     /probe          advance one step
/probe read    read only (daemon): reads, files nothing      /probe status   print it, change nothing
/probe off     off: no daemon, no Probe calls, transcript capture stops
/probe inline  on (inline): no daemon; you read and write Probe yourself
```

`/probe inline` is typed by the researcher only. The daemon stops, and you read
and write Probe yourself, recording by the two skills the notice names; any
other `/probe` move hands Probe back to the daemon.

If the team's free daemon allowance runs out, Probe automatically hands this
session to inline mode and tells you why. Record and read by the inline notice;
if this session was read-only, keep it read-only and record nothing. The saved
recorder preference stays unchanged. `/probe on` tries the daemon again.

Bare `/probe` toggles `on` <-> `read`. `off` is reached only by typing it; one
press leaves it for `read`. Whether the daemon is used at all is set in
`probe wizard`, not by this switch.

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

## `.probe.config`:

Sets what Probe may do with paths (gitignore syntax), whatever the state: a plain
line is `off` (Probe never reads or records it), `<pattern> @probe read` lets the
daemon look but records nothing, `!pattern` or `<pattern> @probe on` turns a path
back on; `@probe <level>` alone on the first line sets every line's level. Create
or edit one when the researcher asks, or when you judge a path should not be
recorded; loosen or remove a rule only when they ask. Tell them each change.
When a notice asks you to put a choice about one to them, ask once and record
their answer as it says; never choose.
