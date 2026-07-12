.PHONY: install test dump-openapi gen-models regen

install:
	pip install -e ".[dev]"

test:
	pytest

# Snapshot the backend contract. Point RESEARCH_OS at a checkout with deps installed.
dump-openapi:
	RESEARCH_OS=$${RESEARCH_OS:-../../research-os} python scripts/dump_openapi.py

# Regenerate typed models from schema/openapi.json.
gen-models:
	python scripts/gen_models.py

# Full refresh: pull the latest schema, then regenerate models.
regen: dump-openapi gen-models

# Keep the plugin's skill copies in sync with the canonical top-level skills/.
sync-plugin-skills:
	@for s in track-experiment manage-research-asset publish-experiment; do \
	  rm -rf plugins/research-os/skills/$$s; mkdir -p plugins/research-os/skills/$$s; \
	  cp -R skills/$$s/. plugins/research-os/skills/$$s/; done
	@echo "synced skills -> plugins/research-os/skills"
