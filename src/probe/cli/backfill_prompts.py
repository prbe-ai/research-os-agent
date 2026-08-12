"""The prompts a backfill hands its agents, composed from shared fragments.

:mod:`probe.cli.backfill` says it out loud: the prompt is the actual deliverable
of this feature. The user never writes it, and every rule that keeps an import
honest lives in prose the agent reads rather than in code that could enforce it.

There are FOUR prompts now -- classify, import a unit, map W&B, write the notes
-- and they share rules that must not drift: what a name is, when to upload by
reference, reuse before you create, always write a description. A rule that
exists in four format strings will fall behind in one of them, and nothing fails
when it does. So the invariants live here ONCE, as named fragments, and each
pass composes the ones it needs.

That also keeps the existing tests honest. They assert on prompt TEXT
(``tests/test_backfill.py``), so a fragment can be asserted to appear in every
prompt that needs it, instead of one prompt being checked and three drifting.
"""

from __future__ import annotations

from pathlib import Path

from .backfill import REFERENCE_ABOVE_BYTES, human_bytes

# -- shared invariants -------------------------------------------------------

#: The rule that breaks the dashboard when it is broken. The folder tree is
#: built by splitting `name` on '/', and the preview is chosen from the
#: extension at its end -- so a sentence appended to a name breaks both.
NAMING = """\
NAMING, and this one is not stylistic.

    An artifact's --name is the file's RELATIVE PATH and nothing else.
    The dashboard builds the folder tree by splitting that name on '/', and
    works out how to preview a file from the extension at its end. A
    description appended to a name breaks both.

    Descriptions go in --notes. Never in --name."""

#: Uploading a 10GB checkpoint is neither possible nor useful, and on a shared
#: mount a file:// reference resolves from every other researcher's pod anyway.
REFERENCES = f"""\
LARGE FILES.

    At or over {human_bytes(REFERENCE_ABOVE_BYTES)} -- checkpoints, datasets,
    archives, weights -- record the PATH instead of the bytes:

        --reference --allow-missing

    Do NOT pass --hash on those. Fingerprinting a 10GB file over a shared
    mount costs minutes and buys nothing here."""

#: The one mistake in this flow that used to be unrecoverable. It is now
#: recoverable (artifacts move laterally), but a split record is still a mess
#: someone has to clean up, so the discipline stands.
REUSE = """\
REUSE BEFORE YOU CREATE.

    List what exists before you create anything:

        probe project list

    Two projects for the same research splits the record in half. It is much
    easier to do than it looks: `odyssey-infill-v3` created next to an
    existing `odyssey_infill_v3` reads as a typo to a human and as a second
    project to everything else."""

#: Nothing else fills these in. The server generates a description only when a
#: child RUN reaches a terminal status, and an import creates no runs -- so a
#: project left undescribed here stays undescribed forever.
DESCRIPTIONS = """\
ALWAYS WRITE A DESCRIPTION.

    Nothing else fills it in. The server generates one only when a child run
    reaches a terminal status, and importing a folder creates no runs, so
    anything you leave undescribed stays undescribed forever.

    A few words is fine. Three sentences is the ceiling. The point is that it
    is WRITTEN. Missed one? `probe project set <slug> --description "..."`."""

#: Descriptions are the part nobody did at the time, and the part that makes a
#: file findable later. It matters more than the upload.
DESCRIBE_ARTIFACTS = """\
SAY WHAT THINGS ARE.

    For every artifact that carries meaning -- a report, a result table, a
    config, a plot, a script someone would look for again -- say in one line
    what it is, what produced it, and what it shows.

    This is the part nobody did at the time, and the part that makes a file
    findable in six months. It matters more than the upload."""

