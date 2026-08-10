# Reference — artifacts, assets, publishing, project admin

Syntax and the rules that only matter once you are already doing the thing. The
judgement lives in the two SKILL.md files; this is the lookup table.

## Artifacts and asset versions

The registry is a named artifact with immutable, zero-copy versions. A version is
pinned from an uploaded run artifact; versions are never edited in place.

| what | command |
|---|---|
| upload an output | `probe artifact add RUN PATH --name N --kind KIND --step N` |
| list a run's outputs | `probe artifact list RUN` |
| download a pinned version | `probe artifact download ARTIFACT_ID --version N --to PATH` |
| read the version chain | `probe artifact versions ARTIFACT_ID` |
| pin a new version | `probe artifact version-add ARTIFACT_ID --from-artifact SOURCE_ID --label L` |
| who depends on this | `probe artifact pin-impact ARTIFACT_ID` |

`probe artifact add` streams to storage, so multi-GB weights or datasets upload
without being read into memory. With a local path and no `--uri` it runs the real
presign → PUT → confirm byte upload; `--uri` records a reference only.

In a script, `run.log_artifact(name, path=…)` is that same streamed upload and
`run.log_artifact(name, uri=…)` that same reference; `reference=True` with a path
records WHERE the bytes are without moving them, which is the shared-volume case for
a checkpoint too big to be worth uploading. The rest of the table maps onto
`client.list_artifact_versions` / `create_artifact_version` /
`download_artifact_version_to`, and `run.list_artifacts()` /
`run.resolve_artifact(name)` read what this run can see, including artifacts promoted
to its experiment or project.

What the reuse check resolved to decides the next command:

- **exact compatible version exists** → download it. Record consumption; do not copy
  it into a new identity.
- **same purpose, content must change** → produce the new content, upload it as a run
  artifact, then `version-add` it to the SAME artifact. Read `versions` before and
  after to show the compatibility impact.
- **nothing compatible exists** → `probe artifact add` creates the new identity, and
  its first version opens the chain. Record the concrete reason in the experiment.

For datasets, pin provenance in the version meta: input asset versions, the transform
script version, parameters, schema/statistics, and the output content hash.

## Tracing a path, URI, or content hash

`search_knowledge` with the path, URI, artifact id, or content hash as the query — its
exact channel matches artifacts directly, and the default collapse keeps those hits
(it dedupes experiments, it does not filter). Then follow
`get_entity(view="lineage")` on the run that owns the hit.

There is no trace-file tool. It was removed rather than fixed: no backend trace index
has ever existed, so it answered "no matches" to every query, and "this file has no
lineage" is a far more damaging answer than "I could not find it". If you cannot
establish provenance, say so — do not infer absence from an empty result.

## Publishing

Only when the researcher explicitly asks to mark, publish, or approve. The published
record is an immutable **experiment version** — a launch-time manifest of the
experiment's runs — plus any reusable results pinned as artifact versions. There is
no run-level "official" flag; never encode one as a filename or run metadata.

1. `get_entity(view="reproduce")` on each candidate run. Verify the hypothesis, the
   config, and that `env_ref` resolves — `missing: ["execution_record"]` means the run
   captured no snapshot and cannot be reproduced. Check `code.manifest.n_pending_upload`
   too: above zero, those files were never stored and the run cannot be rebuilt either. Add `view="trajectory"` when the
   claim depends on what the run did rather than on its final numbers.
2. `get_entity(view="versions")` on the experiment first: if a version already covers
   this set, do not mint a second.
3. Present the exact experiment + asset versions that would become the published
   record and obtain explicit approval for that set. Metrics and exit status are not
   approval.
4. `probe artifact version-add ...` for each approved asset, then
   `probe version create EXPERIMENT_ID --label LABEL`. Report the created version.

Never publish a set that differs from what was approved. A `completeness.state` of
`"partial"` on any view you based the decision on means you have not seen the whole
record — resolve it (follow `next_cursor`, or raise `token_budget`) or say so before
asking for approval. Publication mutates organizational truth; a partial read is not
a basis for it.

## Project and experiment admin

`create` is part of starting work (see `start-research-work` step 2). The rest is
curation — it fires when someone is tidying structure, not when work is happening.

`probe project create | list | get | use | set | move | delete`
`probe project set PROJECT --summary TEXT|@FILE|-` — visible Markdown below the live AI summary
`probe experiment create | set | delete | edges`
`probe run set RUN [--name NAME] [--description DESCRIPTION] [--notes TEXT|@FILE|-]`
`probe group create EXP --name NAME [--kind KIND] [--spec JSON|@FILE] [--notes ...]`
`probe group set GROUP [--name NAME] [--spec JSON|@FILE] [--notes TEXT|@FILE|-]`
`probe notes show | write [FILE] [--append]` — the PROJECT's hidden agent briefing

`probe project use SLUG` sets the ambient project — MACHINE-globally, shared by every
process on the box, so prefer `--project` or `PROBE_PROJECT` when another session
might be running (`start-research-work` step 3). `run start` applies it when
`--project` is omitted, and uses it to CHECK that the experiment belongs there —
it no longer decides where anything gets filed, because `run start` no longer
creates anything. `probe experiment set EXP --hypothesis "..."` amends a hypothesis.
`probe run set` amends a run's human title, description or notes without changing its
lifecycle state, metrics, or lineage.

Notes exist on projects, runs and run groups, and NOT on experiments. `--notes ""`
clears; `--notes @file` and `--notes -` read a file and stdin, since a caveat is
usually a paragraph. Which of the three to use is `track-research-work` step 1.
