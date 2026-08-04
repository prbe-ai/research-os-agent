---
name: capture-run-inputs
description: Decide which files a run actually depends on and get them into its snapshot. Use before or right after `probe snapshot` on any run whose inputs are not fully tracked in git — a downloaded dataset, a base checkpoint, a tokenizer, an out-of-tree config, anything under .gitignore, or any project that is not a git repo at all. Also use when a past run needs auditing for whether it could actually be rebuilt. Not for outputs (checkpoints the run produced, metrics, plots) — those are artifacts, logged with `probe artifact add`.
---

# Capture run inputs

`probe snapshot` captures what git can see. It cannot know that `data/train.jsonl`
is the dataset and `.venv` is not, because `.gitignore` was written to keep a repo
clean, not to describe an experiment. Deciding which files are INPUTS is judgment,
and that judgment is this skill.

The mechanism already exists and is not your job: `probe snapshot RUN --include GLOB`
stores what you name, `probe snapshot-restore RUN DEST` rebuilds it. Your job is
the list of globs, and the record of how you chose them.

## Inputs, not outputs

The distinction is load-bearing and easy to blur.

| | what it is | where it goes |
| --- | --- | --- |
| **input** | what the run consumed — code, config, dataset, base weights, tokenizer | the snapshot (`--include`) |
| **output** | what the run produced — trained checkpoints, metrics, plots, eval dumps | `probe artifact add` |

Sweeping outputs into the snapshot makes it enormous and makes "what produced this
result?" unanswerable, because cause and effect end up in the same bag. If you are
unsure which side something falls on, ask whether the run would behave differently
had the file been different. If yes, it is an input.

## 1. Snapshot first, then read what it missed

```
probe snapshot RUN_ID --cwd PATH
probe snapshot-show RUN_ID
```

`snapshot-show` prints every captured file. What is NOT in that list is your
candidate set. Do not skip this: guessing at the gap without reading the manifest
is how a file that was already captured gets included twice, and how one that was
not gets missed.

## 2. Find the real inputs

Read the entry point the run executes and follow what it opens. Concretely:

- **Paths in the code.** `open(...)`, `pd.read_*`, `load_dataset(...)`,
  `torch.load(...)`, `from_pretrained(...)`, any hard-coded or config-driven path.
- **The config the run was launched with**, including files it `include:`s or
  `${...}`-interpolates from.
- **`.gitignore` itself.** It names the things someone deliberately kept out of the
  repo. Some of them are inputs; most are not. Read it and decide per entry rather
  than including it wholesale.
- **Base weights and tokenizers**, which are inputs even when they came from a
  registry — a registry can move or be re-tagged.
- **Env vars that change behaviour.** Record their NAMES in the decision record,
  never their values; a value is either uninteresting or a credential.

## 3. Include them

```
probe snapshot RUN_ID --cwd PATH \
  --include 'data/**' \
  --include configs/local.yaml \
  --include checkpoints/base.pt
```

Size decides what happens, and you do not have to decide it. Under
`--reference-over-mb` (100 by default) the file is stored in the run's code-bytes
archive. Above it, the path, host and sha256 are recorded and the bytes stay where
they are — copying a 40 GB checkpoint into every run of a sweep is duplication, not
reproducibility.

Three things will stop you, all deliberately:

- a glob matching nothing is an **error**, not a silent no-op
- a path outside the snapshot root is **refused**
- naming a file git already supplies adds no duplicate entry

A project that is not a git repo at all needs no `--include` for its own source:
`probe snapshot` captures the whole directory and uploads it, skipping
lockfile-rebuilt trees (`.venv`, `node_modules`, caches) and credential-shaped
names. Use `--include` there only for inputs living outside the directory.

## 4. Never include these

- **Secrets.** `.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`. The non-git path
  already refuses them; `--include` does not, because an explicit name is taken as
  deliberate. If a credential genuinely gates the run, record the env var NAME in
  the decision record and stop there.
- **Anything rebuildable from a lockfile** — `.venv`, `node_modules`, `__pycache__`.
  The environment is already captured exactly, as `deps` in the execution record.
- **Outputs.** See the table above.

## 5. Record the decision, not just the files

This is the step that is easy to skip and expensive to lose.

Once a human or agent chooses the scope, **absence stops being informative**. A file
missing from a snapshot could mean "not an input", or "considered and judged not an
input", or "nobody looked". Six weeks later those are indistinguishable, and the
third one is the one that ruins a reproduction.

So write down what you considered and rejected, with the reason:

```
probe artifact add RUN_ID inputs-decision.json --kind inputs_decision
```

```json
{
  "included": [
    {"path": "data/train.jsonl", "why": "the training set; regenerating it is not deterministic"},
    {"path": "checkpoints/base.pt", "why": "base weights; referenced, 4.2GB on gpu-node-7"}
  ],
  "excluded": [
    {"path": "data/cache/", "why": "regenerated from train.jsonl on first epoch"},
    {"path": ".env", "why": "credentials; run needs HF_TOKEN, value not recorded"},
    {"path": "outputs/", "why": "produced by the run, logged as artifacts"}
  ],
  "env_vars_that_matter": ["HF_TOKEN", "CUDA_VISIBLE_DEVICES"]
}
```

## 6. Verify it can actually be rebuilt

Do not take the counts on faith. Ask:

```
probe snapshot-restore RUN_ID --verify-only
```

It resolves every file — git blobs from the remote, the rest from the archive —
verifies each against the recorded sha256, and writes nothing. `0 unavailable` is
the claim you want. Files reported `OFF-PLATFORM` are the deliberately-referenced
large ones; they are not failures, but they do mean a rebuild needs that volume
mounted, so say so if you hand the run to someone else.

A run whose `--verify-only` reports unavailable files is not reproducible, no matter
what the snapshot summary said when it ran.

## When the run is already over

The same steps work retroactively as an audit. `snapshot-restore RUN --verify-only`
answers "could this be rebuilt?" for any past run. If it cannot and the machine that
ran it still exists, snapshotting again from that machine is the last chance to
capture the bytes — once the box is gone, the record can identify the code and never
produce it.
