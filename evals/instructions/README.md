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
