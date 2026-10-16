---
name: overview-visuals
description: Pick an experiment's tables and charts, or a project's tables; you name fields and runs, the server reads the numbers. Read before `probe visuals set`.
---

Never type a number. Name what to show; the server reads the values.

```json
[{"id": "acc", "kind": "bar", "title": "Accuracy by learning rate",
  "x": {"config": "lr"}, "group_reduce": "mean",
  "measures": [{"source": "metric", "key": "eval/accuracy"}]}]
```

- Copy keys, paths and run ids verbatim from `probe visuals options`.
- A measure: `metric` (`key`, `reduce`: last, min, max, mean, sum, count), `summary` or `config` (`path`), or `run` (`field`: name, status).
- `runs`: `"all"` or this entity's run ids. `sort` + `limit` keep the top N.
- An experiment's results tables and charts all go here, none on its page. A project's tables go here; its charts stay on its page. `[]` only when there is nothing to show yet, or one number the page can say.

# Kinds

- `table`: a row per run, configs and scores (`columns`). The default for a sweep.
- `bar` / `stacked_bar`: one score across runs, a config or a dimension (`x`, `measures`).
- `scatter`: one value against another, a point per run (`x`, `y`).
- `heatmap`: a sweep grid, two configs and a score per cell (`x`, `y`, `value`).
- `box`: spread across seeds or repeats (`measure`, `group`).
- `histogram`: how a score is distributed (`measure`, `over`).
- `parallel`: many configs and scores, a line per run (`axes`).
- `metric_grid`: one run's scores keyed `{row}/{column}` (`rows`, `columns` with `label`, `value`), instead of a one-row table. The newest run unless `runs` names it; `group_reduce` folds several.
- No `line`: the metric section already charts every metric over steps. One number or a summary is the page's job.

Every `table` and `metric_grid` has a readable `title` and a `description` of one or two short sentences (at most 600 characters), shown just below the title. Explain what the rows compare, what the measures mean, and the checkpoint or evaluation scope when known. Describe the table, without inventing results or typing numeric scores; the server supplies those. Add a description to existing captionless tables when updating their overview. For example: "Compare the recipes at their final dev checkpoint. Each row reports clean-row rate, reward, and fully judged rows."

# A project's tables

`table` and `metric_grid` only: its runs side by side across experiments, metrics by `last`, `min` or `max`. At most 12 columns, 8 tables and 200 runs per table (`limit`; 50 if unset).
