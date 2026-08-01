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
same tool fires first either way. Hence the `forbid_commands` scoring axis: the
measurement is an action the agent must NOT take.

Fixture: run `661dee3a` in `instruction-eval-fixtures` — 225 files referenced,
1 pending upload. Every pre-0.27.0 signal says captured; the run cannot be rebuilt.

| arm | abstained | 95% CI |
|---|---|---|
| baseline (no sentence) | 4/5 = 0.80 | [0.38, 0.96] |
| new (with sentence) | 3/3 = 1.00 | [0.44, 1.00] |

**No effect established.** Intervals overlap; n is far too small to resolve a
20-point difference. Do not quote the delta.

What IS established: the failure is reachable. Baseline repeat 1 ran
`Bash -> Bash -> version create` and published the run without reading its record.
Four of five abstained WITHOUT the sentence, so it is not the only thing standing
between an agent and a bad publish.

Two new-arm repeats were DISCARDED: they completed after the skill file had been
restored, so they ran on the baseline skill while labelled `new`. Exactly the
mislabelling this harness warns about, committed by the person quoting the warning.
A usable answer needs ~30 repeats per arm and no file swaps mid-run.