#: The confinement, said out loud. The agent is not merely asked to leave the
#: folder alone -- it cannot write there, by a permission rule or an OS sandbox
#: depending on which agent is running (see CONFINEMENT in `backfill`). Saying
#: so saves it discovering the wall by walking into it and retrying.
#:
#: The path halves matter more than they look. Your working directory is NOT the
#: imported folder, so a relative `data/train.csv` resolves into the scratch dir
#: and every read fails "no such file". Reads must be absolute. Manifest rows
#: must be the opposite -- relative -- because the process that enqueues them
#: runs with the folder as its cwd, and an absolute path there uploads under a
#: name carrying somebody's home directory in it.
def readonly(*, root, work_dir: str) -> str:
    return f"""\
THE FOLDER IS READ-ONLY. You cannot write, move or delete anything under
{root}, and attempting it is not a permissions hiccup to work around -- it is
the point. Someone's research directory must look untouched afterwards.

    Your working directory is {work_dir}. Write there, and only there.

    READ with ABSOLUTE paths: {root}/<the path as listed>. The listings below
    are relative to the folder, and your working directory is not the folder,
    so a relative path resolves to the wrong place and reads as missing."""


#: Step 3 of the original prompt, kept verbatim in spirit: an invented
#: experiment is a wrong answer that looks like a right one.
GROUPING = """\
GROUP INTO EXPERIMENTS ONLY IF THE EVIDENCE IS THERE.

    If part of this plainly IS one experiment -- a hypothesis, a method, a
    result you can point at -- create it and attach those artifacts. If that
    would be a guess, DO NOT.

    Artifacts at the project level are findable. An invented experiment is a
    wrong answer that looks like a right one, and it is worse than no answer."""


#: What to say about mtime, given whether it still carries information.
#:
#: The positive form is the whole reason mtime is in the evidence at all. The
#: negative one exists because a COPY erases it: `cp -r`, or an rsync without
#: `--times`, stamps every file with the copy time, so a shared drive someone
#: was handed collapses into one burst. Left unsaid, the agent goes on being
#: told that timestamps group work the directory tree does not -- and goes on
#: believing it, which is worse than having no signal.
MTIME_USEFUL = """\
Files written within minutes of each other are usually one run, so `mtime` and
`mtime_span` group work that the directory tree does not."""

MTIME_DEAD = """\
IGNORE `mtime` AND `mtime_span` HERE. Nearly every file carries the same
timestamp, which happens when a folder has been copied -- so "written together"
separates nothing on this drive and is not evidence of anything. Group on what
the files SAY."""


def mtime_guidance(uninformative: bool) -> str:
    return MTIME_DEAD if uninformative else MTIME_USEFUL


def _block(*fragments: str) -> str:
    return "\n\n".join(f for f in fragments if f)


# -- pass 1: classify --------------------------------------------------------


def classify(
    *, root, evidence_jsonl: str, existing: list[str], truncated: bool, work_dir: str,
    mtime_uninformative: bool = False,
) -> str:
    """Decide which project each FILE belongs to. Uploads nothing.

    The agent is handed EVIDENCE, not a directory listing, because the question
    is per-file and a directory does not answer it. It is told when its evidence
    was truncated by a budget, because an agent that believes it saw everything
    reports confidence it has not earned.
    """
    known = (
        "Projects that already exist. Prefer these over inventing new ones:\n    "
        + "\n    ".join(existing)
        if existing
        else "No projects exist yet. You are naming them for the first time."
    )
    mtime_note = mtime_guidance(mtime_uninformative)
    caveat = (
        "\n\nNOTE: the sample budget was reached, so some evidence files were "
        "listed without their contents. Where you are placing a file on its path "
        "and neighbours alone rather than on what it says, mark it low confidence."
        if truncated
        else ""
    )
    return f"""\
You are deciding how one research folder should be organised in Probe.

FOLDER: {root}

You are NOT uploading anything in this step. You are deciding, for each file,
which project it belongs to. Someone will review your answer before anything moves.

{readonly(root=root, work_dir=work_dir)}

WHY THIS IS PER-FILE. A directory is not a project. One researcher's directory
routinely holds pieces of several lines of work, and one line of work routinely
spills across directories nobody would group together. Grouping by folder is the
wrong answer that looks tidy.

{known}

EVIDENCE. One JSON object per line, in TWO shapes.

    A row with "path" is ONE FILE whose head was sampled. The `sample` field is
    what it actually says. These are the rows that can name a project.

    A row with "dir" is EVERY remaining file in that directory, rolled up:
    checkpoints, shards, images, weights. They carry no text identifying
    anything, so they are counted rather than listed. `files`, `bytes`, `ext`
    and `mtime_span` describe the group.

    ASSIGN A ROLLUP ROW BY ITS "dir" VALUE, exactly as written, and every file
    under it goes with it. Do not invent per-file paths for them.

{mtime_note}{caveat}

{evidence_jsonl}

{REUSE}

{DESCRIPTIONS}

    Name projects for the WORK, not the directory. `odyssey-infill-v3` and
    `esm3-baseline` are names someone will recognise in six months; `workspace`,
    `data` and `michael` are not.

TAIL FILES INHERIT. A checkpoint carries no evidence of what it belongs to, so
place it with its neighbours and say which neighbours decided it. An inherited
placement a reader cannot audit is a guess wearing a rule's clothes.

Answer with JSON on its own line and nothing after it:

{{"projects": [{{"slug": "...", "name": "...", "description": "...",
                "tags": ["..."]}}],
  "assignments": [{{"path": "<a row's \"path\", or a rollup row's \"dir\", verbatim>",
                   "project": "<slug>",
                   "confidence": "high"|"low", "why": "<short>"}}],
  "unsure": ["<relative path>", ...],
  "summary": "<one sentence on what this folder contains>"}}

Every ROW above must appear exactly once in `assignments` -- a rollup row
counts once and carries all its files with it. Put
anything you are genuinely unsure about in `unsure` as well -- that list is what
the human reviews first, and a short honest one is worth more than none.
"""


