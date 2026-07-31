"""harbor_capture: the SDK-owned attach()/finalize() facade for Harbor bridges.

Same layering philosophy as test_harbor_runner: everything here runs against
fake trial/environment objects with ``harbor.trial.hooks`` stubbed at the
module boundary (the facade's only harbor import), so CI always exercises the
capture-mode gating, correlation retention, staging, and fail-open rules
without harbor installed.  The recorder protocol itself is covered by
test_harbor_runner; here it only has to fold into the capture result.
"""

from __future__ import annotations

import asyncio
import enum
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from probe.connectors import harbor_capture
from probe.connectors.harbor_capture import (
    CaptureIncompleteError,
    HarborCaptureResult,
    SandboxCorrelationCapture,
    attach,
)
from probe.connectors.harbor_runner import (
    HarborContractError,
    SandboxStateOptions,
    SandboxStateRecorder,
)
from probe.connectors.sandbox_state import BUNDLE_DIRNAME
from tests.test_harbor_runner import FakeSandbox


class FakeTrialEvent(enum.Enum):
    AGENT_START = "agent-start"
    AGENT_END = "agent-end"


@pytest.fixture
def harbor_stub(monkeypatch):
    """Stub harbor's hook module (the facade's only harbor import) and pass
    the contract check, so attach() runs without harbor installed."""
    for package in ("harbor", "harbor.trial"):
        module = ModuleType(package)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, package, module)
    hooks_module = ModuleType("harbor.trial.hooks")
    hooks_module.TrialEvent = FakeTrialEvent
    monkeypatch.setitem(sys.modules, "harbor.trial.hooks", hooks_module)
    monkeypatch.setattr(harbor_capture, "verify_harbor_contract", lambda: [])
    return hooks_module


