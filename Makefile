.PHONY: install test parity dump-openapi gen-models regen sync-plugin-skills

install:
	pip install -e ".[dev]"

test:
	# Bare `pytest` works: `pythonpath = ["."]` in pyproject puts the repo root on
	# sys.path, so `tests.conftest` imports without the `python -m` trick (which CI,
	# editors, and a plain `pytest` invocation do not use).
	pytest

# Contract guard: every route in schema/openapi.json must be reachable from a client
# method, or be explicitly allowlisted. Run by `regen` so a schema refresh that adds a
# backend route fails here instead of shipping a client that silently cannot call it.
parity:
	pytest tests/test_parity.py -q

# Snapshot the backend contract. Point RESEARCH_OS at a checkout with deps installed.
dump-openapi:
	RESEARCH_OS=$${RESEARCH_OS:-../../research-os} python scripts/dump_openapi.py

# Regenerate typed models from schema/openapi.json.
gen-models:
	python scripts/gen_models.py

# Full refresh: pull the latest schema, regenerate models, then prove the client can
# still reach everything the backend now declares. The parity step is the point: before
# it existed, a new backend route regenerated a model and nothing failed, so nobody
# noticed the client had no way to call it.
regen: dump-openapi gen-models parity

# Keep the plugin's skill copies in sync with the canonical top-level skills/.
# Edit skills/, never the plugin copy. tests/test_skills_sync.py fails if they drift,
# so a forgotten sync is caught by CI (and blocks the MCP deploy) instead of silently
# shipping a plugin that teaches the old thing. Adding a skill? Update this list AND
# _SYNCED in that test.
sync-plugin-skills:
	@for s in track-experiment manage-research-asset publish-experiment; do \
	  rm -rf plugins/probe-research/skills/$$s; mkdir -p plugins/probe-research/skills/$$s; \
	  cp -R skills/$$s/. plugins/probe-research/skills/$$s/; done
	@echo "synced skills -> plugins/probe-research/skills"