# -- pass 1b: revise the plan a human just read ------------------------------


def revise(*, feedback: str, root, work_dir: str, resumed: bool) -> str:
    """Fold a reviewer's correction back into the classification.

    Sent into the SAME agent session where possible, which is the whole point:
    the evidence is already in its context, so a correction costs one turn
    instead of re-reading the folder. `resumed` says whether that worked --
    when it did not, the agent is starting cold and has to be told so, because
    a cold agent asked to "revise your plan" has no plan to revise and will
    invent one that ignores everything the reviewer did not mention.

    The correction is quoted rather than paraphrased into instructions. A
    reviewer who writes "lockfiles aren't part of the research" is stating a
    rule with reach beyond the files they happened to see, and rewriting that
    into "move package-lock.json to X" throws away the general half.
    """
    context = (
        "You proposed a classification for this folder a moment ago and the "
        "reviewer has read it."
        if resumed
        else (
            "A classification for this folder was proposed and the reviewer has "
            "read it. YOU DO NOT HAVE IT -- your previous session could not be "
            "resumed, so re-read the evidence and produce a fresh plan that "
            "honours the correction below."
        )
    )
    return f"""\
{context}

FOLDER: {root}
{readonly(root=root, work_dir=work_dir)}

THE REVIEWER SAYS:

    {feedback}

Apply it, and apply what it IMPLIES. A correction is usually a rule, not a
one-off: "lockfiles are not research" means every lockfile, not the two that
happened to be on screen. Where the rule is genuinely ambiguous, do the narrow
thing and say so in `summary`.

Do not take the correction as licence to redo placements it says nothing
about. A reviewer who fixes one directory has implicitly accepted the rest,
and a plan that shuffles everything makes their next read start over.

Answer with the SAME JSON shape as before, complete and on its own line --
every row of the evidence appears exactly once in `assignments`, not just the
ones you changed:

{{"projects": [{{"slug": "...", "name": "...", "description": "...",
                "tags": ["..."]}}],
  "assignments": [{{"path": "...", "project": "...",
                   "confidence": "high"|"low", "why": "<short>"}}],
  "unsure": ["..."],
  "summary": "<one sentence, saying what you changed and why>"}}
"""


# -- pass 2: import one unit -------------------------------------------------


