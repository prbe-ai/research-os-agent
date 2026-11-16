---
name: write-preview
description: A project's canvas preview. Use it unasked after `probe project create`.
---

# Write a project's preview

Only while you record this chat yourself (`probe session status` says `inline`); otherwise the
daemon's writer does it. Sandboxed: add `--out <a folder in your workspace>` to `checkout`. Told to
leave it: tell the researcher in one line.

Pane `about` shows what the project is about. Use one visual:

1. An example attached to its runs, read through the recipe (even locked files): input beside
   output, two models on one input, one output two ways. Several: a `<select>` in a
   `<foreignObject>` redrawing every view.
2. No example attached to a run yet (files only on disk count as none): never upload, attach or
   start runs for a preview; draw a concrete example in its subject's form (question beside SQL),
   clearly illustrative (labelled), never results. Don't stop to ask.

Shape: one `<svg viewBox="0 0 440 H">`, H 88-528, filling the width, no wrapper padding/margin,
nothing visible outside it. Images: script sets each `<image>` `href` from `PROBE.image(row)`; a
`data:` URI or path in markup is refused.

Code `var(--ov-mono)`. Example text 16-20px, weight 600; labels ≥12px, weight 500; text in
`var(--ov-fg)` or `var(--ov-muted)`.

Colour: fills, marks and chip tints from `var(--ov-c1)`...`var(--ov-c8)`. Chips and panels:
`var(--ov-surface-2)` or a hue at 15% opacity, radius 8, no outline, outer border or background.
Declare no custom properties (`--name:`, `setProperty('--…')`), not even `--ov-*` aliases. Label
outputs: generated, predicted or reference.

No charts, scores, tables, captions, title, description line, Source button, decorative icons,
input→process→output flowchart. No useful visual: leave unset. Data fails to load: show nothing.

Write it when `probe pane preview show <project>` says `none`, rewrite when `is_static` and runs
hold one.

Run `probe pane preview checkout <project>`, follow its `next`, at most twice after a refusal.

Nobody picks the preview by hand. Clear it only when the researcher asks: `probe pane preview
clear <project> --directed`.

Recipes, data calls: `write-pane`.
