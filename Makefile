.PHONY: install test test-tap test-codex-tap verify-codex-pre-release parity dump-openapi gen-models regen regen-mcp-schema sync-plugin-skills sync-plugin-policy sync-plugin

install:
	pip install -e ".[dev]"

test: test-tap test-codex-tap
	# Bare `pytest` works: `pythonpath = ["."]` in pyproject puts the repo root on
	# sys.path, so `tests.conftest` imports without the `python -m` trick (which CI,
	# editors, and a plain `pytest` invocation do not use).
	pytest

# probe-research-tap plugin tests. The plugin is stdlib-only and carries its own
# pyproject (pytest picks it as rootdir/inifile when given this path), so these
# don't collect under the root `pytest` run and need their own invocation.
test-tap:
	pytest -q plugins/probe-research-tap/tests

# Codex rollout adapter + hook contract. Kept isolated because the package is
# also named `tap`, like the Claude adapter above.
test-codex-tap:
	PROBE_TAP_SOURCE=codex pytest -q plugins/probe-research-tap/tests/test_codex_sanitize.py plugins/probe-research-tap/tests/test_codex_research_os_contract.py

# Non-deploying Codex release gate: validates both plugin packages, installs
# them with the real Codex CLI in an isolated home, and runs the cross-repo
# contract suites. Point RESEARCH_OS at the backend checkout.
verify-codex-pre-release:
	python scripts/verify_codex_pre_release.py --backend "$${RESEARCH_OS:?set RESEARCH_OS to the research-os checkout}"

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

# Re-capture the MCP tool-schema baseline that tests/test_mcp_threading.py pins.
# Run this whenever a tool's SIGNATURE or DOCSTRING changes; never hand-edit the
# fixture.
#
# PYTHONPATH is the whole point. Worktrees sometimes run against the primary
# checkout's interpreter, and a bare `import probe.mcp.server` then resolves to
# the INSTALLED package, not your edits. You would snapshot the OLD schema, the
# pin would compare old to old, and the suite would go green while the shipped
# tool schema was never checked. Forcing the source tree onto the front of
# sys.path is what makes that impossible. Override PY= to point at a different
# interpreter.
PY ?= python
regen-mcp-schema:
	@PYTHONPATH=$(CURDIR)/src $(PY) -c "import anyio, json, probe.mcp.server as m; \
	  assert m.__file__.startswith('$(CURDIR)/src'), 'refusing to snapshot %s -- not this tree' % m.__file__; \
	  t = anyio.run(m.create_server().list_tools); \
	  open('tests/fixtures/mcp_tool_schemas.json','w').write(json.dumps( \
	  sorted((x.model_dump(mode='json', exclude_none=True) for x in t), \
	  key=lambda d: d['name']), indent=2, sort_keys=True) + chr(10))"
	@echo "re-captured tests/fixtures/mcp_tool_schemas.json from $(CURDIR)/src"

# Keep the plugin's skill copies in sync with the canonical top-level skills/.
# Edit skills/, never the plugin copy. tests/test_skills_sync.py fails if they drift,
# so a forgotten sync is caught by CI (and blocks the MCP deploy) instead of silently
# shipping a plugin that teaches the old thing. Adding a skill? Update this list AND
# _SYNCED in that test.
sync-plugin-skills:
	@for s in start-research-work track-research-work capture-run-inputs show-research-timeline; do \
	  rm -rf plugins/probe-research/skills/$$s; mkdir -p plugins/probe-research/skills/$$s; \
	  cp -R skills/$$s/. plugins/probe-research/skills/$$s/; done
	@echo "synced skills -> plugins/probe-research/skills"

# Keep the plugin's copy of the shared version policy in sync with the canonical
# src/probe/version_policy.py. Same contract as the skills above: edit the
# canonical file, never the plugin copy, and tests/test_policy_sync.py fails when
# they drift.
#
# The copy exists because the hook cannot import it. session-start.sh runs
# version_check.py under the SYSTEM python3, which has no probe package on its
# path, so a shared import is impossible and a shipped duplicate is the only
# option. `import version_policy` resolves to this sibling because sys.path[0] is
# the script's own directory.
sync-plugin-policy:
	@cp src/probe/version_policy.py plugins/probe-research/hooks/version_policy.py
	@echo "synced version_policy -> plugins/probe-research/hooks"

sync-plugin: sync-plugin-skills sync-plugin-policy
