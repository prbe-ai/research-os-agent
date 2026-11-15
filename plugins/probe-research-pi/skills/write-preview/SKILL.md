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
   one output shown two ways (a protein's structure and sequence). Several examples: a
   `<select>` that redraws every view. Read it through the recipe even if its files are locked to
   you; others load them.
2. No example file yet: a concrete example of the project's purpose in its subject's own form
   (question beside SQL, an edited document, a folder tree, a task's graph). Invented examples stay
   clearly illustrative, never results. No generic input → process → output flowchart or
   decorative icons.

Width: natural, up to 440px, `max-width:100%`; SVGs carry width, height and a viewBox; never
stretch to a fixed height. Check it at card size, also after its frame parks. Label outputs:
generated, predicted or the reference. No Source button, dropdown or footnote. Fonts: only
`var(--ov-font)`; `var(--ov-mono)` for code. Background: `var(--ov-bg)` or transparent; no page,
panel or border.

No charts, scores, tables, captions, title or description line. No useful visual: leave it unset.
If its data cannot load, show nothing.

Write it after `probe project create`, or when `probe pane preview show <project>` says `none`.
Rewrite it when `show` says `is_static` and runs now hold a real example.

Run `probe pane preview checkout <project>` and follow its `next`, at most twice after a refusal.

Nobody picks the preview by hand. Turn it off only when the researcher asks: `probe pane preview
clear <project> --directed`.

Recipes and data calls: `write-pane`.
