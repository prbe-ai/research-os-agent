---
name: manage-research-asset
description: Reuse, inspect, materialize, or version shared research assets such as scripts, scoring methods, datasets, transforms, configs, container or SIF definitions, checkpoints, and templates. Use before copying or creating a reusable asset, or whenever an existing asset needs changes, to reduce duplicate identities and preserve version lineage.
---

# Manage a research asset

The registry is a named asset with immutable, zero-copy versions. A new version is
pinned from an uploaded run artifact; versions are never edited in place.

1. Call `get_entity(ref="asset:<name>", view="versions")`, adding `filters={"requirement": ">=N"}` when you need a specific version floor. Read the result carefully — the two empty answers mean opposite things:
   - **The name does not exist** → an error, like any other bad ref. That licenses step 4.
   - **`state: "no_match"`** → the asset EXISTS and no version satisfies your requirement. The response carries the versions that DO exist, so you are looking at a real version ceiling. That licenses step 3 (pin a new version of the same asset), NOT step 4.
   Confusing the two is how duplicate asset identities get created, which is the single most expensive avoidable error here: two assets with the same intent and different behaviour make every result that used either one unreproducible.
   Version requirements match monotonic integers and labels (`>=3`, `<2`, `v1.4-final`), not semver ranges.
2. If an exact compatible version exists, materialize its pinned bytes with `probe asset materialize NAME --to PATH`. Record consumption; do not copy it into a new identity.
3. If the purpose is the same but the content must change, produce the new content, upload it as a run artifact (`probe artifact add RUN PATH` — the file is streamed to storage, so multi-GB model weights or datasets upload without being read into memory), then pin it as a new version of the SAME asset: `probe asset version ASSET_ID --from-artifact ARTIFACT_ID --label LABEL`. Read the asset's versions before and after to show the diff/compatibility impact.
4. If no compatible identity exists, register a new asset (`probe asset register NAME --kind KIND`) and pin its first version. Only start a new identity when resolution returned no compatible match; record the concrete reason in the experiment.
5. For datasets, pin provenance in the version meta: input asset versions, the transform script version, parameters, schema/statistics, and the output content hash.
6. Validate the change (normalized diff, compatibility impact, tests/evaluations) and record it. Marking an experiment or version as the published record is the separate publish-experiment workflow — never encode "official" as a filename or run-metadata flag.

## Tracing a path, URI, or content hash

To find what produced or consumed a file, search for it: `search_knowledge` with the path,
URI, artifact id, or content hash as the query and `collapse=null` (its exact channel matches
artifacts directly). Then follow `get_entity(view="lineage")` on the run that owns the hit.

There is no trace-file tool. It was removed rather than fixed: no backend trace
index has ever existed, so it answered "no matches" to every query, and "this file has no
lineage" is a far more damaging answer than "I could not find it". If you cannot establish
provenance, say that — do not infer absence from an empty result.
