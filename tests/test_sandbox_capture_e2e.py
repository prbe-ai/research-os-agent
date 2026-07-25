"""End-to-end sandbox capture: real binary -> bundle -> stage_trial_export -> validate.

Closes the one seam neither test_sandbox_state.py (host helpers, fabricated
manifests) nor the Go tests (binary in isolation) cover: the *actual*
probe-sandbox-snapshot binary run against a real filesystem, authored into a
probe.sandbox-state/1 bundle, staged through the SDK's Harbor producer API, and
validated — proving the capture pipeline works without Docker, Harbor, or miles.

Builds the binary for the host platform (skips if the Go toolchain is absent),
so it exercises the current source rather than only the shipped Linux binaries.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from probe.connectors import sandbox_state as ss
from probe.connectors.harbor import stage_trial_export

_TOOL_DIR = Path(__file__).resolve().parents[1] / "tools" / "sandbox-snapshot"


@pytest.fixture(scope="session")
def snapshot_bin(tmp_path_factory) -> Path:
    """Build the snapshot binary for the host OS/arch; skip if Go is unavailable."""
    if shutil.which("go") is None:
        pytest.skip("Go toolchain not available to build the snapshot binary")
    out = tmp_path_factory.mktemp("psbx-bin") / "sandbox-snapshot"
    subprocess.run(
        ["go", "build", "-o", str(out), "."],
        cwd=_TOOL_DIR,
        check=True,
        env={**os.environ, "CGO_ENABLED": "0"},
    )
    return out


def _run(binary: Path, phase: str, workdir: Path, root: Path, begin: Path | None = None) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [str(binary), phase, "--workdir", str(workdir), "--root", str(root), "--max-seconds", "60"]
    if phase == "end":
        cmd += ["--begin-manifest", str(begin)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return ss.parse_trailer(result.stdout)


def _manifest_paths(path: Path) -> set[str]:
    with gzip.open(path, "rb") as handle:
        return {json.loads(line)["p"] for line in handle}


def _delta_members(path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                members[member.name] = tar.extractfile(member).read()
    return members


def test_capture_pipeline_end_to_end(snapshot_bin, tmp_path):
    work = tmp_path
    sandbox = work / "sandbox"
    (sandbox / "app").mkdir(parents=True)
    (sandbox / "app" / "keep.txt").write_text("unchanged\n")
    (sandbox / "app" / "modify.txt").write_text("baseline v1\n")
    (sandbox / "app" / "delete.txt").write_text("bye\n")

    host = work / "host"
    host.mkdir()

    # 1. begin: real binary snapshots the sandbox; integrity verified host-side.
    begin_wd = work / "cbegin"
    begin_tr = _run(snapshot_bin, "begin", begin_wd, sandbox)
    shutil.copy(begin_wd / ss.BEGIN_MANIFEST, host / ss.BEGIN_MANIFEST)
    assert ss.sha256_file(host / ss.BEGIN_MANIFEST) == begin_tr["files"][ss.BEGIN_MANIFEST]["sha256"]

    # 2. the "agent" mutates the sandbox: add / modify / delete / binary / symlink.
    (sandbox / "app" / "added.txt").write_text("brand new\n")
    (sandbox / "app" / "modify.txt").write_text("baseline v2 edited\n")
    (sandbox / "app" / "delete.txt").unlink()
    (sandbox / "app" / "blob.bin").write_bytes(os.urandom(2048))
    (sandbox / "app" / "link").symlink_to("added.txt")

    # 3. end: delta against the begin manifest.
    end_wd = work / "cend"
    end_tr = _run(snapshot_bin, "end", end_wd, sandbox, begin=host / ss.BEGIN_MANIFEST)
    for name in (ss.END_MANIFEST, ss.END_DELTA):
        shutil.copy(end_wd / name, host / name)

    # every change class is reflected in the trailer stats.
    assert end_tr["stats"]["added"] >= 3  # added.txt, blob.bin, link
    assert end_tr["stats"]["modified"] >= 1  # modify.txt
    assert end_tr["stats"]["deleted"] >= 1  # delete.txt

    # 4. integrity verify every output host-side (as the bridge does).
    integrity = {}
    for phase_tr in (begin_tr, end_tr):
        for name in phase_tr["files"]:
            integrity[name] = ss.sha256_file(host / name) == phase_tr["files"][name]["sha256"]
    assert all(integrity.values())

    # the delta tarball carries the real bytes of the added/modified files.
    members = _delta_members(host / ss.END_DELTA)
    assert any(name.endswith("app/added.txt") for name in members)
    assert any(v == b"baseline v2 edited\n" for v in members.values())

    # deletion shows only in the manifest diff, never in the delta.
    begin_paths = _manifest_paths(host / ss.BEGIN_MANIFEST)
    end_paths = _manifest_paths(host / ss.END_MANIFEST)
    deleted = begin_paths - end_paths
    assert any(p.endswith("app/delete.txt") for p in deleted)

    # 5. author the probe.sandbox-state/1 bundle into a Harbor-shaped trial tree.
    trial_dir = work / "trial"
    trial_dir.mkdir()
    (trial_dir / "config.json").write_text(json.dumps({"task": {"name": "capture-e2e"}}))
    (trial_dir / "lock.json").write_text(json.dumps({"task": {"checksum": "sha256:e2e"}}))
    (trial_dir / "result.json").write_text(
        json.dumps({"trial_name": "capture-e2e__local", "verifier_result": {"rewards": {"reward": 1.0}}})
    )
    meta = ss.build_meta(
        begin_trailer=begin_tr,
        end_trailer=end_tr,
        status={"begin": "ok", "end": "ok"},
        begin_at="2026-07-25T00:00:00Z",
        end_at="2026-07-25T00:01:00Z",
        arch="host",
        integrity=integrity,
        errors=[],
    )
    ss.write_bundle(
        trial_dir / "artifacts" / ss.BUNDLE_DIRNAME,
        {name: host / name for name in (ss.BEGIN_MANIFEST, ss.END_MANIFEST, ss.END_DELTA)},
        meta,
    )

    # 6. stage through the SDK's Harbor producer API (no network, no Docker).
    capture_dir = work / "captures"
    staged = stage_trial_export(
        trial_dir,
        capture_dir / "capture-e2e",
        run_id=None,
        step_index=600,
        environment={"type": "docker"},
        correlation={"trial_id": "capture-e2e", "task_id": "capture-e2e"},
        context={"sandbox_state": {"status": {"begin": "ok", "end": "ok"}}},
        expected_paths=("config.json", "lock.json", "result.json"),
        expand=False,
    )

    # 7. the staged trial carries a complete, integrity-true bundle + a descriptor.
    #    Validate through the SDK-owned validator (one definition of "valid").
    reports = ss.validate_captures(capture_dir, require_integrity=True)
    assert len(reports) == 1 and reports[0].ok, reports and reports[0].problems
    assert reports[0].summary["added"] >= 3 and reports[0].summary["deleted"] >= 1
    staged_bundle = Path(staged.staged_trial.trial_dir) / "artifacts" / ss.BUNDLE_DIRNAME
    assert (staged_bundle / ss.END_DELTA).is_file()
    assert Path(staged.request_path).is_file()  # probe-harbor-export/1 descriptor

    # host-side manifest sort survived staging (bytewise sorted).
    with gzip.open(staged_bundle / ss.BEGIN_MANIFEST, "rb") as handle:
        staged_paths = [json.loads(line)["p"] for line in handle]
    assert staged_paths == sorted(staged_paths)


def test_shipped_binaries_are_present_for_deploy():
    """The deploy path relies on committed binaries, not a Go build."""
    for arch in ("amd64", "arm64"):
        path = ss.snapshot_binary_path(arch)
        assert path.is_file() and path.read_bytes()[:4] == b"\x7fELF"