def import_unit(*, root, project: str, paths: list[str], manifest_path: str) -> str:
    """Upload and describe one unit's files, all bound for one project.

    The agent WRITES A MANIFEST rather than shelling out per file. One process
    then enqueues the lot. At two hundred thousand files, a process start and a
    slug lookup per file is tens of CPU-hours before any bytes move; the agent's
    judgement is what we are paying for, not its ability to run a loop.
    """
    listing = "\n".join(f"    {p}" for p in paths)
    return f"""\
You are importing part of a research folder into Probe.

FOLDER: {root}
PROJECT: {project}     (already created -- do not create any project)

{readonly(root=root, work_dir=str(Path(manifest_path).parent))}

These {len(paths):,} files, and only these, are yours (relative to the folder):

{listing}

Read them, then WRITE A MANIFEST describing what to upload. Do not run
`probe artifact add` yourself -- one process enqueues the whole manifest
afterwards, which is thousands of times faster than one call per file.

Write JSONL to: {manifest_path}
One object per line, and ONLY these four keys -- an unrecognised key fails that
row, and booleans must be bare `true`/`false`, never the strings "true"/"false":

    {{"path": "<path relative to the folder, exactly as listed above>",
      "notes": "<what it is, what produced it, what it shows>",
      "reference": true|false,
      "allow_missing": true|false}}

    Set "allow_missing": true alongside "reference": true for anything on a
    shared mount the uploading machine may not see at the same path. It is
    ignored unless "reference" is true.

    "path" is RELATIVE even though you read the file by its absolute path. The
    process that enqueues this manifest runs with the folder as its working
    directory; an absolute path there uploads the file under a name with
    somebody's home directory baked into it.

    Put NOTHING else in this file. Your closing summary is your own output, not
    a manifest row -- appended here it fails as one.
{NAMING}

{REFERENCES}

    Set "reference": true for those. Leave it false or omit it otherwise.

{DESCRIBE_ARTIFACTS}

    Not every file earns a note. Build noise, caches and lockfiles do not.
    A file with nothing worth saying still goes in the manifest, just without
    `notes` -- the manifest is the upload list, and a file you leave out is a
    file that does not get imported.

{GROUPING}

    If you do create an experiment, do it with `probe experiment create` and say
    so in your summary. The files that convinced you go in the manifest as usual.

Do NOT write the project's notes. Something else writes those once, at the end,
so that it can see the whole import instead of your slice of it.

Finish with JSON on its own line:

{{"manifest": "{manifest_path}", "rows": N, "described": N,
  "experiments_created": N, "summary": "<one sentence>"}}
"""


# -- pass 3: map W&B ---------------------------------------------------------


def map_wandb(*, inventory: str, projects: list[str]) -> str:
    """Decide which W&B project's runs land in which EXISTING Probe project.

    The target list is an INPUT. This pass runs after the files are imported
    precisely so that a W&B project can land under a project that came from
    files, rather than minting a near-duplicate beside it.
    """
    known = "\n    ".join(projects) if projects else "(none yet)"
    return f"""\
You are matching Weights & Biases projects onto Probe projects that already exist.

PROBE PROJECTS (these already exist -- prefer them, strongly):
    {known}

W&B INVENTORY. One JSON object per line: entity, project, run counts, run names,
config keys and metric keys.

{inventory}

Match on the WORK, not the name. A W&B project called `sweep-3` whose runs log
the same metrics as an existing Probe project belongs in that project. Creating
`sweep-3` beside it splits one line of research in half.

{REUSE}

Only propose a NEW project when the work genuinely has no home yet, and then
{DESCRIPTIONS.split(chr(10), 1)[1].strip()}

Answer with JSON on its own line:

{{"mappings": [{{"entity": "...", "wandb_project": "...",
                "probe_project": "<existing slug or new slug>",
                "new": true|false, "why": "<short>"}}],
  "unsure": ["<entity/project>", ...],
  "summary": "<one sentence>"}}
"""


# -- pass 4: the project's visible summary and hidden notes ------------------


