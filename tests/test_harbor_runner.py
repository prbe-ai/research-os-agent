"""harbor_runner: the fork-free bridge for Harbor's per-trial hook surface.

Three layers, mirroring the module's own risk profile:

  * PROTOCOL unit tests — the ``SandboxStateRecorder`` hook bodies against a
    fake trial/environment that emulates the container with a host directory.
    No harbor import needed (by design: the recorder never touches harbor),
    so the fail-open rules, ephemerality, integrity verification, and
    partial-bundle behavior are always exercised in CI.
  * CONTRACT canary — ``verify_harbor_contract()`` against the actually
    installed harbor.  The hook API is undocumented upstream; this is the
    test the design doc requires so an upgrade breaks loudly.
  * REAL integration — a real ``harbor`` Docker sandbox run through
    ``run_trial`` with the oracle agent (no LLM keys): asserts the begin/end
    bundle lands in the trial dir, integrity verifies, the delta contains
    exactly the file the agent wrote, and the verifier is unaffected.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe.connectors.harbor_runner import (
    SandboxStateOptions,
    SandboxStateRecorder,
)
from probe.connectors.sandbox_state import (
    BEGIN_BYTES,
    BEGIN_MANIFEST,
    BUNDLE_DIRNAME,
    END_DELTA,
    END_MANIFEST,
    TRAILER_PREFIX,
    TRAILER_SCHEMA,
)


# ---------------------------------------------------------------------------
# Fake container: host-directory filesystem + programmable snapshot behavior.
# ---------------------------------------------------------------------------
class FakeSandbox:
    """Emulates BaseEnvironment.exec/upload_file/download_file over a host dir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []
        self.fail_begin_exec = False
        self.fail_end_exec = False
        self.lie_in_trailer = False
        self.cancel_on_end = False

    def _host(self, container_path: str) -> Path:
        return self.root / container_path.lstrip("/")

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        self.calls.append(f"upload {target_path}")
        dest = self._host(target_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, dest)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        self.calls.append(f"download {source_path}")
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._host(source_path), target_path)

    async def exec(self, command: str, timeout_sec=None, user=None, **_):
        self.calls.append(f"exec[{user}] {command.split()[0]} {command.split()[1] if len(command.split()) > 1 else ''}")
        if command.startswith("uname"):
            return SimpleNamespace(stdout="x86_64\n", stderr="", return_code=0)
        if command.startswith("mkdir -p "):
            self._host(command.split()[-1]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="", stderr="", return_code=0)
        if command.startswith("chmod"):
            return SimpleNamespace(stdout="", stderr="", return_code=0)
        if command.startswith("rm -rf "):
            shutil.rmtree(self._host(command.split()[-1]), ignore_errors=True)
            return SimpleNamespace(stdout="", stderr="", return_code=0)
        if " begin " in command:
            if self.fail_begin_exec:
                return SimpleNamespace(stdout="", stderr="boom", return_code=1)
            return self._run_snapshot(command, phase="begin")
        if " end " in command:
            if self.cancel_on_end:
                raise asyncio.CancelledError
            if self.fail_end_exec:
                return SimpleNamespace(stdout="", stderr="boom", return_code=1)
            return self._run_snapshot(command, phase="end")
        raise AssertionError(f"unexpected command: {command}")

    def _run_snapshot(self, command: str, *, phase: str) -> SimpleNamespace:
        workdir = self._host(command.split("--workdir")[1].split()[0])
        # Deliberately UNSORTED lines: the host-side bundle write must sort.
        lines = [b'{"p": "/workspace/z.py", "t": "f", "s": 2}',
                 b'{"p": "/workspace/a.py", "t": "f", "s": 1}']
        files: dict[str, Path] = {}
        stats: dict[str, int] = {"entries": 2}
        if phase == "begin":
            files[BEGIN_MANIFEST] = workdir / BEGIN_MANIFEST
            files[BEGIN_MANIFEST].write_bytes(gzip.compress(b"\n".join(lines) + b"\n"))
            if "--bytes" in command:
                # Mirrors the real binary: --bytes tees the walk into an archive
                # and records the budget in the trailer stats.
                files[BEGIN_BYTES] = workdir / BEGIN_BYTES
                files[BEGIN_BYTES].write_bytes(gzip.compress(b"begin-archive-bytes"))
                stats["begin_bytes_budget_bytes"] = 1024
        else:
            assert (workdir / "begin.jsonl.gz").is_file(), "begin manifest not re-uploaded"
            files[END_MANIFEST] = workdir / END_MANIFEST
            files[END_MANIFEST].write_bytes(gzip.compress(b"\n".join(lines) + b"\n"))
            files[END_DELTA] = workdir / END_DELTA
            files[END_DELTA].write_bytes(gzip.compress(b"delta-bytes"))
        trailer = {
            "schema": TRAILER_SCHEMA,
            "phase": phase,
            "files": {
                name: {
                    "sha256": (
                        "0" * 64
                        if self.lie_in_trailer
                        else hashlib.sha256(path.read_bytes()).hexdigest()
                    ),
                    "size_bytes": path.stat().st_size,
                }
                for name, path in files.items()
            },
            "stats": stats,
            "hash_mode": "fast",
            "errors": [],
        }
        return SimpleNamespace(
            stdout=f"noise\n{TRAILER_PREFIX}{json.dumps(trailer)}\n",
            stderr="",
            return_code=0,
        )

    def leftover_workdirs(self) -> list[Path]:
        tmp = self.root / "tmp"
        return sorted(tmp.glob(".psbx-*")) if tmp.is_dir() else []


