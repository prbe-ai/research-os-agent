---
name: manage-research-asset
description: Reuse, inspect, materialize, or version shared research assets such as scripts, scoring methods, datasets, transforms, configs, container or SIF definitions, checkpoints, and templates. Use before copying or creating a reusable asset, or whenever an existing asset needs changes, to reduce duplicate identities and preserve version lineage.
---

# Manage a research asset

The registry is a named asset with immutable, zero-copy versions. A new version is
pinned from an uploaded run artifact; versions are never edited in place.

1. Call `research_resolve` with the intended purpose, kind, and compatibility requirement. Inspect the matched asset and its versions. `state: "no_match"` is a real answer, not a failure — it is what licenses step 4.
2. If an exact compatible version exists, materialize its pinned bytes with `probe asset materialize NAME --to PATH`. Record consumption; do not copy it into a new identity.
3. If the purpose is the same but the content must change, produce the new content, upload it as a run artifact (`probe artifact add RUN PATH`), then pin it as a new version of the SAME asset: `probe asset version ASSET_ID --from-artifact ARTIFACT_ID --label LABEL`. Use `research_compare` to show the diff/compatibility impact.
4. If no compatible identity exists, register a new asset (`probe asset register NAME --kind KIND`) and pin its first version. Only start a new identity when resolution returned no compatible match; record the concrete reason in the experiment.
5. For datasets, pin provenance in the version meta: input asset versions, the transform script version, parameters, schema/statistics, and the output content hash.
6. Validate the change (normalized diff, compatibility impact, tests/evaluations) and record it. Marking an experiment or version as the published record is the separate publish-experiment workflow — never encode "official" as a filename or run-metadata flag.

## Tracing a path, URI, or content hash

To find what produced or consumed a file, search for it: `research_search` with the path,
URI, artifact id, or content hash as the query and `collapse=null` (its exact channel matches
artifacts directly). Then follow `research_get view="lineage"` on the run that owns the hit.

There is no `research_trace_file` tool. It was removed rather than fixed: no backend trace
index has ever existed, so it answered "no matches" to every query, and "this file has no
lineage" is a far more damaging answer than "I could not find it". If you cannot establish
provenance, say that — do not infer absence from an empty result.
