#!/usr/bin/env python3
"""Non-deploying, cross-repository Codex release gate.

This intentionally stops before publish/deploy/tag. The deployed canary remains
the separate final gate in ``verify_codex_live.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("probe-research", "probe-research-tap")


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)  # noqa: S603


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    args = parser.parse_args()
    backend = args.backend.expanduser().resolve()
    if not (backend / "app").is_dir():
        parser.error(f"not a research-os checkout: {backend}")

    agent_venv_python = ROOT / ".venv/bin/python"
    agent_python = str(agent_venv_python if agent_venv_python.is_file() else Path(sys.executable))

    run(
        [
            agent_python,
            "-m",
            "pytest",
            "-q",
            "tests/test_codex_plugins.py",
            "tests/test_verify_codex_live.py",
            "tests/test_agent_session_headers.py",
            "tests/test_setup_wizard.py",
            "tests/test_updater.py",
        ]
    )
    run([agent_python, "-m", "pytest", "-q", "plugins/probe-research-tap/tests"])
    run(["npm", "test"], cwd=ROOT / "npm")

    backend_python = backend / ".venv/bin/python"
    python = str(backend_python if backend_python.is_file() else Path(sys.executable))
    run(
        [
            python,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_schema_parity.py",
            "tests/integration/test_pairing_api.py",
            "tests/integration/test_claude_code_ingest_api.py",
            "tests/integration/test_device_auth_grants.py",
            "tests/integration/test_migrate_authorization_grants.py",
            "tests/integration/test_client_installations.py",
            "tests/integration/test_agent_session_linking.py",
            "tests/integration/test_search_api.py",
            "tests/integration/test_ingestion_stats_api.py",
            "tests/integration/test_session_transcript_read.py",
        ],
        cwd=backend,
    )

    dashboard = backend / "dashboard"
    run(
        [
            "npm",
            "test",
            "--",
            "--run",
            "src/app/sessions/[id]/page.test.tsx",
            "src/lib/session-transcript.test.ts",
            "src/lib/api/sessions.test.ts",
            "src/lib/search-results.test.ts",
            "src/components/install/install-flow.test.ts",
            "src/components/connect/pair-claude-code.test.tsx",
            "src/components/connect/ingestion-stats.test.tsx",
            "src/lib/scopes.grants.test.ts",
        ],
        cwd=dashboard,
    )
    run(
        [
            "npx",
            "eslint",
            "src/app/authorize/page.tsx",
            "src/app/sessions/[id]/page.tsx",
            "src/components/connect/ingestion-stats.tsx",
            "src/components/connect/pair-claude-code.tsx",
            "src/components/install/claude-code-setup.tsx",
            "src/components/install/install-flow.ts",
            "src/lib/constants.ts",
            "src/lib/api/sessions.ts",
            "src/lib/scopes.ts",
            "src/lib/search-results.ts",
            "src/lib/session-transcript.ts",
            "src/lib/types.ts",
        ],
        cwd=dashboard,
    )

    codex = shutil.which("codex")
    if not codex:
        parser.error("codex CLI is not on PATH")
    with tempfile.TemporaryDirectory(prefix="probe-codex-preflight-") as temp:
        env = {**os.environ, "CODEX_HOME": temp}
        run([codex, "plugin", "marketplace", "add", str(ROOT)], env=env)
        for plugin in PLUGINS:
            run([codex, "plugin", "add", f"{plugin}@research-os-agent"], env=env)
        completed = subprocess.run(  # noqa: S603
            [codex, "plugin", "list", "--json"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        installed = json.loads(completed.stdout)
        snapshot = json.dumps(installed)
        missing = [plugin for plugin in PLUGINS if plugin not in snapshot]
        if missing:
            raise RuntimeError(f"Codex did not report installed plugins: {missing}")

    print("Codex pre-release gate passed. No release or deployment action was taken.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
