---
name: pull-rules
description: Read the team's stored rules and procedures out of Probe's workflow memory. Use BEFORE doing something the team is likely to have a convention about — launching a training run, opening a PR, deploying, processing a dataset, running an eval, provisioning infrastructure, recovering from an incident, or claiming work is finished — and when someone asks what the rules are, how the team does something, or what conventions exist. Also use when arriving in an unfamiliar workspace. Reads only; `/set-rule` is what records one.
---

# Pull the team's rules

Somebody on this team already worked out how this should be done and wrote it down.
This is how you get it back before repeating a mistake they have already paid for.

## When to reach for it

Before an irreversible or shared-consequence step, not after. The situations the
store is organised around are the ones where doing it wrong costs money, data or an
incident: launching a run, opening a PR, deploying, processing a dataset, running an
eval, reviewing code, provisioning infrastructure, reproducing an experiment,
debugging a failing run, recovering from an incident, editing a repo, and claiming
work is done.

If you are about to do one of those in an unfamiliar workspace, check first. It is
one command and the answer is usually short.

## Reading

Ask for what applies to the situation you are actually in:

```
probe rule list --query "about to kick off a training run" --session <session id>
```

Or name the situation directly when you already know it:

```
probe rule list --situation launch-run
```

Or, to see everything the team has captured:

```
probe rule list --limit 50
```

Pass `--session` when you have one. It records which rules were put in front of this
session, which is what later lets the system tell an independent opinion from an echo
of something it was already shown.

## Reading the answer honestly

Every rule comes back with more than its text, and the extra fields change what you
should do with it.

**`shared_by` and `human_backers` together.** `human_backers: 2` or more means two
or more people's evidence independently backs this — the team demonstrably does it.
`shared_by` set with `human_backers: 1` means ONE person published this on their own
authority; it is a real instruction from a named colleague, but nobody has
corroborated it. Both are worth following. Only the first is worth calling "the
team's practice" when you tell the researcher about it.

**`status`.** `declared`, `documented` and `expert_confirmed` are safe to follow
without asking. `contested` means somebody disputed it and `stale` means it has
rotted — surface those, do not silently act on them.

**Conflicts.** If two rules contradict each other, both are returned. Do not pick
one. Show the researcher both and ask, because the disagreement is the finding.

**An empty result is not nothing.** Read which empty it is:

- `no_situations_configured: true` — this workspace's rule vocabulary was never set
  up, so nothing can ever match. Tell the researcher; it is a five-minute admin fix
  and until somebody does it the feature is silently dead.
- `classification.outcome: "unknown"` — the classifier could not tell which
  situation you are in, so it served nothing rather than guessing. Retry with a
  clearer description of what you are doing, or name the situation with
  `--situation`. It did not fail; guessing would have been the failure.
- Genuinely no rules — the team has not captured one for this yet. If the researcher
  then tells you how it should be done, that is `/set-rule`.

## Using what you get

Follow the rules that apply and say which one you are following, by name, when you
act on it. A rule silently obeyed teaches the researcher nothing about whether the
store is any good, and whether it is any good is the question that decides if this
is worth keeping.

If a rule is wrong, say so rather than working around it. Wrong rules in a store
people trust are worse than no store; the researcher can correct or withdraw it.

**Their instruction beats a stored rule, always.** If the researcher tells you to do
something a rule forbids, do what they said. Mention the rule once so they know it
exists, then move on — do not argue it, and do not refuse. The store is memory, not
policy enforcement.
