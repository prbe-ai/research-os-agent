"""SDK-owned Harbor sandbox-state capture (probe.connectors.harbor.SandboxStateCapture).

Tier 1 (fake environment) verifies the hook choreography + fail-open + bundle
authoring with no Docker. The Docker-gated ``@pytest.mark.harbor`` test (step 3b)
runs a REAL oracle trial with capture registered and validates the bundle.
"""

from __future__ import annotations

import asyncio
import enum
import gzip
import hashlib
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from probe.connectors import sandbox_state
from probe.connectors.harbor import SandboxStateCapture, stage_trial_export


class FakeTrialEvent(enum.Enum):
    AGENT_START = "agent-start"
    AGENT_END = "agent-end"


@pytest.fixture
def fake_hooks(monkeypatch):
    for pkg in ("harbor", "harbor.trial"):
        mod = ModuleType(pkg)
        mod.__path__ = []
        monkeypatch.setitem(sys.modules, pkg, mod)
    hooks = ModuleType("harbor.trial.hooks")
    hooks.TrialEvent = FakeTrialEvent
    monkeypatch.setitem(sys.modules, "harbor.trial.hooks", hooks)


def _gz(records: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as h:
        for r in records:
            h.write(json.dumps(r).encode() + b"\n")
    return buf.getvalue()


class FakeEnv:
    def __init__(self):
        self.execs: list[str] = []
        self.exec_kwargs: list[dict] = []
        self.uploads: list[tuple[str, str]] = []
        self.machine = "x86_64"
        self.fail_phase: str | None = None
        self._outputs: dict[str, bytes] = {}

    def _trailer(self, phase, files):
        payload = {
            "schema": sandbox_state.TRAILER_SCHEMA,
            "phase": phase,
            "files": {n: {"sha256": hashlib.sha256(d).hexdigest(), "size_bytes": len(d)} for n, d in files.items()},
            "stats": {"entries": 3, "files_scanned": 2, "added": 1, "modified": 0, "deleted": 0},
            "errors": [],
            "hash_mode": "fast",
        }
        return sandbox_state.TRAILER_PREFIX + json.dumps(payload)

    async def exec(self, command, user=None, **kwargs):
        self.execs.append(command)
        self.exec_kwargs.append(kwargs)
        if command == "uname -m":
            return SimpleNamespace(stdout=self.machine + "\n", stderr="", return_code=0)
        if command.startswith(("rm -rf ", "mkdir -p ")):
            return SimpleNamespace(stdout="", stderr="", return_code=0)
        phase = "begin" if " begin " in command else "end"
        if self.fail_phase == phase:
            return SimpleNamespace(stdout="", stderr="boom", return_code=3)
        wd = command.split("--workdir ")[1].split()[0]
        files = (
            {sandbox_state.BEGIN_MANIFEST: _gz([{"p": "/b"}, {"p": "/a"}])}
            if phase == "begin"
            else {sandbox_state.END_MANIFEST: _gz([{"p": "/a"}]), sandbox_state.END_DELTA: b"delta"}
        )
        for n, d in files.items():
            self._outputs[f"{wd}/{n}"] = d
        return SimpleNamespace(stdout="noise\n" + self._trailer(phase, files), stderr="", return_code=0)

    async def upload_file(self, src, dst):
        self.uploads.append((str(src), dst))

    async def download_file(self, src, dst):
        Path(dst).write_bytes(self._outputs[src])


class FakeTrial:
    def __init__(self):
        self.agent_environment = FakeEnv()
        self.hooks = {e: [] for e in FakeTrialEvent}

    def add_hook(self, event, hook):
        self.hooks[event].append(hook)

    async def emit(self, event):
        for hook in self.hooks[event]:
            await hook(SimpleNamespace(event=event))


def _capture(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("PROBE_SANDBOX_SNAPSHOT_BIN", __file__)  # any real file
    trial = FakeTrial()
    host = tmp_path / "host"
    host.mkdir()
    cap = SandboxStateCapture(trial, host, **kw)
    cap.install()
    return trial, cap


def test_happy_path_is_ephemeral_and_bounded(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    env = trial.agent_environment

    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    assert cap.status["begin"] == "ok"
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_END))
    assert cap.status["end"] == "ok"

    # fresh binary per phase into distinct workdirs; begin manifest re-uploaded.
    snaps = [t for _, t in env.uploads if t.endswith("/snap")]
    assert len(snaps) == 2 and len(set(snaps)) == 2
    assert any(t.endswith("/begin.jsonl.gz") for _, t in env.uploads)
    # every snapshot exec is timeout-bounded; mkdir precedes the snapshot exec.
    for cmd, kw in zip(env.execs, env.exec_kwargs, strict=True):
        if cmd != "uname -m":
            assert kw.get("timeout_sec")
    mkdir_i = next(i for i, c in enumerate(env.execs) if c.startswith("mkdir -p "))
    snap_i = next(i for i, c in enumerate(env.execs) if "/snap begin " in c)
    assert mkdir_i < snap_i and "--max-seconds" in env.execs[snap_i]
    # each workdir was cleaned up.
    workdirs = {c.split("--workdir ")[1].split()[0] for c in env.execs if "--workdir" in c}
    cleaned = {c.removeprefix("rm -rf ").strip("'") for c in env.execs if c.startswith("rm -rf")}
    assert workdirs <= cleaned


def test_write_bundle_produces_a_valid_bundle(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_END))

    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    summary = cap.write_bundle(trial_dir)
    assert summary["status"] == {"begin": "ok", "end": "ok"}

    report = sandbox_state.validate_bundle(
        trial_dir / "artifacts" / sandbox_state.BUNDLE_DIRNAME, require_integrity=True
    )
    assert report.ok, report.problems


