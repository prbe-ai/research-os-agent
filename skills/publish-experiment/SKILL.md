---
name: publish-experiment
description: Validate and explicitly publish a completed research experiment or candidate asset as official. Use only when the researcher asks to mark, promote, publish, or approve results; inspect the exact manifest, completeness, deviations, selected artifacts, and asset changes before mutating organizational truth.
---

# Publish an experiment

1. Call `research_get` with `view="reproduce"` and `view="handoff"`. Verify the hypothesis, lifecycle outcome, code/environment snapshot, exact evaluation procedure, selected artifacts, resolved asset hashes, and missing prerequisites.
2. Compare candidate asset versions with their pinned bases and show source/data diffs, validation evidence, compatibility impact, and known consumers.
3. Present the exact experiment and asset set that would become official. Obtain explicit researcher approval for that set; metrics or exit status are not approval.
4. If `promotion_manifests` or `versioned_assets` is unavailable, stop and preserve a candidate handoff. Never encode “official” as an ordinary metadata flag.
5. Promote approved candidate assets with `exp asset promote CANDIDATE --approval TEXT`, then publish the immutable experiment manifest with `exp promote RUN --approval TEXT --asset CANDIDATE`.

Never publish an incomplete or materially changed set that differs from what the researcher approved.
