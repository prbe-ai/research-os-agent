---
name: publish-experiment
description: Validate and explicitly publish a completed research experiment as an immutable version, pinning any approved reusable results as asset versions. Use only when the researcher asks to mark, publish, or approve results; inspect the exact reproduction manifest, completeness, deviations, selected artifacts, and asset changes before mutating organizational truth.
---

# Publish an experiment

The published record is an immutable **experiment version** (a launch-time manifest of
the experiment's runs) plus any reusable results pinned as immutable **asset versions**.
There is no separate "official" flag or run-level promotion.

1. Call `research_get` with `view="reproduce"` and `view="handoff"`. Verify the hypothesis, lifecycle outcome, code/environment snapshot (`env_ref`), exact evaluation procedure, selected artifacts, resolved asset versions, and missing prerequisites.
2. Compare candidate asset versions with their pinned bases (`research_compare`): source/data diffs, validation evidence, compatibility impact, and known consumers.
3. Present the exact experiment + asset versions that would become the published record. Obtain explicit researcher approval for that set; metrics or exit status are not approval.
4. Pin any approved reusable results as asset versions from their run artifacts: `probe asset version ASSET_ID --from-artifact ARTIFACT_ID --label LABEL`.
5. Mint the immutable experiment version manifest: `probe version create EXPERIMENT_ID --label LABEL`. Report the created version.

Never publish an incomplete or materially changed set that differs from what the researcher approved.
