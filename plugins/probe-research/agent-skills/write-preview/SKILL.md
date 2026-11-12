---
name: write-preview
description: Write a project's preview - the pane in its box on the canvas that makes plain what the project is about. Use it unasked right after a project is created, and again only when what the project is about changes or something explains it better.
---

# Write a project's preview

Write one only while you record this chat yourself (`probe session status` says `inline`); where the
Probe daemon records it, its writer writes previews: leave them. Under `read` or `off`, write none.
A sandboxed `probe` writes only in your workspace: add `--out <folder in your workspace>` to `checkout`.

A preview is one pane (`write-pane`) chosen to fill a project's box on the canvas. Its one job: someone
glancing at the box understands what the project is about. The box already shows the project's title,
kind and activity time; write only what goes under them.

## When

- Right after `probe project create`: one plain sentence on what the project is for. Every project gets
  one.
- Later, rewrite it only when what the project is about changes, or when something explains it better
  (the first real example, once runs hold one). Never for a new score, a new run or a heartbeat.
- Projects only: an experiment's preview is its project's.
- `probe pane preview checkout <project>` writes nothing and says why when you may not choose (someone
  else chose one, or previews are off here) or the project holds 8 panes: leave it, and say so to the
  researcher in one line. When a command says previews are not on for this team, skip previews and say
  nothing.

## What goes in it

Whatever makes the project plainest:
- A visual, when it explains better than words: a diagram of the setup, one real input next to its
  output (a question and the SQL the model wrote; one frame under two models' masks; a designed protein's
  3D structure beside its sequence), a sample image.
- Words: what it is for, what goes in, what comes out.
- At least one plain sentence on what the project is for. Alone, it is the preview of a project whose
  runs hold nothing to show yet, a static pane (`"data": []`); once they hold a real example, show one,
  and when they hold several, a `<select>` picks which one shows.

A result appears only to explain the task, never as a scoreboard, best run, progress or status. Never
a report, a results table, a list of experiments, a metric chart, captions or footnotes, the project's
title, kind or time, or an outer card.

A real example comes from this project's own runs, read through the recipe; never type in values you
did not read. Both halves of a pair are one sample (the same file, or files with the same name under
each run). Give each sample shown a short label, never a caption: generated, predicted or measured; a
model's output or the reference. An illustration you drew or a question you reworded says so in its Source panel.

## How it looks

- 18-24px padding; 320-480px wide and as tall as the content; no fixed heights, no empty space.
- Text: `font-family: var(--ov-font)`, 14px, labels 11-12px, semibold only for headings;
  `var(--ov-mono)` only for code, sequences and math. No other font, no uppercase eyebrows, no
  letter-spacing.
- Colors: the host's `--ov-bg`, `--ov-surface`, `--ov-surface-2`, `--ov-fg`, `--ov-muted`, `--ov-line`
  and accent tokens; neutral chrome, the example carries the color. Badges: 4px radius.
- Picking a sample: a plain `<select>` with a `change` handler; the frame turns it into the product's
  own dropdown. Never style it or build your own menu.
- Where an example came from (run, file, "illustration"): behind a small "Source" button that opens a
  short panel, never a caption.
- Every file read can fail (`PROBE.image` and `PROBE.text` reject; a run past retention is `locked`):
  catch it and keep the sentence.

## Budget

- Recipe: `"runs"` = the 1-3 run ids the example comes from; at most 3 entries; `files` by exact
  `prefix` or `name`, `limit` 6 or less.
- HTML under 40,000 characters; at most one exact-version cdnjs library.
- No `PROBE.onData`: a preview does not change while a run is live.

## Write it and push it

`probe pane preview checkout <project>` writes the preview you may change, or a one-sentence starter,
as two draft files and prints their paths and the slug to push (`about` for a new one). Replace the
starter sentence, edit both, then:

    probe pane push <project> <slug> --html <html> --recipe <recipe> --preview --check
    probe pane push <project> <slug> --html <html> --recipe <recipe> --preview

`--preview` saves the pane and makes it the project's preview in one step, only if nobody else chose
one; a refusal saves nothing:
- `preview_opted_out`, `preview_chosen` or `project_previews_off`: leave it.
- `pane_taken`: run `probe pane preview checkout <project> --new` and push to the slug it prints.
- `pane_refused`: fix what it names and push again, twice at most.

Only when the researcher asks: `probe pane preview set <project> <slug> --directed` makes one of the
project's panes its preview (also after previews were turned off), and `probe pane preview clear
<project> --directed` turns them off here; the panes stay.

Recipes, files and `PROBE` calls: `write-pane` §2-§3.