class CorrelatedSandbox(FakeSandbox):
    """FakeSandbox plus the identifier surface the correlation capture reads."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.session_id = "logical-session-1"
        self._sandbox = SimpleNamespace(object_id="provider-sandbox-1")


class FakeTrial:
    """Mirrors the Trial surface the facade touches: add_hook, run-time hook
    emission, agent_environment, id, paths/config."""

    id = "trial-uuid-1"

    def __init__(self, tmp_path: Path) -> None:
        trials_dir = tmp_path / "trials"
        self.config = SimpleNamespace(trials_dir=trials_dir, trial_name="task__unit")
        self.paths = SimpleNamespace(trial_dir=trials_dir / "task__unit")
        self.agent_environment = CorrelatedSandbox(tmp_path / "container")
        self.hooks: dict[FakeTrialEvent, list] = {event: [] for event in FakeTrialEvent}

    def add_hook(self, event, hook) -> None:
        self.hooks[event].append(hook)

    async def emit(self, event) -> None:
        for hook in self.hooks[event]:
            await hook(SimpleNamespace(event=event))

    def write_trial_tree(self) -> Path:
        self.paths.trial_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.trial_dir / "config.json").write_text(
            json.dumps({"task": {"name": "task"}})
        )
        (self.paths.trial_dir / "lock.json").write_text("{}")
        (self.paths.trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "id": self.id,
                    "trial_name": "task__unit",
                    "task_name": "task",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                }
            )
        )
        agent_dir = self.paths.trial_dir / "agent"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "native.log").write_text("native agent log")
        return self.paths.trial_dir

    async def run(self) -> None:
        """The Harbor choreography the facade depends on: hooks fire around
        the agent phase, then Harbor nulls the environment handle."""
        await self.emit(FakeTrialEvent.AGENT_START)
        self.write_trial_tree()
        await self.emit(FakeTrialEvent.AGENT_END)
        self.agent_environment = None


# ---------------------------------------------------------------------------
# attach(): mode gating and loud setup.
# ---------------------------------------------------------------------------
class TestAttach:
    def test_unknown_capture_mode_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="capture_mode"):
            attach(FakeTrial(tmp_path), capture_mode="audit")

    def test_off_mode_installs_nothing_and_never_imports_harbor(
        self, tmp_path, monkeypatch
    ):
        # No harbor stub and a poisoned contract check: off must touch neither.
        def explode() -> list[str]:
            raise AssertionError("off mode must not check the harbor contract")

        monkeypatch.setattr(harbor_capture, "verify_harbor_contract", explode)
        trial = FakeTrial(tmp_path)
        handle = attach(trial, capture_mode="off")
        assert all(hooks == [] for hooks in trial.hooks.values())
        assert handle.correlation_capture is None
        assert handle.sandbox_state is None

    def test_off_mode_finalize_is_a_filesystem_noop(self, tmp_path):
        trial = FakeTrial(tmp_path)
        trial_dir = trial.write_trial_tree()
        handle = attach(trial, capture_mode="off")
        result = asyncio.run(handle.finalize(trial_dir, capture_dir=tmp_path / "caps"))
        assert result == HarborCaptureResult(status="not_attempted")
        assert handle.result is result
        assert not result.complete
        assert not (tmp_path / "caps").exists()

    def test_contract_problems_raise_loudly_at_attach(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            harbor_capture, "verify_harbor_contract", lambda: ["TrialEvent.AGENT_START is missing"]
        )
        with pytest.raises(HarborContractError, match="AGENT_START"):
            attach(FakeTrial(tmp_path), capture_mode="shadow")

    def test_hook_install_failure_is_recorded_not_raised(self, tmp_path, harbor_stub):
        trial = FakeTrial(tmp_path)

        def broken_add_hook(event, hook):
            raise RuntimeError("hooks unavailable")

        trial.add_hook = broken_add_hook
        handle = attach(
            trial, capture_mode="shadow", sandbox_state=SandboxStateOptions()
        )
        assert handle.correlation_capture is None
        assert any("correlation hook install failed" in e for e in handle.errors)
        assert any("sandbox-state hook install failed" in e for e in handle.errors)
        # The recorder exists, never fired, and says why.
        summary = handle.sandbox_state.summary()
        assert summary["status"] == "not_attempted"
        assert any("hook install failed" in e for e in summary["errors"])


# ---------------------------------------------------------------------------
# Correlation capture: stable-ID discipline + retention.
# ---------------------------------------------------------------------------
class TestSandboxCorrelation:
    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            (SimpleNamespace(_sandbox=SimpleNamespace(id="daytona-1")), "daytona-1"),
            (SimpleNamespace(_sandbox=SimpleNamespace(sandbox_id="e2b-1")), "e2b-1"),
            (SimpleNamespace(_sandbox=SimpleNamespace(object_id="sb-modal-1")), "sb-modal-1"),
            (SimpleNamespace(_devbox=SimpleNamespace(id="dbx-runloop-1")), "dbx-runloop-1"),
            (SimpleNamespace(provider_ref="provider-ref-1"), "provider-ref-1"),
            (SimpleNamespace(provider_sandbox_id="psid-1"), "psid-1"),
            (None, None),
            (SimpleNamespace(_sandbox=None), None),
        ],
    )
    def test_provider_id_reads_cloud_backend_handles(self, environment, expected):
        assert harbor_capture._provider_sandbox_id(environment) == expected

    def test_methods_are_never_invoked_as_identifiers(self):
        environment = SimpleNamespace(
            _sandbox=SimpleNamespace(id=lambda: "never-call-me")
        )
        assert harbor_capture._provider_sandbox_id(environment) is None

    def test_retention_survives_harbor_nulling_the_handle(self, tmp_path, harbor_stub):
        trial = FakeTrial(tmp_path)
        capture = SandboxCorrelationCapture(trial)
        capture.install()
        asyncio.run(trial.emit(FakeTrialEvent.AGENT_START))
        trial.agent_environment = None  # Harbor tears the handle down
        assert capture.resolved() == ("logical-session-1", "provider-sandbox-1")

    def test_post_run_fallback_when_hooks_never_fired(self, tmp_path, harbor_stub):
        trial = FakeTrial(tmp_path)
        capture = SandboxCorrelationCapture(trial)
        capture.install()  # installed but never emitted
        assert capture.resolved() == ("logical-session-1", "provider-sandbox-1")


# ---------------------------------------------------------------------------
# finalize(): staging, identifiers, sandbox-state folding, failure modes.
# ---------------------------------------------------------------------------
class TestFinalize:
    def _run(self, tmp_path, **attach_kwargs):
        trial = FakeTrial(tmp_path)
        handle = attach(
            trial,
            correlation={
                "session_id": "session-123",
                "task_id": "task",
                "rollout_id": 17,
                "sample_id": 41,
            },
            context={"mix": "swe-and-terminal"},
            **attach_kwargs,
        )
        asyncio.run(trial.run())
        return trial, handle

    def test_happy_path_stages_and_carries_identifiers(self, tmp_path, harbor_stub):
        trial, handle = self._run(
            tmp_path, capture_mode="shadow", sandbox_state=SandboxStateOptions()
        )
        result = asyncio.run(
            handle.finalize(
                trial.paths.trial_dir,
                capture_dir=tmp_path / "captures",
                run_id="run-1",
                step_index=17,
            )
        )
        assert result.complete and result.status == "complete"
        assert result.error is None
        result.raise_if_incomplete()  # required-mode gate passes

        # Identifiers were retained across Harbor nulling the environment.
        assert result.sandbox_id == "logical-session-1"
        assert result.provider_sandbox_id == "provider-sandbox-1"
        assert result.external_key.startswith("probe:v1:harbor:rollout:")

        # The staged tree carries the native bytes AND the sandbox bundle.
        staged = Path(result.staged_trial_dir)
        assert (staged / "agent" / "native.log").read_text() == "native agent log"
        assert (staged / "artifacts" / BUNDLE_DIRNAME / "meta.json").is_file()
        assert result.file_count >= 3
        assert result.size_bytes > 0
        assert Path(result.manifest_path).is_file()
        assert Path(result.export_descriptor_path).is_file()
        descriptor = json.loads(Path(result.export_descriptor_path).read_text())
        assert descriptor["arguments"]["expand"] is True
        assert Path(result.archive_path).is_file()
        assert result.archive_content_hash

        # The sandbox-state verdict is folded into the result AND the staged
        # capture manifest's context, exactly like miles' inline bridge did.
        assert result.sandbox_state["status"] == {"begin": "ok", "end": "ok"}
        assert result.sandbox_state["integrity"] == {
            "begin_verified": True,
            "end_verified": True,
        }
        manifest = json.loads(Path(result.manifest_path).read_text())
        assert (
            manifest["source"]["context"]["sandbox_state"] == result.sandbox_state
        )
        assert manifest["source"]["session_id"] == "session-123"
        assert manifest["environment"]["sandbox_id"] == "logical-session-1"
        assert manifest["environment"]["provider_sandbox_id"] == "provider-sandbox-1"
        assert manifest["trial"]["id"] == "trial-uuid-1"
        assert handle.result is result

    def test_finalize_surfaces_begin_bytes_captured(self, tmp_path, harbor_stub):
        # A bridge learns whether the elected trial actually archived begin bytes
        # from the finalize result — no re-reading the authored bundle off disk.
        trial, handle = self._run(
            tmp_path, capture_mode="shadow", sandbox_state=SandboxStateOptions()
        )
        handle.sandbox_state._begin_bytes_captured = True  # a verified begin archive
        result = asyncio.run(
            handle.finalize(trial.paths.trial_dir, capture_dir=tmp_path / "caps")
        )
        assert result.begin_bytes_captured is True

    def test_finalize_begin_bytes_captured_false_by_default(self, tmp_path, harbor_stub):
        trial, handle = self._run(
            tmp_path, capture_mode="shadow", sandbox_state=SandboxStateOptions()
        )
        result = asyncio.run(
            handle.finalize(trial.paths.trial_dir, capture_dir=tmp_path / "caps")
        )
        assert result.begin_bytes_captured is False

    def test_default_capture_dir_is_trials_sibling(self, tmp_path, harbor_stub):
        trial, handle = self._run(tmp_path, capture_mode="shadow")
        result = asyncio.run(handle.finalize(trial.paths.trial_dir))
        assert result.complete
        assert Path(result.staged_trial_dir).is_relative_to(tmp_path / "trials-captures")
        assert result.sandbox_state is None  # no recorder attached

    def test_staging_failure_in_shadow_mode_is_recorded_not_raised(
        self, tmp_path, harbor_stub, monkeypatch
    ):
        trial, handle = self._run(tmp_path, capture_mode="shadow")

        def explode(*args, **kwargs):
            raise RuntimeError("capture unavailable")

        monkeypatch.setattr(harbor_capture, "stage_trial_export", explode)
        result = asyncio.run(
            handle.finalize(trial.paths.trial_dir, capture_dir=tmp_path / "captures")
        )
        assert result.status == "failed"
        assert result.error == "RuntimeError: capture unavailable"
        # Identifiers still ride the failed result for the bridge's response.
        assert result.sandbox_id == "logical-session-1"
        assert result.provider_sandbox_id == "provider-sandbox-1"
        # The required-mode gate is exactly this result's verdict.
        assert not result.complete
        with pytest.raises(CaptureIncompleteError, match="capture unavailable"):
            result.raise_if_incomplete()

    def test_cancellation_propagates_out_of_finalize(
        self, tmp_path, harbor_stub, monkeypatch
    ):
        trial, handle = self._run(tmp_path, capture_mode="required")

        def cancelled(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(harbor_capture, "stage_trial_export", cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(handle.finalize(trial.paths.trial_dir))

    def test_missing_expected_file_yields_partial_not_complete(
        self, tmp_path, harbor_stub
    ):
        trial, handle = self._run(tmp_path, capture_mode="required")
        (trial.paths.trial_dir / "result.json").unlink()
        result = asyncio.run(
            handle.finalize(trial.paths.trial_dir, capture_dir=tmp_path / "captures")
        )
        assert result.status == "partial"
        with pytest.raises(CaptureIncompleteError):
            result.raise_if_incomplete()

    def test_sandbox_state_failure_is_folded_honestly(self, tmp_path, harbor_stub):
        trial = FakeTrial(tmp_path)
        trial.agent_environment.fail_begin_exec = True
        handle = attach(
            trial, capture_mode="shadow", sandbox_state=SandboxStateOptions()
        )
        asyncio.run(trial.run())
        result = asyncio.run(
            handle.finalize(trial.paths.trial_dir, capture_dir=tmp_path / "captures")
        )
        # The trial capture itself still completes; only the snapshot failed.
        assert result.complete
        assert result.sandbox_state["status"] == {"begin": "failed", "end": "failed"}
        assert any("begin" in e for e in result.sandbox_state["errors"])
        assert not (
            Path(result.staged_trial_dir) / "artifacts" / BUNDLE_DIRNAME
        ).exists()


# ---------------------------------------------------------------------------
# Recorder summary(): the shape the facade folds into capture context.
# ---------------------------------------------------------------------------
class TestRecorderSummary:
    def test_not_attempted_until_a_hook_fires(self, tmp_path):
        trial = SimpleNamespace(
            agent_environment=FakeSandbox(tmp_path / "container"),
            paths=SimpleNamespace(trial_dir=tmp_path / "trial"),
            config=SimpleNamespace(trials_dir=tmp_path, trial_name="trial"),
        )
        rec = SandboxStateRecorder(trial, SandboxStateOptions())
        assert rec.attempted() is False
        assert rec.summary()["status"] == "not_attempted"
        (tmp_path / "trial").mkdir()
        asyncio.run(rec.on_agent_start())
        asyncio.run(rec.on_agent_end())
        summary = rec.summary()
        assert summary["schema"] == "probe.sandbox-state/1"
        assert summary["status"] == {"begin": "ok", "end": "ok"}
        assert summary["arch"] == "amd64"
        assert summary["integrity"] == {"begin_verified": True, "end_verified": True}
        assert summary["errors"] == []

    def test_install_failure_is_reported(self, tmp_path):
        trial = SimpleNamespace(agent_environment=None)
        rec = SandboxStateRecorder(trial, SandboxStateOptions())
        rec.record_install_failure(RuntimeError("no hook surface"))
        summary = rec.summary()
        assert summary["status"] == "not_attempted"
        assert summary["errors"] == [
            "hook install failed: RuntimeError: no hook surface"
        ]
