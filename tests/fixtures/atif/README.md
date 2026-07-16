# ATIF trajectory fixtures

Golden `*.trajectory.json` files copied verbatim from
[laude-institute/harbor](https://github.com/laude-institute/harbor)
`tests/golden/terminus_2/` (Apache-2.0) — real ATIF v1.6/v1.7 documents
produced by the terminus-2 agent, used here to prove `probe.connectors.atif`
parses the format Harbor actually emits, not our idea of it.

`synthetic-subagent.trajectory.json` is ours: an ATIF-v1.7 document with an
embedded subagent trajectory (no golden fixture exercises that), validated
against Harbor's own pydantic models before being committed.
