---
name: manage-research-asset
description: Reuse, inspect, materialize, modify, derive, or propose shared research assets such as scripts, scoring methods, datasets, transforms, configs, container or SIF definitions, checkpoints, and templates. Use before copying or creating a reusable asset, or whenever an existing asset needs changes, to reduce duplicate identities and preserve version lineage.
---

# Manage a research asset

1. Call `research_resolve` with the intended purpose, kind, and compatibility requirement. Inspect exact, candidate, and near-match results.
2. If an exact compatible version exists, materialize its pinned ref with `exp asset materialize ASSET_REF --run RUN_ID --to PATH`. Record consumption; do not copy it into a new identity.
3. If the purpose is the same but content must change, call `research_compare` as needed, then fork the pinned base with `exp asset fork`. Never edit the immutable version.
4. Validate the change. For scripts and methods, capture the normalized diff, compatibility impact, and tests/evaluations. For datasets, pin every input version, the transform script version, parameters, schema/statistics, and output manifest.
5. Propose the result with `exp asset propose --base BASE_REF`. Use no base only when resolution returned no compatible identity, and supply a concrete new-identity reason.
6. Leave the proposal as a candidate. Do not move an official pointer without explicit publication approval.

If the MCP capability envelope reports `versioned_assets: false`, do not imitate an asset registry with filenames or run metadata. Record the intended reuse/deviation in the experiment and report that registry operations are unavailable.
