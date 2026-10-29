---
name: overview-charts
description: Which charts a page draws and how to frame each on the point it makes. Load when an entity has metrics.
---

Every visual makes one point, said in one sentence beside it.

- A curve over steps (loss, reward, accuracy over training): declare its series and draw it; never type its points.
- Results across runs (a score per setting, a sweep grid, seeds): draw your own chart from the numbers the runs report (`probe run get`, the entity card), 3 significant figures: bars for one score across settings, a heatmap for a two-setting grid, points with their spread for seeds. Label each mark with its run or setting.
- One number: say it. Two or three: a small table.
- How parts fit (a pipeline, two arms): a diagram (`artifact-diagramming`).
- A flat, single-point or broken series: leave it out and say so in one line.
- Charts, tables and labels follow `artifact-design`: one scale per chart, every label a value the chart reaches.

# Frame a curve on its sentence

A declared curve shows whole runs unless its entry says otherwise. When the sentence beside it is about something narrower, set the entry to show that:

- A moment (a spike, a divergence, a crash, the last stretch): zoom with `x_min` / `x_max` around it. Whole steps on a step axis; on `wall_clock`, ISO times inside the run's `first_time`-`last_time`, and the entry names its `kind`.
- Keys read together (train and val loss, reward and KL): one chart, the extra keys in `metrics`.
- A ratio, gap or running total no run logged: a computed line per run, `expression` + `label`, as JSON text. Each `series` leaf names a `kind` and `key` from that file: `{"op":"binary","fn":"sub","left":{"op":"series","kind":"eval","key":"loss"},"right":{"op":"series","kind":"train","key":"loss"}}`. Other ops: `const` (`value`), `unary` (`log`, `abs`, ...), `smooth` (`ema`/`sma`), `scan` (`cumsum`, `delta`), `window` (`mean`, `max`, ...); other `binary` fns: `add`, `mul`, `div`.
- Evals at a few steps the runs share, such as one per epoch: `chart`: `bar`.
- An early spike that flattens the rest: `y_min` / `y_max`, or `log`.
- At most 16 lines per chart (runs x keys, plus the computed line per run), and 8 computed lines per page.
- More curves than 6 declarations hold: chart the ones that carry the point and say what was left out.
