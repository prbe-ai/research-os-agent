---
name: publish-experiment
description: Validate and explicitly publish a completed research experiment as an immutable version, pinning any approved reusable results as asset versions. Use only when the researcher asks to mark, publish, or approve results; inspect the exact reproduction manifest, completeness, deviations, selected artifacts, and asset changes before mutating organizational truth.
---

# Publish an experiment

The published record is an immutable **experiment version** (a launch-time manifest of
the experiment's runs) plus any reusable results pinned as immutable **asset versions**.
There is no separate "official" flag or run-level promotion.

1. Call `get_entity` with `view="reproduce"` on each candidate run. Verify the hypothesis, the code/environment snapshot (`env_ref` must resolve to an execution record — `missing: ["execution_record"]` means the run captured none and CANNOT be reproduced, which disqualifies it), and the config. Add `view="handoff"` for lifecycle outcome and series, `view="artifacts"` for the selected outputs, and `view="trajectory"` when the claim depends on what the run actually did rather than on its final numbers.
2. Check `view="versions"` on the experiment first — if a version already covers this set, do not mint a second. Compare candidate asset versions with their pinned bases by reading `get_entity(ref="asset:<name>", view="versions")` for each and diffing them: source/data diffs, validation evidence, compatibility impact, and known consumers.
3. Present the exact experiment + asset versions that would become the published record. Obtain explicit researcher approval for that set; metrics or exit status are not approval.
4. Pin any approved reusable results as asset versions from their run artifacts: `probe asset version ASSET_ID --from-artifact ARTIFACT_ID --label LABEL`.
5. Mint the immutable experiment version manifest: `probe version create EXPERIMENT_ID --label LABEL`. Report the created version.

Never publish an incomplete or materially changed set that differs from what the researcher
approved. A `completeness.state` of `"partial"` on any view you based the decision on means
you have not seen the whole record — resolve it (follow `next_cursor`, or raise the
`token_budget`) or say so before asking for approval. Publication mutates organizational
truth; a partial read is not a basis for it.
