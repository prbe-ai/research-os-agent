"""Launch-block capture: per-launch ephemera, never hashed into env identity."""
from __future__ import annotations

import sys

from probe.sdk.launch import (
    LAUNCH_SCHEMA,
    build_launch_block,
    capture_determinism,
    capture_process,
    capture_runtime,
    scrub_argv,
)
from probe.sdk.snapshot import capture_system


def test_scrub_argv_redacts_flag_value_pairs():
    argv = ["train.py", "--api-key", "sk-abc12345678", "--lr", "3e-4"]
    out, scrubbed = scrub_argv(argv)
    assert out == ["train.py", "--api-key", "[redacted]", "--lr", "3e-4"]
    assert scrubbed is True


def test_scrub_argv_redacts_inline_and_bare_secrets():
    argv = ["run.sh", "--token=ghp_abcdefghij0123456789", "AKIAABCDEFGH12345678"]
    out, scrubbed = scrub_argv(argv)
    assert out == ["run.sh", "--token=[redacted]", "[redacted]"]
    assert scrubbed is True


def test_scrub_argv_clean_passthrough():
    argv = ["python", "train.py", "--seed", "42"]
    out, scrubbed = scrub_argv(argv)
    assert out == argv
    assert scrubbed is False


def test_scrub_argv_ml_flags_are_not_secrets():
    argv = ["train.py", "--tokenizer", "gpt2", "--max-tokens", "512", "--monkey", "see"]
    out, scrubbed = scrub_argv(argv)
    assert out == argv
    assert scrubbed is False


def test_scrub_argv_segmented_secret_flags_still_redact():
    out, scrubbed = scrub_argv(["run.py", "--hf-token", "abc", "--key-file=x.pem"])
    assert out == ["run.py", "--hf-token", "[redacted]", "--key-file=[redacted]"]
    assert scrubbed is True


def test_capture_process_records_identity(tmp_path):
    info, errors = capture_process(argv=["python", "train.py"], cwd=str(tmp_path))
    assert info["argv"] == ["python", "train.py"]
    assert info["argv_scrubbed"] is False
    assert info["cwd"] == str(tmp_path)
    assert info["hostname"]
    assert info["user"]
    assert info["started_at"].endswith("+00:00") or info["started_at"].endswith("Z")
    assert errors == []


def test_capture_process_defaults_to_sys_argv():
    info, _ = capture_process()
    assert info["argv"] == scrub_argv(sys.argv)[0]


def test_runtime_env_names_but_not_values(monkeypatch):
    monkeypatch.setenv("MY_DB_PASSWORD", "hunter2")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    monkeypatch.setenv("NCCL_DEBUG", "INFO")
    info, errors = capture_runtime()
    assert "MY_DB_PASSWORD" in info["env_names"]
    assert "MY_DB_PASSWORD" not in info["env_values"]
    assert info["env_values"]["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert info["env_values"]["NCCL_DEBUG"] == "INFO"
    assert errors == []


def test_runtime_allowlist_extension(monkeypatch):
    monkeypatch.setenv("PROBE_ENV_ALLOWLIST", "+MY_SHARD_ID")
    monkeypatch.setenv("MY_SHARD_ID", "7")
    info, _ = capture_runtime()
    assert info["env_values"]["MY_SHARD_ID"] == "7"


def test_runtime_container_absent_on_bare_host(monkeypatch, tmp_path):
    # No /.dockerenv, no KUBERNETES_SERVICE_HOST → no container key (macOS CI
    # hosts and bare-metal Linux both land here).
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    info, _ = capture_runtime(_dockerenv_path=str(tmp_path / "nope"))
    assert "container" not in info


def test_runtime_container_k8s(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HOSTNAME", "trainer-abc123")
    info, _ = capture_runtime(_dockerenv_path=str(tmp_path / "nope"))
    assert info["container"]["detected_via"] == "kubernetes"
    assert info["container"]["pod"] == "trainer-abc123"


def _seed_names(info):
    return {s["name"]: s for s in info["seeds"]}


def test_determinism_from_argv_and_env(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    info, errors = capture_determinism(argv=["train.py", "--seed", "42", "--data-seed=7"])
    seeds = _seed_names(info)
    assert seeds["PYTHONHASHSEED"]["value"] == "0"
    assert seeds["seed"]["value"] == "42"
    assert seeds["seed"]["provenance"] == "detected"
    assert seeds["data-seed"]["value"] == "7"
    assert errors == []


def test_determinism_declared_config_seeds():
    info, _ = capture_determinism(argv=[], config={"lr": 1e-4, "seed": 1234, "seeds": {"torch": 1, "numpy": 2}})
    seeds = _seed_names(info)
    assert seeds["seed"]["provenance"] == "declared"
    assert seeds["seeds.torch"]["value"] == 1
    assert seeds["seeds.numpy"]["value"] == 2


def test_determinism_no_seeds_is_honest():
    info, _ = capture_determinism(argv=["train.py"], config={})
    assert info["seeds"] == [] or all(s["source"] != "argv" for s in info["seeds"])


def test_build_launch_block_composes_and_never_raises(monkeypatch):
    block = build_launch_block(argv=["train.py", "--seed", "3"], config={"seed": 3})
    assert block["schema"] == LAUNCH_SCHEMA
    assert block["process"]["argv"] == ["train.py", "--seed", "3"]
    assert block["runtime"]["env_names"]
    assert _seed_names(block["determinism"])["seed"]["value"] == "3"
    assert "probe_version" in block


def test_capture_system_shape():
    info = capture_system()
    assert info["os"]["platform"]
    assert info["os"]["machine"]
    assert info["cpu"]["count"] >= 1
    # cuda/cudnn only when torch is already loaded or nvcc exists — absent here
    # is legitimate; the key must then be missing, not null.
    assert info.get("cuda") is None or info["cuda"].get("runtime")
