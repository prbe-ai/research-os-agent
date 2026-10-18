---
name: overview-charts
description: When to chart what a run recorded, and how to frame the chart on the point it makes; you declare a series, the host draws it. Load when an entity has metrics.
---

Never type a data point. Declare a series; the host injects the real points.

```html
<div id="chart-loss" class="ov-chart" style="height:280px"></div>
<script>PROBE.lineChart('chart-loss', 'loss', {title: 'Validation loss', yName: 'val/loss'})</script>
```

- Copy the metric key and run ids verbatim from the `.chartable.json` file the checkout names. One container and one call per series. Options: `title`, `yName`, `xName`, `smooth`, `log`.
- Chart: a shape over steps (`point_count` 2 or more); two or three runs of one key compared; a crashed-and-resumed run as one line.
- Don't chart: one number (say it), a key with `point_count` 1 in every run, such as a final or test score (a small table), numbers from notes rather than logged metrics (a small table, never a plot drawn by hand), how parts fit (a diagram: `artifact-diagramming`), a flat series.
- An experiment's results charts go in its Metrics center (`overview-visuals`), not here; a training curve over steps (loss, learning rate, reward) stays here.
- One sentence per chart says what it shows.

# Frame the chart on its sentence

A chart shows whole runs unless its entry says otherwise. When the sentence beside it is about something narrower, set the entry to show that:

- A moment (a spike, a divergence, a crash, the last stretch): zoom with `x_min` / `x_max` around it. Whole steps on a step axis; on `wall_clock`, ISO times inside the run's `first_time`-`last_time`, and the entry names its `kind`.
- Keys read together (train and val loss, reward and KL): one chart, the extra keys in `metrics`.
- A ratio, gap or running total no run logged: a computed line per run, `expression` + `label`, as JSON text. Each `series` leaf names a `kind` and `key` from that file: `{"op":"binary","fn":"sub","left":{"op":"series","kind":"eval","key":"loss"},"right":{"op":"series","kind":"train","key":"loss"}}`. Other ops: `const` (`value`), `unary` (`log`, `abs`, ...), `smooth` (`ema`/`sma`), `scan` (`cumsum`, `delta`), `window` (`mean`, `max`, ...); other `binary` fns: `add`, `mul`, `div`.
- Evals at a few steps the runs share, such as one per epoch: `chart`: `bar`.
- An early spike that flattens the rest: `y_min` / `y_max`, or `log`.
- At most 16 lines per chart (runs x keys, plus the computed line per run), and 8 computed lines per page.