def test_begin_failure_skips_end_and_never_raises(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    trial.agent_environment.fail_phase = "begin"
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_END))
    assert cap.status["begin"].startswith("RuntimeError")
    assert "begin snapshot unavailable" in cap.status["end"]


def test_hook_swallows_environment_explosions(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)

    async def boom(*a, **k):
        raise OSError("environment vanished")

    trial.agent_environment.upload_file = boom
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    assert cap.status["begin"].startswith("OSError")


def test_integrity_mismatch_detected(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    env = trial.agent_environment
    original = env.download_file

    async def tamper(src, dst):
        await original(src, dst)
        if src.endswith(sandbox_state.BEGIN_MANIFEST):
            Path(dst).write_bytes(b"tampered")

    env.download_file = tamper
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    assert cap.status["begin"] == "ok"
    assert cap._integrity[sandbox_state.BEGIN_MANIFEST] is False


def test_unknown_arch_falls_back_to_amd64(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    trial.agent_environment.machine = "riscv64"
    asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
    assert cap.summary()["arch"] == "amd64"
    assert any("unrecognized machine" in e for e in cap.summary()["errors"])


def test_not_attempted_writes_no_bundle(tmp_path, fake_hooks, monkeypatch):
    trial, cap = _capture(tmp_path, monkeypatch)
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    summary = cap.write_bundle(trial_dir)
    assert summary["status"] == "not_attempted"
    assert not (trial_dir / "artifacts" / sandbox_state.BUNDLE_DIRNAME).exists()


# --- Step 3b: real oracle trial WITH in-container sandbox capture ------------

def _write_oracle_task(task_dir: Path) -> None:
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n[task]\nname = "probe/sandbox-capture-oracle"\nauthors = []\n'
        "[verifier]\ntimeout_sec = 60.0\n[agent]\ntimeout_sec = 60.0\n"
        "[environment]\nbuild_timeout_sec = 300.0\ncpus = 1\nmemory_mb = 512\nstorage_mb = 1024\ngpus = 0\n"
        "[verifier.env]\n\n[solution.env]\n"
    )
    (task_dir / "instruction.md").write_text("Create /app/out.txt containing `done`.\n")
    (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\n")
    (task_dir / "solution" / "solve.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'done\\n' > /app/out.txt\n"
    )
    (task_dir / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /logs/verifier\n"
        'if [ "$(cat /app/out.txt 2>/dev/null)" = "done" ]; then\n'
        "  printf '1\\n' > /logs/verifier/reward.txt; exit 0\nfi\n"
        "printf '0\\n' > /logs/verifier/reward.txt; exit 1\n"
    )


@pytest.mark.harbor
def test_real_oracle_trial_with_sandbox_capture(tmp_path):
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.trial import Trial

    _write_oracle_task(tmp_path / "task")
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    async def run() -> Path:
        config = TrialConfig(
            task=TaskConfig(path=tmp_path / "task"),
            trials_dir=tmp_path / "trials",
            agent=AgentConfig(name="oracle"),
            environment=EnvironmentConfig(type="docker", delete=True),
        )
        trial = await Trial.create(config)
        capture = SandboxStateCapture(trial, host_dir)
        capture.install()
        await trial.run()
        return Path(trial.paths.trial_dir).resolve(), capture

    (tmp_path / "trials").mkdir()
    trial_dir, capture = asyncio.run(run())

    # the oracle's solve.sh wrote /app/out.txt -> the sandbox delta must show it.
    summary = capture.write_bundle(trial_dir)
    assert summary["status"] == {"begin": "ok", "end": "ok"}, summary

    stage_trial_export(
        trial_dir, tmp_path / "cap" / "t",
        run_id=None, step_index=0,
        expected_paths=("config.json", "result.json"), expand=False,
    )
    reports = sandbox_state.validate_captures(tmp_path / "cap", require_integrity=True)
    assert reports and reports[0].ok, reports and reports[0].problems
    assert reports[0].summary["added"] >= 1  # /app/out.txt (and any oracle churn)
