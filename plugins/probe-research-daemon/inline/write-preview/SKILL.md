---
name: write-preview
description: A project's canvas preview. Use it unasked after `probe project create`.
---

# Write a project's preview

Only while you record this chat yourself (`probe session status` says `inline`); otherwise the
daemon's writer does it. Sandboxed: add `--out <a folder in your workspace>` to `checkout`. Told to
leave it: tell the researcher in one line.

The preview is one custom pane, `about`, showing what the project is about. Use one visual:

1. A real example from its runs: the model's input beside its output, two models on one input, or
   an output with no input shown two ways (a protein's 3D structure beside its sequence). Several
   examples: a `<select>` that redraws every view. Read it through the recipe, even when its files
   are locked or expired for you today: others, or newer runs, load them.
2. No run has an example file yet: a small SVG drawing of the setup, its 2-4 steps and one worked
   example in numbers. Invented examples stay clearly illustrative, never results.
Label each output: generated, predicted or the reference. Provenance stays in the recipe and linked
run/file records. No Source button, dropdown or footnote. Fonts: only `var(--ov-font)`; `var(--ov-mono)`
for code. The visual sits directly on the project card: a `var(--ov-bg)` or transparent background,
no white page, outer panel or extra border.

No charts, scores, tables, captions, title or description line. No useful visual: leave the preview
unset. If its data or files cannot load, hide the content and show nothing.

Write it after `probe project create`, or when `probe pane preview show <project>` says `none`.
Rewrite it when `show` says `is_static` and runs now hold a real example.

Run `probe pane preview checkout <project>` and follow its `next`. On a refusal, do what it says,
at most twice.

Nobody picks the preview by hand. Only when the researcher asks to turn it off: `probe pane preview
clear <project> --directed`.

Recipes and data calls: `write-pane`.
