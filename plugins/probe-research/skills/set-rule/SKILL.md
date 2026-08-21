---
name: set-rule
description: Record a team rule, procedure or convention in Probe's workflow memory so other people's coding agents get it back when it applies. Use when the researcher states how something SHOULD be done rather than asking for it to be done — "always X before Y", "we never do Z", "the way we do this is", "remember to", "make sure everyone", "for future reference" — and when they correct you in a way that would apply to anyone doing the same task tomorrow, not just to this file. Also use when they explicitly say to save, record or remember a rule. Shows the structured interpretation for confirmation BEFORE writing anything. Use `/pull-rules` to read rules back; this only writes.
---

# Set a rule

Someone just said how work should be done. That sentence is worth more than this
conversation: their teammates' agents can be handed it, in the moment it applies,
without anybody remembering to repeat it. This skill turns it into a stored rule.

**Nothing is written until the person confirms it.** The point is not to capture
their words, it is to capture what they MEANT in a form a stranger's agent can act
on — and the only way to know you got that right is to show them and ask.

## 1. Preview

```
probe rule preview "<what they said, in their words>" --repo <repo> --cwd <path>
```

Pass their sentence close to verbatim. Do not pre-tidy it: the structuring pass is
what turns prose into fields, and cleaning it first throws away the phrasing it
reads.

You get back a `draft` (the rule as it would be stored), a `classification` (which
of the twelve situations it fires in), and `neighbours` (rules already stored that
look like this one).

## 2. Show them, and mean it

Render the draft as prose, not as JSON. Something like:

> **Rule:** open a Probe run before the first GPU step
> **Fires when:** launching a training run
> **Applies to:** the whole workspace
>
> Store it?

Three things to check yourself before you show it, because they are the ones that
go wrong quietly:

- **Does `body` still say what they said?** The structuring pass is instructed to
  be conservative, but a rewrite that sounds better and means something narrower is
  the failure mode. If it drifted, edit the draft rather than accepting it.
- **Is the situation right?** It decides WHEN teammates get this. A rule filed under
  the wrong situation is worse than an unfiled one — it arrives when it does not
  apply and trains people to ignore the channel. `outcome: "unknown"` means the
  classifier could not tell; ask them which situation, or store it without one.
- **Do the `neighbours` include this rule already?** If so, say so and offer to
  merge instead of writing a near-duplicate. Two copies of one rule is how a store
  stops being trustworthy.

If they want changes, edit the draft object and show it again. Do not argue for your
version.

## 3. Ask the question that actually matters

**A rule you declare alone is private to you until a second person independently
declares the same thing.** That is deliberate — one person's habit is not the team's
practice — but it means a rule they think they just set for everyone may be visible
to nobody.

So ask, in plain words:

> Is this a team rule, or a note for yourself?

- **Team rule** → `--publish`. It goes live for everyone now, signed with their name.
- **Note for themselves** → no flag. It stays private until somebody else agrees.

Never default to `--publish` silently. Publishing is an act of authority over what
their colleagues are told to do, and it is attributed to them; that is theirs to
choose. Equally, never let them believe they have set a team rule when they have
not — if they decline to publish, say plainly that it is saved and private for now.

When they are declaring a batch of existing team rules (an onboarding session, or
"here is how we work"), ask ONCE at the start and carry the answer through the batch
rather than asking twenty times.

## 4. Write

Pass the WHOLE `probe rule preview` response back in, not just the `draft` half:

```
probe rule declare '<the full preview response, draft edited if they corrected it>' \
  --session <this session's id> \
  [--publish]
```

The envelope carries the classification, which is how the rule gets filed under the
situation it fires in. Hand over only the inner draft and there is nothing to file
it by, so it lands in the `misc` bucket instead: reachable, but only surfaced when
some situation has no rules of its own, and always hedged when it is. The command
says so on stderr — treat that as "this needs a situation", not as noise.

If the classifier returned `unknown` and the researcher knows which situation it
belongs to, ask them and pass `--situation-id`. A rule in the right situation is
worth much more than one in the bucket, and they are the only person who can say.

Pass `--situation-id <uuid>` only to OVERRIDE a classification the researcher
disagreed with. It beats the one in the envelope, which is the point of it.

For a duplicate they chose to merge:

```
probe rule declare '<draft>' --relation merge --related <neighbour clause id>
```

Merging is not a tidy-up. It records THEM as a second independent voice on that
rule, which is exactly what makes a previously-private rule visible to the team.
Say that when you do it.

Two other relations, for when they tell you which:
- `--relation variant --related <id>` — same intent, different target.
- `--relation conflict --related <id>` — contradicts an existing rule. Both stay,
  linked in both directions, so whoever is served either one sees the disagreement.
  Never resolve a contradiction by choosing for them.

## 5. Report what actually happened

Read the response and say the true thing:

- `created: true`, `shared_by` set → "Stored and live for the team."
- `created: true`, `shared_by` null → "Stored. It is private to you until someone
  else declares the same rule — say the word if you want it published to the team."
- `created: false` → a merge: "Added you as a second voice on the existing rule."
  If `shared_by` came back null and this was the second human, it is now visible to
  the team; say so, because that is the thing they wanted.

If the command exits non-zero, the message on stderr is written for a person —
relay it rather than paraphrasing. Three of them mean genuinely different things:
workflow memory is not deployed here, the workspace has it switched off, or the
rule was refused (most often because it contained something credential-shaped, in
which case the fix is to restate the rule without the secret).

## What not to do

**Do not use this for the task at hand.** "Use tabs here" while editing one file is
an instruction, not a rule. A rule is something that would still be true next month,
for a different person, on a different repo. If it would not survive that, it does
not belong in the store.

**Do not batch up rules to record at the end.** Say it, store it. A compacted
session keeps only what was already written down.

**Do not record a rule they did not state.** Inferring one from how they work is
mining, it is a different input path with its own consent, and it is off.