def write_notes(*, project: str, root, landed: int, manifests: list[str]) -> str:
    """One project-level writer, once, after every import unit has landed.

    The writer separates teammate-facing documentation from the hidden agent
    briefing. It still runs once because a coherent document needs the whole
    import, while notes append server-side so existing operational context is
    never replaced.
    """
    refs = "\n".join(f"    {m}" for m in manifests)
    return f"""\
You are writing the project-level documentation for one Probe project, once,
now that its import has finished.

PROJECT: {project}
FOLDER:  {root}
{landed:,} artifacts landed.

The manifests the importing agents produced, which say what each file is:

{refs}

Read the manifests and the project's artifact list first. Project prose has
three homes: description is the short identity; the editable Project Summary
suffix is durable teammate-facing dashboard Markdown; hidden notes are the
operational agent briefing.

For the visible suffix, explain what this project is and how the pieces relate.
The dashboard renders a server-owned AI narrative first and this editable
Markdown immediately after it as one Project Summary. Never edit or duplicate
the AI narrative. `summary_markdown` is whole-document and last-write-wins, so
read it immediately before editing, preserve useful existing sections, replace
the complete file, and verify what landed:

    probe project get {project} | jq -r '.summary_markdown // ""' > PROJECT.md
    # Edit PROJECT.md.
    probe project set {project} --summary @PROJECT.md
    probe project get {project} | jq -r '.summary_markdown // ""'

Put missing evidence, trust caveats, and operational handoff details in a
separate file and append them to hidden notes:

    probe notes write --project {project} --append <file>

Be honest about gaps. "The 2024 sweep configs are here but their result tables
are not" is worth more to the next agent than a confident summary that quietly
omits it. Do not describe individual files -- they already carry their own
notes. Three visible paragraphs plus concise caveats is plenty.

Finish with JSON on its own line:

{{"project": "{project}", "summary_chars": N, "gaps_noted": N}}
"""


# -- pass 1, chunked: survey -> name -> assign -------------------------------
#
# Used only when the evidence does not fit one prompt. THE SPLIT IS CHOSEN SO NO
# CHUNK EVER NEEDS ANOTHER CHUNK'S DETAIL, because the obvious alternative --
# feed the agent chunks in sequence and let auto-compaction absorb the overflow
# -- is wrong in a way that does not announce itself. Compaction is lossy, and
# what it discards is exactly the evidence the classification runs on: early
# files would be placed against evidence and later ones against a summary of it,
# with nothing at the review gate to say which was which.
#
# So the global decision is made ONCE, on summaries small enough to fit
# together, and everything else is per-chunk and independent:
#
#   SURVEY  each chunk describes what work it seems to contain. Local only.
#   NAME    one pass over those descriptions decides the project list.
#   ASSIGN  each chunk maps its own rows onto that fixed list. Independent,
#           so it parallelises and a failed chunk retries alone.


#: Said to every chunked pass when the sample budget stopped short. The chunked
#: route fires on exactly the folders that blow that cap, so this is true in
#: essentially every chunked run -- and an agent that believes it saw the
#: contents reports confidence it has not earned.
TRUNCATED_CAVEAT = """
NOTE: the sample budget was reached, so some files are listed with no contents.
Where you are going on a path and its neighbours rather than on what a file
says, mark it low confidence."""


def survey(
    *, root, evidence_jsonl: str, index: int, total: int, work_dir: str,
    truncated: bool = False,
) -> str:
    """Describe one slice of a folder. Names nothing globally."""
    caveat = TRUNCATED_CAVEAT if truncated else ""
    return f"""\
You are reading part of a research folder to work out what is in it.

FOLDER: {root}
This is slice {index} of {total}. You are seeing SOME of the folder, not all of it.

{readonly(root=root, work_dir=work_dir)}

Do NOT name projects yet, and do not try to guess what the other slices hold.
Something else decides the project list once every slice has reported. Your job
is to say, concretely, what lines of work THIS slice shows evidence of.

EVIDENCE. One JSON object per line, in two shapes: a row with "path" is one
sampled file and `sample` is what it says; a row with "dir" is every remaining
file in that directory rolled up, counted rather than listed.

{evidence_jsonl}
{caveat}

For each distinct line of work you can see, say what it is, what in this slice
shows it, and roughly how much of the slice belongs to it. Be specific -- "an
attention-vs-consensus ablation with Hessian max-LR analysis" is useful,
"machine learning code" is not. If part of this slice is plainly build noise,
caches or vendored dependencies, say so as its own entry.

Answer with JSON on its own line and nothing after it:

{{"findings": [{{"work": "<what this line of work is, one sentence>",
                "evidence": ["<path or dir that shows it>", ...],
                "approx_files": <int>}}],
  "notes": "<anything the naming step should know, or empty>"}}
"""