def _fake_trial(tmp_path: Path) -> SimpleNamespace:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    return SimpleNamespace(
        agent_environment=FakeSandbox(tmp_path / "container"),
        paths=SimpleNamespace(trial_dir=trial_dir),
        config=SimpleNamespace(trials_dir=tmp_path, trial_name="trial"),
    )


def _recorder(trial, **overrides) -> SandboxStateRecorder:
    return SandboxStateRecorder(trial, SandboxStateOptions(**overrides))


class TestSandboxStateProtocol:
    def test_happy_path_bundle_complete_and_container_probe_free(self, tmp_path):
        trial = _fake_trial(tmp_path)
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        assert rec.status["begin"] == "ok"
        assert trial.agent_environment.leftover_workdirs() == []  # probe-free window
        asyncio.run(rec.on_agent_end())
        assert rec.status["end"] == "ok"
        assert rec.integrity == {"begin_verified": True, "end_verified": True}

        bundle = trial.paths.trial_dir / "artifacts" / BUNDLE_DIRNAME
        assert rec.bundle_dir == bundle
        assert {p.name for p in bundle.iterdir()} == {
            BEGIN_MANIFEST,
            END_MANIFEST,
            END_DELTA,
            "meta.json",
        }
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["schema"] == "probe.sandbox-state/1"
        assert meta["integrity"] == {"begin_verified": True, "end_verified": True}
        assert meta["errors"] == []
        # Manifests are sorted bytewise host-side (fake emitted z before a).
        with gzip.open(bundle / BEGIN_MANIFEST) as handle:
            paths = [json.loads(line)["p"] for line in handle]
        assert paths == sorted(paths)
        # container fully clean after end
        assert trial.agent_environment.leftover_workdirs() == []

    def test_begin_failure_is_recorded_and_no_bundle_is_written(self, tmp_path):
        trial = _fake_trial(tmp_path)
        trial.agent_environment.fail_begin_exec = True
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())  # must NOT raise — fail-open
        assert rec.status["begin"] == "failed"
        assert any("begin" in e for e in rec.errors)
        asyncio.run(rec.on_agent_end())
        assert rec.status["end"] == "failed"
        assert rec.bundle_dir is None
        assert not (trial.paths.trial_dir / "artifacts" / BUNDLE_DIRNAME).exists()
        assert trial.agent_environment.leftover_workdirs() == []

    def test_end_failure_writes_partial_but_honest_bundle(self, tmp_path):
        trial = _fake_trial(tmp_path)
        trial.agent_environment.fail_end_exec = True
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())
        assert rec.status == {"begin": "ok", "end": "failed"}
        bundle = trial.paths.trial_dir / "artifacts" / BUNDLE_DIRNAME
        assert {p.name for p in bundle.iterdir()} == {BEGIN_MANIFEST, "meta.json"}
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["integrity"]["end_verified"] is False
        assert any("end" in e for e in meta["errors"])

    def test_trailer_mismatch_flags_integrity_but_keeps_bundle(self, tmp_path):
        trial = _fake_trial(tmp_path)
        trial.agent_environment.lie_in_trailer = True
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())
        assert rec.status == {"begin": "ok", "end": "ok"}
        assert rec.integrity == {"begin_verified": False, "end_verified": False}
        meta = json.loads((rec.bundle_dir / "meta.json").read_text())
        assert any("sha256 mismatch" in e for e in meta["errors"])

    def test_cancelled_error_propagates_uncaught(self, tmp_path):
        trial = _fake_trial(tmp_path)
        trial.agent_environment.cancel_on_end = True
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(rec.on_agent_end())

    def test_begin_bytes_captured_rides_bundle_and_meta(self, tmp_path):
        trial = _fake_trial(tmp_path)
        rec = _recorder(trial, begin_bytes=True, begin_bytes_ref="task-abc123")
        asyncio.run(rec.on_agent_start())
        assert rec.status["begin"] == "ok"
        assert rec.integrity["begin_verified"] is True
        assert trial.agent_environment.leftover_workdirs() == []  # still probe-free
        asyncio.run(rec.on_agent_end())

        bundle = trial.paths.trial_dir / "artifacts" / BUNDLE_DIRNAME
        assert (bundle / BEGIN_BYTES).is_file()
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["begin_bytes"]["captured"] is True
        assert meta["begin_bytes"]["ref"] == "task-abc123"
        assert meta["begin_bytes"]["budget_bytes"] == 1024

    def test_begin_bytes_ref_without_capture_stamps_meta(self, tmp_path):
        trial = _fake_trial(tmp_path)
        rec = _recorder(trial, begin_bytes_ref="task-abc123")
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())

        bundle = trial.paths.trial_dir / "artifacts" / BUNDLE_DIRNAME
        assert not (bundle / BEGIN_BYTES).exists()
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["begin_bytes"]["captured"] is False
        assert meta["begin_bytes"]["ref"] == "task-abc123"

    def test_meta_has_no_begin_bytes_block_by_default(self, tmp_path):
        trial = _fake_trial(tmp_path)
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())
        meta = json.loads((rec.bundle_dir / "meta.json").read_text())
        assert "begin_bytes" not in meta

    def test_snapshot_command_plumbs_root_and_bytes(self, tmp_path):
        trial = _fake_trial(tmp_path)
        rec = _recorder(trial, root="/repo", begin_bytes=True, max_begin_bytes=123456)
        begin_cmd = rec._snapshot_command("/tmp/.psbx-x", phase="begin")
        assert "--root /repo" in begin_cmd
        assert "--bytes" in begin_cmd
        assert "--max-begin-bytes 123456" in begin_cmd
        end_cmd = rec._snapshot_command("/tmp/.psbx-x", phase="end")
        assert "--root /repo" in end_cmd
        assert "--bytes" not in end_cmd

        plain_root = tmp_path / "plain"
        plain_root.mkdir()
        plain = _recorder(_fake_trial(plain_root))
        assert "--root" not in plain._snapshot_command("/tmp/.psbx-y", phase="begin")

    def test_begin_timeout_resolves_by_bytes_mode(self):
        assert SandboxStateOptions().resolved_begin_timeout_sec() == 120.0
        assert SandboxStateOptions(begin_bytes=True).resolved_begin_timeout_sec() == 600.0
        assert (
            SandboxStateOptions(begin_bytes=True, begin_timeout_sec=45.0).resolved_begin_timeout_sec()
            == 45.0
        )

    def test_missing_environment_is_fail_open(self, tmp_path):
        trial = _fake_trial(tmp_path)
        trial.agent_environment = None
        rec = _recorder(trial)
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())
        assert rec.status == {"begin": "failed", "end": "failed"}
        assert rec.bundle_dir is None


