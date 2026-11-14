---
name: write-preview
description: A project's canvas preview. Use it unasked after `probe project create`.
---

# Write a project's preview

Only while you record this chat yourself (`probe session status` says `inline`); otherwise the
daemon's writer does it. Sandboxed: add `--out <a folder in your workspace>` to `checkout`. Told to
leave it: tell the researcher in one line.

The preview is one custom pane, `about`, showing what the project is about. Pick one:

1. A real example from its runs: the model's input beside its output, two models on one input, or
   an output with no input shown two ways (a protein's 3D structure beside its sequence). Several
   examples: a `<select>` that redraws every view. Read it through the recipe, even when its files
   are locked or expired for you today: others, or newer runs, load them.
2. No run has an example file yet: a small SVG drawing of the setup, its 2-4 steps and one worked
   example in numbers, marked "illustration" in Source.
3. Only if no picture is clearer: one line of 6-8 plain words, `<p class="about">`.

Label each output: generated, predicted or the reference. Where it came from (run, file,
"illustration") goes behind a small Source button, never in a caption. Fonts: only `var(--ov-font)`,
and `var(--ov-mono)` for code.

No charts, scores, tables, captions or title. A picture that reads run data also keeps the line
from 3, shown only when no example loads, with everything else hidden.

Write it after `probe project create`, or when `probe pane preview show <project>` says `none`.
Rewrite it when `show` says `is_static` and runs now hold an example, or `is_floor` and a drawing is
clearer.

Run `probe pane preview checkout <project>` and follow its `next`. On a refusal, do what it says,
at most twice.

Only when the researcher asks: `probe pane preview set <project> <slug> --directed` or `probe pane
preview clear <project> --directed`.

Recipes and data calls: `write-pane`.
