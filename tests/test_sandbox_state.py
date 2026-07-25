"""Host-side sandbox-state helpers (probe.sandbox-state/1)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from probe.connectors import sandbox_state


def _gzip_jsonl(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wb") as handle:
        for record in records:
            handle.write(json.dumps(record).encode() + b"\n")


class TestMachineToArch:
    @pytest.mark.parametrize(
        ("machine", "arch"),
        [
            ("x86_64", "amd64"),
            ("amd64", "amd64"),
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
            ("  X86_64\n", "amd64"),
            ("riscv64", None),
            ("", None),
        ],
    )
    def test_mapping(self, machine, arch):
        assert sandbox_state.machine_to_arch(machine) == arch


class TestSnapshotBinaryPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = tmp_path / "snap"
        fake.write_bytes(b"\x7fELF")
        monkeypatch.setenv("PROBE_SANDBOX_SNAPSHOT_BIN", str(fake))
        assert sandbox_state.snapshot_binary_path("amd64") == fake

    def test_env_override_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROBE_SANDBOX_SNAPSHOT_BIN", str(tmp_path / "gone"))
        with pytest.raises(FileNotFoundError):
            sandbox_state.snapshot_binary_path("amd64")

    def test_unknown_arch_rejected(self, monkeypatch):
        monkeypatch.delenv("PROBE_SANDBOX_SNAPSHOT_BIN", raising=False)
        with pytest.raises(ValueError):
            sandbox_state.snapshot_binary_path("riscv64")

    def test_packaged_binaries_ship(self, monkeypatch):
        monkeypatch.delenv("PROBE_SANDBOX_SNAPSHOT_BIN", raising=False)
        for arch in ("amd64", "arm64"):
            path = sandbox_state.snapshot_binary_path(arch)
            assert path.is_file(), f"packaged binary missing for {arch}"
            assert path.read_bytes()[:4] == b"\x7fELF"


class TestParseTrailer:
    def test_last_trailer_wins_and_noise_ignored(self):
        good = {"schema": sandbox_state.TRAILER_SCHEMA, "phase": "begin", "files": {}}
        stdout = "\n".join(
            [
                "agent noise",
                sandbox_state.TRAILER_PREFIX
                + json.dumps({"schema": sandbox_state.TRAILER_SCHEMA, "phase": "stale"}),
                "PSBX1-lookalike without space",
                sandbox_state.TRAILER_PREFIX + json.dumps(good),
            ]
        )
        assert sandbox_state.parse_trailer(stdout)["phase"] == "begin"

    def test_missing_trailer_raises(self):
        with pytest.raises(ValueError, match="no PSBX1 trailer"):
            sandbox_state.parse_trailer("nothing here\n")

    def test_wrong_schema_raises(self):
        stdout = sandbox_state.TRAILER_PREFIX + json.dumps({"schema": "other/1"})
        with pytest.raises(ValueError, match="unexpected trailer schema"):
            sandbox_state.parse_trailer(stdout)


class TestSortManifest:
    def test_sorts_bytewise_and_preserves_lines(self, tmp_path):
        src, dst = tmp_path / "in.jsonl.gz", tmp_path / "out.jsonl.gz"
        records = [
            {"p": "/z/last", "t": "f"},
            {"p": "/a/b", "t": "f"},
            {"p": "/a.txt", "t": "f"},  # '.' sorts before '/'
            {"p": "/a/evil\nname", "t": "f"},
        ]
        _gzip_jsonl(src, records)
        count = sandbox_state.sort_manifest(src, dst)
        assert count == 4
        with gzip.open(dst, "rb") as handle:
            paths = [json.loads(line)["p"] for line in handle]
        assert paths == ["/a.txt", "/a/b", "/a/evil\nname", "/z/last"]

    def test_atomic_no_partial_on_bad_input(self, tmp_path):
        src, dst = tmp_path / "in.jsonl.gz", tmp_path / "out.jsonl.gz"
        with gzip.open(src, "wb") as handle:
            handle.write(b'{"p": "/ok"}\nnot-json\n')
        with pytest.raises(json.JSONDecodeError):
            sandbox_state.sort_manifest(src, dst)
        assert not dst.exists()


class TestWriteBundle:
    def _trailer(self, **extra):
        base = {
            "schema": sandbox_state.TRAILER_SCHEMA,
            "files": {},
            "stats": {"entries": 3, "files_scanned": 2},
            "errors": [],
        }
        base.update(extra)
        return base

    def test_bundle_layout_and_meta_last(self, tmp_path):
        host = tmp_path / "host"
        host.mkdir()
        _gzip_jsonl(host / sandbox_state.BEGIN_MANIFEST, [{"p": "/b"}, {"p": "/a"}])
        _gzip_jsonl(host / sandbox_state.END_MANIFEST, [{"p": "/a"}])
        (host / sandbox_state.END_DELTA).write_bytes(b"tarball-bytes")

        meta = sandbox_state.build_meta(
            begin_trailer=self._trailer(),
            end_trailer=self._trailer(
                stats={"added": 1, "modified": 0, "deleted": 2, "delta_budget_bytes": 99},
                truncated=True,
                dropped=["/big"],
                dropped_count=1,
            ),
            status={"begin": "ok", "end": "ok"},
            begin_at="2026-07-23T00:00:00Z",
            end_at="2026-07-23T00:05:00Z",
            arch="amd64",
            integrity={"begin-manifest.jsonl.gz": True},
            errors=["bridge-side note"],
        )
        bundle = tmp_path / "bundle"
        sandbox_state.write_bundle(
            bundle,
            {
                sandbox_state.BEGIN_MANIFEST: host / sandbox_state.BEGIN_MANIFEST,
                sandbox_state.END_MANIFEST: host / sandbox_state.END_MANIFEST,
                sandbox_state.END_DELTA: host / sandbox_state.END_DELTA,
            },
            meta,
        )

        loaded = json.loads((bundle / "meta.json").read_text())
        assert loaded["schema"] == sandbox_state.SCHEMA
        assert loaded["summary"]["deleted"] == 2
        assert loaded["limits"]["truncated"] is True
        assert loaded["limits"]["dropped"] == ["/big"]
        assert loaded["errors"] == ["bridge-side note"]
        assert (bundle / sandbox_state.END_DELTA).read_bytes() == b"tarball-bytes"
        with gzip.open(bundle / sandbox_state.BEGIN_MANIFEST, "rb") as handle:
            paths = [json.loads(line)["p"] for line in handle]
        assert paths == ["/a", "/b"]
        assert not (bundle / "meta.json.tmp").exists()

    def test_partial_bundle_skips_missing_files(self, tmp_path):
        host = tmp_path / "host"
        host.mkdir()
        _gzip_jsonl(host / sandbox_state.BEGIN_MANIFEST, [{"p": "/a"}])
        meta = sandbox_state.build_meta(
            begin_trailer=self._trailer(),
            end_trailer=None,
            status={"begin": "ok", "end": "TimeoutError: end phase"},
            begin_at="2026-07-23T00:00:00Z",
            end_at=None,
            arch="amd64",
            integrity={},
            errors=[],
        )
        bundle = tmp_path / "bundle"
        sandbox_state.write_bundle(
            bundle,
            {
                sandbox_state.BEGIN_MANIFEST: host / sandbox_state.BEGIN_MANIFEST,
                sandbox_state.END_MANIFEST: host / "never-created",
                sandbox_state.END_DELTA: host / "never-created-2",
            },
            meta,
        )
        assert (bundle / sandbox_state.BEGIN_MANIFEST).is_file()
        assert not (bundle / sandbox_state.END_MANIFEST).exists()
        loaded = json.loads((bundle / "meta.json").read_text())
        assert loaded["status"]["end"].startswith("TimeoutError")
        assert loaded["end_at"] is None


class TestValidateBundle:
    """The SDK-owned validator (probe.connectors.sandbox_state.validate_bundle)."""

    def _bundle(self, root: Path, *, meta: dict | None, manifests: bool = True) -> Path:
        d = root / "trial" / "artifacts" / sandbox_state.BUNDLE_DIRNAME
        d.mkdir(parents=True)
        if manifests:
            for name in (sandbox_state.BEGIN_MANIFEST, sandbox_state.END_MANIFEST):
                _gzip_jsonl(d / name, [{"p": "/a", "t": "f"}])
            (d / sandbox_state.END_DELTA).write_bytes(b"tar")
        if meta is not None:
            (d / "meta.json").write_text(json.dumps(meta))
        return d

    def _healthy_meta(self) -> dict:
        return {
            "schema": sandbox_state.SCHEMA,
            "tool": {"arch": "amd64"},
            "status": {"begin": "ok", "end": "ok"},
            "summary": {"begin_files": 92, "added": 2, "modified": 1, "deleted": 0},
            "integrity": {n: True for n in (sandbox_state.BEGIN_MANIFEST, sandbox_state.END_MANIFEST, sandbox_state.END_DELTA)},
            "limits": {"truncated": False},
        }

    def test_healthy_bundle_is_ok(self, tmp_path):
        d = self._bundle(tmp_path, meta=self._healthy_meta())
        report = sandbox_state.validate_bundle(d, require_integrity=True)
        assert report.ok and not report.problems
        assert report.summary["added"] == 2

    def test_missing_meta_is_a_hard_failure(self, tmp_path):
        d = self._bundle(tmp_path, meta=None)
        report = sandbox_state.validate_bundle(d)
        assert not report.ok
        assert any("never completed" in p for p in report.problems)

    def test_integrity_mismatch_warns_then_fails_when_required(self, tmp_path):
        meta = self._healthy_meta()
        meta["integrity"][sandbox_state.END_DELTA] = False
        d = self._bundle(tmp_path, meta=meta)
        assert sandbox_state.validate_bundle(d).ok  # warn only
        assert not sandbox_state.validate_bundle(d, require_integrity=True).ok

    def test_bad_phase_status_fails(self, tmp_path):
        meta = self._healthy_meta()
        meta["status"]["end"] = "TimeoutError: end phase"
        assert not sandbox_state.validate_bundle(self._bundle(tmp_path, meta=meta)).ok

    def test_truncation_is_a_warning_not_a_failure(self, tmp_path):
        meta = self._healthy_meta()
        meta["limits"] = {"truncated": True, "dropped_count": 3}
        report = sandbox_state.validate_bundle(self._bundle(tmp_path, meta=meta))
        assert report.ok and any("truncated" in w for w in report.warnings)

    def test_find_bundles_locates_incomplete_ones(self, tmp_path):
        self._bundle(tmp_path, meta=None)  # bundle dir with no meta.json
        found = sandbox_state.find_bundles(tmp_path)
        assert len(found) == 1  # found despite missing meta -> reported as failed, not "not found"

    def test_validate_captures_latest_and_empty(self, tmp_path):
        assert sandbox_state.validate_captures(tmp_path / "nothing") == []
        self._bundle(tmp_path, meta=self._healthy_meta())
        reports = sandbox_state.validate_captures(tmp_path, latest=True)
        assert len(reports) == 1 and reports[0].ok