# ---------------------------------------------------------------------------
# Contract canary — requires harbor installed; the loud-upgrade tripwire.
# ---------------------------------------------------------------------------
class TestHarborContract:
    def test_installed_harbor_matches_pinned_surface(self):
        pytest.importorskip("harbor", reason="harbor not installed")
        from probe.connectors.harbor_runner import verify_harbor_contract

        assert verify_harbor_contract() == []


# ---------------------------------------------------------------------------
# Real integration — harbor + Docker, oracle agent, no LLM keys.
# ---------------------------------------------------------------------------
def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20, check=False
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


def _write_echo_task(root: Path) -> Path:
    task = root / "echo-reward"
    (task / "environment").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "tests").mkdir()
    (task / "instruction.md").write_text(
        "Write the exact string `probe-rl` to `/app/answer.txt`.\n"
    )
    (task / "task.toml").write_text(
        'schema_version = "1.3"\n'
        "artifacts = []\n\n"
        "[task]\n"
        'name = "probe/echo-reward"\n\n'
        "[metadata]\n\n"
        "[verifier]\n"
        "timeout_sec = 120.0\n\n"
        "[agent]\n"
        "timeout_sec = 120.0\n\n"
        "[environment]\n"
        'network_mode = "public"\n'
        "build_timeout_sec = 600.0\n"
        'os = "linux"\n'
    )
    (task / "environment" / "Dockerfile").write_text(
        "FROM ubuntu:24.04\n\nWORKDIR /app\n"
    )
    (task / "solution" / "solve.sh").write_text(
        '#!/bin/bash\necho "probe-rl" > /app/answer.txt\n'
    )
    (task / "tests" / "test.sh").write_text(
        "#!/bin/bash\nmkdir -p /logs/verifier\n"
        'if [ -f /app/answer.txt ] && grep -q "probe-rl" /app/answer.txt; then\n'
        '  echo "0.85" > /logs/verifier/reward.txt\nelse\n'
        '  echo "0" > /logs/verifier/reward.txt\nfi\n'
    )
    return task


@pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable")
class TestRealHarborLibraryMode:
    def test_run_trial_captures_sandbox_state_end_to_end(self, tmp_path, monkeypatch):
        pytest.importorskip("harbor", reason="harbor not installed")
        # The suite's isolation fixture redirects HOME, which hides Docker
        # Desktop's compose plugin (~/.docker/cli-plugins). Point DOCKER_CONFIG
        # at the real one; probe's own config isolation stays intact.
        import os
        import pwd

        real_docker_config = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"
        if real_docker_config.is_dir():
            monkeypatch.setenv("DOCKER_CONFIG", str(real_docker_config))
        from probe.connectors.harbor import parse_trial
        from probe.connectors.harbor_runner import run_trial

        outcome = asyncio.run(
            run_trial(
                _write_echo_task(tmp_path / "tasks"),
                trials_dir=tmp_path / "trials",
                agent="oracle",
            )
        )

        # The trial itself is unaffected by the instrumentation.
        assert outcome.result.exception_info is None, outcome.result.exception_info
        rewards = outcome.result.verifier_result.rewards
        assert rewards["reward"] == pytest.approx(0.85)

        # The bundle is complete, verified, and where the contract says.
        rec = outcome.sandbox_state
        assert rec is not None
        assert rec.status == {"begin": "ok", "end": "ok"}
        assert rec.integrity == {"begin_verified": True, "end_verified": True}
        bundle = outcome.trial_dir / "artifacts" / BUNDLE_DIRNAME
        assert rec.bundle_dir == bundle
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["schema"] == "probe.sandbox-state/1"
        assert meta["integrity"] == {"begin_verified": True, "end_verified": True}

        # The delta contains exactly the agent's footprint: answer.txt exists
        # in the end manifest and rides the delta tar; /logs bytes are excluded.
        with tarfile.open(bundle / END_DELTA) as tar:
            members = tar.getnames()
        assert any(m.lstrip("/").rstrip("/") == "app/answer.txt" for m in members), members
        assert not any(m.lstrip("/").startswith("logs/") for m in members)
        with gzip.open(bundle / END_MANIFEST) as handle:
            end_paths = {json.loads(line)["p"] for line in handle}
        assert "/app/answer.txt" in end_paths

        # The bundle is ordinary trial-tree bytes: parse_trial carries it.
        parsed = parse_trial(outcome.trial_dir)
        parsed_rel = {
            p.relative_to(parsed.trial_dir).as_posix() for p in parsed.files
        }
        for name in (BEGIN_MANIFEST, END_MANIFEST, END_DELTA, "meta.json"):
            assert f"artifacts/{BUNDLE_DIRNAME}/{name}" in parsed_rel

    def test_run_trial_with_begin_bytes_recovers_before_content(
        self, tmp_path, monkeypatch
    ):
        """The begin-bytes slice against REAL harbor: the archive rides the
        bundle, meta carries the sharing block, and the pre-agent bytes of the
        sandbox are recoverable and hash-verified against the begin manifest —
        while the trial itself (verifier, reward) is untouched."""
        pytest.importorskip("harbor", reason="harbor not installed")
        import hashlib as _hashlib
        import os
        import pwd

        real_docker_config = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"
        if real_docker_config.is_dir():
            monkeypatch.setenv("DOCKER_CONFIG", str(real_docker_config))
        from probe.connectors.harbor_runner import SandboxStateOptions, run_trial
        from probe.connectors.sandbox_state import BEGIN_BYTES as _BB

        outcome = asyncio.run(
            run_trial(
                _write_echo_task(tmp_path / "tasks"),
                trials_dir=tmp_path / "trials",
                agent="oracle",
                options=SandboxStateOptions(
                    begin_bytes=True,
                    begin_bytes_ref="task-echo-reward-ci",
                    hash_files=True,
                ),
            )
        )
        assert outcome.result.exception_info is None, outcome.result.exception_info
        assert outcome.result.verifier_result.rewards["reward"] == pytest.approx(0.85)

        rec = outcome.sandbox_state
        assert rec.status == {"begin": "ok", "end": "ok"}
        assert rec.integrity == {"begin_verified": True, "end_verified": True}
        bundle = outcome.trial_dir / "artifacts" / BUNDLE_DIRNAME
        meta = json.loads((bundle / "meta.json").read_text())
        assert meta["begin_bytes"]["captured"] is True
        assert meta["begin_bytes"]["ref"] == "task-echo-reward-ci"
        assert meta["scan"]["hash_mode"] == "sha256"

        # The agent's own footprint is END-only: answer.txt must be in the
        # delta and absent from the begin archive.
        with gzip.open(bundle / BEGIN_MANIFEST) as handle:
            manifest = {record["p"]: record for record in map(json.loads, handle)}
        with tarfile.open(bundle / _BB) as tar:
            begin_names = set(tar.getnames())
            assert not any(n.rstrip("/") == "app/answer.txt" for n in begin_names)
            assert len(begin_names) > 100  # a real image, not a toy tree

            # Per-file validity join on a sample of REGULAR files: the begin
            # manifest's sha256 must equal the archived member's bytes — the
            # exact check the server will run before serving shared
            # before-content out of another trial's archive.
            sampled = 0
            for path, record in manifest.items():
                if sampled >= 25 or record.get("t") != "f" or not record.get("h"):
                    if sampled >= 25:
                        break
                    continue
                member = tar.extractfile(path.lstrip("/"))
                if member is None:
                    continue
                assert (
                    _hashlib.sha256(member.read()).hexdigest() == record["h"]
                ), path
                sampled += 1
            assert sampled == 25
        with tarfile.open(bundle / END_DELTA) as tar:
            assert any(
                m.lstrip("/").rstrip("/") == "app/answer.txt" for m in tar.getnames()
            )

    def test_atif_agent_trajectory_expands_end_to_end(
        self, tmp_path, monkeypatch, client, app
    ):
        """Item 1 closed for real: a SUPPORTS_ATIF agent through the REAL harness.

        The custom agent does exactly what claude-code/goose/terminus do — write
        one of Harbor's own golden ATIF documents into its logs dir — so this
        proves the whole chain: harness delivers the trajectory at the contract
        location (agent/trajectory.json), parse_trial reads THAT location, and
        capture_trial expands it into turn/tool_call spans on the wire.
        """
        pytest.importorskip("harbor", reason="harbor not installed")
        import os
        import pwd

        from probe.connectors.harbor import capture_trial, parse_trial
        from probe.connectors.harbor_runner import run_trial
        from tests.conftest import open_run
        from tests.miniatif_agent import GOLDEN_ATIF

        real_docker_config = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"
        if real_docker_config.is_dir():
            monkeypatch.setenv("DOCKER_CONFIG", str(real_docker_config))

        outcome = asyncio.run(
            run_trial(
                _write_echo_task(tmp_path / "tasks"),
                trials_dir=tmp_path / "trials",
                agent="tests.miniatif_agent:MiniAtifAgent",
            )
        )
        assert outcome.result.exception_info is None, outcome.result.exception_info
        # The custom agent actually solved the task (verifier unaffected).
        assert outcome.result.verifier_result.rewards["reward"] == pytest.approx(0.85)

        # Harness delivered the trajectory at the REAL contract location.
        emitted = outcome.trial_dir / "agent" / "trajectory.json"
        assert emitted.is_file()
        assert not (outcome.trial_dir / "trajectory.json").exists()
        golden = json.loads(GOLDEN_ATIF.read_text())
        parsed = parse_trial(outcome.trial_dir)
        assert parsed.trajectory == json.loads(emitted.read_text()) == golden
        assert parsed.trajectory_format == golden["schema_version"]  # ATIF-v1.7

        # ...and capture expands it into turn/tool_call spans on the wire.
        run = open_run(client, experiment="e", name="r")
        result = capture_trial(run, outcome.trial_dir, step_index=7, strict=True)
        assert result["trajectory"]["expanded"] is True
        assert result["trajectory"]["spans"] >= len(golden["steps"])
        posted_types = [
            span.get("span_type")
            for req in app.requests
            if req.url.path.endswith("/spans")
            for span in json.loads(req.content)["spans"]
        ]
        assert "turn" in posted_types