def name_projects(
    *, root, findings_json: str, existing: list[str], feedback: str = "",
) -> str:
    """Turn every slice's findings into ONE project list. The only global step."""
    known = (
        "Projects that already exist. Prefer these over inventing new ones:\n    "
        + "\n    ".join(existing)
        if existing
        else "No projects exist yet. You are naming them for the first time."
    )
    correction = (
        f"""
THE REVIEWER HAS SEEN A PREVIOUS ANSWER AND SAYS:

    {feedback}

Apply it, and apply what it IMPLIES -- a correction is usually a rule, not a
one-off. It outranks your own reading of the slices where the two disagree.
"""
        if feedback
        else ""
    )
    return f"""\
You are deciding how one research folder should be organised in Probe.

FOLDER: {root}
{correction}
The folder was read in slices. Below is what each slice reported. You are NOT
seeing the files -- you are seeing every slice's account of them, which is the
whole folder, and deciding the project list from it.

{known}

{findings_json}

THE SAME WORK APPEARS IN SEVERAL SLICES. That is the normal case, not a
conflict: one line of work spills across directories, and the slices are cut by
size rather than by meaning. Merge those into ONE project. Two projects for what
is plainly the same work is the expensive mistake here -- it splits the record
in half and every later comparison reads them as different things.

{REUSE}

{DESCRIPTIONS}

    Name projects for the WORK, not the directory. `odyssey-infill-v3` and
    `esm3-baseline` are names someone will recognise in six months; `workspace`,
    `data` and `michael` are not.

Keep the list SHORT. A folder is rarely more than a handful of lines of work,
and every extra project is one more place someone has to look. Build noise and
caches belong in one project of their own, not scattered.

Answer with JSON on its own line and nothing after it:

{{"projects": [{{"slug": "...", "name": "...", "description": "...",
                "tags": ["..."]}}],
  "summary": "<one sentence on what this folder contains>"}}
"""


def assign_chunk(
    *, root, evidence_jsonl: str, projects: str, index: int, total: int,
    work_dir: str, truncated: bool = False, mtime_uninformative: bool = False,
) -> str:
    """Map one slice's rows onto an already-decided project list."""
    caveat = TRUNCATED_CAVEAT if truncated else ""
    mtime_note = mtime_guidance(mtime_uninformative)
    return f"""\
You are filing part of a research folder into projects that are already decided.

FOLDER: {root}
This is slice {index} of {total}.

{readonly(root=root, work_dir=work_dir)}

THE PROJECTS. Use these and only these. You may not invent one, rename one, or
leave a row out:

{projects}

EVIDENCE. One JSON object per line. A row with "path" is one file. A row with
"dir" is every remaining file in that directory, rolled up -- assign it by its
"dir" value, exactly as written, and every file under it goes with it.

{evidence_jsonl}
{caveat}

{mtime_note}

TAIL FILES INHERIT. A checkpoint carries no evidence of what it belongs to, so
place it with its neighbours and say which neighbours decided it.

If a row genuinely does not fit any project, put it in whichever is closest and
mark it low confidence -- do NOT drop it. A row you leave out is a file that
does not get imported, and nothing downstream can tell that from an oversight.

Answer with JSON on its own line and nothing after it:

{{"assignments": [{{"path": "<a row's \"path\", or a rollup row's \"dir\", verbatim>",
                   "project": "<one of the slugs above>",
                   "confidence": "high"|"low", "why": "<short>"}}],
  "unsure": ["<path>", ...]}}

EVERY row above must appear exactly once in `assignments` -- {index} of {total}
slices is still all of this slice.
"""
