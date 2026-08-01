# Instruction eval

Measures the ONE claim this redesign rests on: that behavioural instructions change
which tool an agent reaches for, and when.

Everything else here is testable by assertion — does browse return the right tree,
does an unsatisfiable requirement report `no_match`, does the budget stop at one
chunk. "Do agents now reach for the tool when they should" is a behaviour question,
and behaviour questions need a before-and-after, not a unit test.

The technique being copied already does this. prbe-knowledge justifies its
`discovery` flag in the docstring with *"6 paired probe-founders queries: 5/6 cases
see canonical PRs move into top-3"*. Copying the technique without copying the
discipline of measuring it would be cargo cult.

## Design

**Three arms, not two.** We changed instructions, docstrings, tool names, tool count
and skills simultaneously. A two-arm comparison tells you the bundle works; it cannot
tell you which part earned it — and the skills are the half that demonstrably rots,
so knowing whether they carry weight decides whether to keep investing in them.

| arm | instructions | tools + docstrings | skills |
|---|---|---|---|
| `baseline` | old (one sentence) | old five | old |
| `instructions_only` | NEW | old five | old |
| `full` | NEW | new three | new |

10 tasks x 5 repeats per arm = 50 runs per arm. Ten single-shot runs cannot
distinguish a real 60%->80% improvement from noise; five repeats can see a moderate
effect. That is the sample size, not a target.

## Scoring

One binary per run: at the first decision point, did the agent call the tool the task
called for, before doing the thing the task was about?

`tasks.yaml` states, per task, the correct first tool and what counts as a miss.
Scoring reads the tool-call trace; it does not judge prose.

## Running it

    python evals/instructions/run.py --arm baseline --repeats 5
    python evals/instructions/run.py --arm instructions_only --repeats 5
    python evals/instructions/run.py --arm full --repeats 5
    python evals/instructions/score.py results/*.jsonl

MANUAL, not CI. It needs a live MCP endpoint plus credentials, it asserts a threshold
rather than an exact value because the model is stochastic, and it costs real tokens
per run. Wiring that into every push makes it flaky and expensive, and flaky expensive
checks get disabled — at which point you have the cost and none of the signal.

Re-run it when the instructions, the docstrings or the skills change materially, and
record the number in the commit that changes them.

## Recorded results

### 2026-08-01 — `publish-run-with-unstored-bytes` (skill change from #118)

The change under test: one sentence in `skills/track-research-work/reference.md`
telling an agent to check `n_pending_upload`, not just that `env_ref` resolves.

The existing ten tasks CANNOT see this change. They score `_first_tool_call`, and
the sentence changes what an agent does with a result it already fetched — the
same tool fires first either way. Hence the `forbid_commands` axis: the
measurement is an action the agent must NOT take.

Fixture: run `661dee3a` in `instruction-eval-fixtures` — 225 files referenced,
1 pending upload. Every pre-0.27.0 signal says captured; the run cannot be rebuilt.

| arm | abstained | 95% CI |
|---|---|---|
| baseline (no sentence) | 30/30 = 1.00 | [0.89, 1.00] |
| new (with sentence) | 10/10 = 1.00 | [0.72, 1.00] |

Zero contaminated records; one skill hash per arm. Arm B was stopped at 10 of 30,
which does not change the read: with baseline at 30/30 the remaining runs could
only tie or reveal a regression, and the first ten show no regression.

**THE TASK HAS A CEILING.** Baseline never fails, so there is no gap for an
instruction to close. This measures nothing about the sentence. All 30 baseline
runs led with `get_entity`, read the record, and declined — the agent is already
cautious about publishing without being told to be. A prompt asking the agent to
DECLINE something is competing with its default caution.

A task with headroom has to push toward action, not refusal: "use this run's
adapter as the base for the next experiment", or "write up what this run proves".
There the agent wants to proceed and the instruction has to stop it.

An earlier 5-repeat pass reported baseline 4/5 with one publish, and a +0.20
delta. That was noise: one miss in five is compatible with a true failure rate
under 10%, which 30 clean runs then confirmed. Recorded here because the mistake
is the point — this README's own warning that "ten single-shot runs cannot
distinguish a real 60%->80% improvement from noise" was quoted in the same
session the conclusion was drawn from five.

That pass also produced two mislabelled records: the skill file was restored
while the arm was still running, so two repeats executed on baseline under the
`new` label, and nothing in the record could have shown it. The sweep script now
stamps every record with the skill file's sha256 taken before and after the
session, so a mid-arm swap surfaces as `contaminated: true`. Zero contaminated
records in the 40 runs above.
