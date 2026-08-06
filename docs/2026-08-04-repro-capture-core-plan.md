# Reproducibility Capture Core — Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every run automatically captures launch ephemera (argv, host, env, seeds), system identity (OS/CUDA/CPU), and lockfiles — with `probe run check` reporting the new slots honestly and an opt-in strict gate.

**Architecture:** Implements D1–D4 + D7 of `docs/2026-08-04-reproducibility-capture-enforcement-design.md`. Per-launch ephemera go in a new `src/probe/sdk/launch.py` and land in `runs.metadata["launch"]` via the existing RunPatch (verified: `RunPatch.metadata` exists in `schema/openapi.json` — zero backend changes). Machine-stable identity (OS/CUDA/CPU) joins `execution_records.hardware` (hashed). Lockfiles become force-included manifest entries whose hashes join `deps`. Capture NEVER fails a run; `PROBE_REQUIRE_COMPLETE=1` is the only hard gate.

**Tech Stack:** Python 3.11+, stdlib only (no new deps — `torch`/`numpy` are read from `sys.modules`, never imported). pytest with the existing `FakeApp`/`client` fixtures from `tests/conftest.py`.

**Repo/branch:** `research-os-agent`, new branch `feat/repro-capture-core` off `origin/main`, in a dedicated worktree (`git worktree add ~/Desktop/prbe/research-os-agent-worktrees/repro-capture-core -b feat/repro-capture-core origin/main`). Run tests with `uv run pytest tests/<file> -x -q` from the worktree root.

**Plans 2–3 (NOT here):** backend `/reproduce` endpoints (research-os); MCP views + CLI `reproduce`/`freeze` verbs + skill updates.

**Source anchors (verified at `origin/main` 82a92f2):**
- `src/probe/sdk/snapshot.py:166` `pushed_base` (the N+1), `:287` `_include_entries`, `:357-374` SKIP_DIRS/SKIP_SECRETS, `:485` `capture_manifest`, `:389` `capture_directory_manifest`, `:998` `capture_gpu`
- `src/probe/sdk/run.py:1267` `Run.snapshot()`, `:1646` `Run.execute()`, `:1565` `Run.finish()`
- `src/probe/sdk/client.py:1484` `Client.run()`, `:2428` `Client.check_run()`
- `src/probe/cli/main.py:2582` `exec` command, `run_check` at the `@run_app.command("check")` decorator
- Tests: `tests/test_snapshot_capture.py` (the `repo` fixture pattern), `tests/conftest.py:1634` `app` / `:1639` `client` fixtures, `open_run` helper at `:1643`

---

### Task 1: Batch `pushed_base` (perf prerequisite)

The per-remote-head loop spawns ~3 git processes per branch (2.6–6.6s on this repo). Replace with two invocations: `git cat-file --batch-check` fed all advertised SHAs, then `git rev-list --boundary HEAD --not <present>`. Boundary lines (`-` prefix) are the pushed frontier; first boundary line = newest pushed ancestor; empty output = HEAD itself pushed. Semantics preserved: remote head not present locally = NOT pushed; root with no pushed ancestor → `(None, None)`. `--boundary` is merge-safe (computes the frontier directly instead of max-over-pairwise-merge-bases).

**Files:**
- Modify: `src/probe/sdk/snapshot.py` (`pushed_base`, lines ~200–233 — keep everything above the `shas` loop)
- Test: `tests/test_snapshot_capture.py`

- [ ] **Step 1: Write the failing merge-safety test**

Append to `tests/test_snapshot_capture.py` (uses its existing `repo` fixture and `_git` helper):

```python
def test_pushed_base_merge_commit_frontier(repo):
    """After merging a pushed branch into unpushed work, the base must be the
    merge-frontier commit, not an older pairwise merge-base approximation."""
    _git(repo, "checkout", "-qb", "side")
    (repo / "side.py").write_text("s = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/side")
    side_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    (repo / "local.py").write_text("l = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    base, remote = pushed_base(str(repo))
    # Both parents' pushed ancestors are on the frontier; the newest pushed
    # ancestor must be the side tip (committed after main's pushed commit).
    assert base == side_tip
    assert remote


def test_pushed_base_root_never_pushed(tmp_path):
    """A repo with a remote configured but nothing ever pushed → (None, None)."""
    remote = tmp_path / "r.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "w"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(remote))
    (work / "a.py").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    assert pushed_base(str(work)) == (None, None)
```

- [ ] **Step 2: Run to verify current behavior** — `uv run pytest tests/test_snapshot_capture.py -x -q`. The merge test may PASS on the old code (pairwise can get this right); that is fine — it pins the frontier semantics the rewrite must keep. The root test should already pass. If both pass, they are regression pins, not red tests; proceed.

- [ ] **Step 3: Replace the loop.** In `pushed_base`, keep everything through the `shas` list build, delete `_is_ancestor` and the `for sha in shas:` loop, and replace with:

```python
    if not shas:
        return None, None

    # One `cat-file --batch-check` for every advertised SHA instead of one
    # `cat-file -e` process per branch. A remote head we do not have locally is
    # treated as NOT pushed, same rule as before. (~6000 branches would hit
    # ARG_MAX on the rev-list below; switch to `rev-list --stdin` if that
    # ever becomes real.)
    probe = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=cwd, input="\n".join(shas) + "\n",
        capture_output=True, text=True,
    )
    present = []
    for line in probe.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit":
            present.append(parts[0])
    if not present:
        return None, None

    # `--boundary` computes the pushed frontier directly, which is merge-safe;
    # the old max-over-pairwise-merge-bases only approximated it. Boundary
    # lines are emitted newest-first, so the first is the newest pushed
    # ancestor. Empty output means HEAD is reachable from an advertised head.
    out = subprocess.run(
        ["git", "rev-list", "--boundary", "HEAD", "--not", *present],
        cwd=cwd, capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None, None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not lines:
        return head, _remote_url(cwd, remote)
    boundary = [ln[1:] for ln in lines if ln.startswith("-")]
    if not boundary:
        return None, None
    return boundary[0], _remote_url(cwd, remote)
```

- [ ] **Step 4: Run the full snapshot suites** — `uv run pytest tests/test_snapshot_capture.py tests/test_snapshot.py tests/test_snapshot_include.py tests/test_snapshot_nongit.py tests/test_snapshot_upload.py tests/test_snapshot_venv.py tests/test_snapshot_restore.py -q`. Expected: all PASS. If a pushed_base test disagrees on which commit wins, the OLD semantics are the contract — fix the implementation, not the test.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "perf(snapshot): batch pushed_base into two git invocations"`

---

### Task 2: `launch.py` — `scrub_argv` + `capture_process`

**Files:**
- Create: `src/probe/sdk/launch.py`
- Test: create `tests/test_launch.py`

- [ ] **Step 1: Write failing tests**

```python
"""Launch-block capture: per-launch ephemera, never hashed into env identity."""
from __future__ import annotations

import os
import sys

from probe.sdk.launch import capture_process, scrub_argv


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
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_launch.py -x -q`. Expected: FAIL, `ModuleNotFoundError: probe.sdk.launch`.

- [ ] **Step 3: Implement**

```python
"""Per-launch ephemera: what was true of THIS process start on THIS machine.

Everything here lands in the run's ``metadata["launch"]`` block, never in the
content-addressed execution record — argv, hostnames, and env values differ per
launch, and hashing them would mint a unique execution record per run and
destroy dedup (design doc D1, docs/2026-08-04-reproducibility-capture-enforcement-design.md).

Every capture is best-effort: failures collect into an ``errors`` list and
``build_launch_block`` never raises. Absence is recorded, not guessed.
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

LAUNCH_SCHEMA = "probe.launch/1"

# Secret-shaped flags and bare values. Redaction is marked, so a reader knows
# the command line is not verbatim.
_SECRET_FLAG = re.compile(
    r"^--?[\w-]*(key|token|secret|password|passwd|credential)[\w-]*(=.*)?$", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    r"^(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{12,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+)"
)


def scrub_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Redact secret-shaped tokens; return (argv, whether anything was redacted)."""
    out: list[str] = []
    scrubbed = False
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("[redacted]")
            scrubbed = True
            redact_next = False
            continue
        if _SECRET_FLAG.match(arg):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=[redacted]")
                scrubbed = True
            else:
                out.append(arg)
                redact_next = True
            continue
        if _SECRET_VALUE.match(arg):
            out.append("[redacted]")
            scrubbed = True
            continue
        out.append(arg)
    return out, scrubbed


#: Process names that identify HOW a run was launched, when seen in the
#: parent chain. Shells are recorded in the chain but are not "launchers".
_LAUNCHERS = ("sbatch", "srun", "slurmstepd", "kubelet", "containerd-shim", "dockerd")


def _parent_chain(max_hops: int = 10) -> tuple[list[str], str | None]:
    """Walk parent processes to a recognized launcher. Best-effort, bounded."""
    chain: list[str] = []
    launcher: str | None = None
    pid = os.getppid()
    for _ in range(max_hops):
        if pid <= 1:
            break
        name = None
        try:  # Linux
            with open(f"/proc/{pid}/comm") as fh:
                name = fh.read().strip()
            with open(f"/proc/{pid}/stat") as fh:
                pid = int(fh.read().split()[3])
        except OSError:
            try:  # macOS / no procfs
                out = subprocess.run(
                    ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2,
                )
                parts = out.stdout.split(None, 1)
                if len(parts) != 2:
                    break
                name = os.path.basename(parts[1].strip())
                pid = int(parts[0])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                break
        if not name:
            break
        chain.append(name)
        if launcher is None and any(name.startswith(l) for l in _LAUNCHERS):
            launcher = name
    return chain, launcher


def capture_process(
    argv: list[str] | None = None, cwd: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Identity of this launch: command, place, machine, user, lineage."""
    errors: list[str] = []
    raw = list(argv) if argv is not None else list(sys.argv)
    scrubbed_argv, scrubbed = scrub_argv(raw)
    info: dict[str, Any] = {
        "argv": scrubbed_argv,
        "argv_scrubbed": scrubbed,
        "cwd": os.path.abspath(cwd or os.getcwd()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    entry = raw[0] if raw else None
    if entry:
        info["entrypoint"] = os.path.abspath(entry) if os.path.exists(entry) else entry
    try:
        info["hostname"] = socket.gethostname()
    except OSError as exc:
        errors.append(f"hostname: {exc}")
    try:
        info["user"] = getpass.getuser()
    except Exception as exc:  # getuser can raise KeyError on daemon UIDs
        errors.append(f"user: {exc}")
    chain, launcher = _parent_chain()
    if chain:
        info["parent_chain"] = chain
    if launcher:
        info["launcher"] = launcher
    return info, errors
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_launch.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(sdk): launch.py — scrubbed process-identity capture"`

---

### Task 3: `capture_runtime` (env names + allowlisted values + container)

**Files:** Modify `src/probe/sdk/launch.py`; Test `tests/test_launch.py`

- [ ] **Step 1: Write failing tests**

```python
from probe.sdk.launch import capture_runtime


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
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_launch.py -x -q`. Expected: FAIL, `ImportError: capture_runtime`.

- [ ] **Step 3: Implement** (append to `launch.py`)

```python
#: Values captured only for these names — everything else is name-only.
#: Extensible per-site via PROBE_ENV_ALLOWLIST="+VAR1,VAR2". NCCL_* is
#: value-captured wholesale: knob names are the reproduction surface there.
ENV_ALLOWLIST = frozenset({
    "CUDA_VISIBLE_DEVICES", "WORLD_SIZE", "RANK", "LOCAL_RANK",
    "MASTER_ADDR", "MASTER_PORT", "OMP_NUM_THREADS", "PYTHONHASHSEED",
    "SLURM_JOB_ID", "SLURM_ARRAY_TASK_ID",
})


def _allowlist() -> frozenset[str]:
    extra = os.environ.get("PROBE_ENV_ALLOWLIST", "")
    names = {v.strip() for v in extra.lstrip("+").split(",") if v.strip()}
    return ENV_ALLOWLIST | names


def _detect_container(_dockerenv_path: str = "/.dockerenv") -> dict[str, Any] | None:
    """Container context, provenance-tagged. Image identity is PROVENANCE, not
    correctness (design doc D1 / the 2026-07-29 begin-state-bytes decision)."""
    via = None
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        via = "kubernetes"
    elif os.path.exists(_dockerenv_path):
        via = "dockerenv"
    else:
        try:
            with open("/proc/1/cgroup") as fh:
                text = fh.read()
            if any(marker in text for marker in ("docker", "containerd", "kubepods")):
                via = "cgroup"
        except OSError:
            pass
    if via is None:
        return None
    info: dict[str, Any] = {"detected_via": via}
    image = os.environ.get("PROBE_CONTAINER_IMAGE") or os.environ.get("IMAGE")
    if image:
        info["image"] = image
    if via == "kubernetes" and os.environ.get("HOSTNAME"):
        info["pod"] = os.environ["HOSTNAME"]
    return info


def capture_runtime(
    _dockerenv_path: str = "/.dockerenv",
) -> tuple[dict[str, Any], list[str]]:
    """Env-var names (all), values (allowlist + NCCL_* only), container context."""
    errors: list[str] = []
    allow = _allowlist()
    names = sorted(os.environ)
    info: dict[str, Any] = {
        "env_names": names,
        "env_values": {
            k: os.environ[k] for k in names if k in allow or k.startswith("NCCL_")
        },
    }
    container = _detect_container(_dockerenv_path)
    if container:
        info["container"] = container
    return info, errors
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_launch.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(sdk): capture_runtime — env names, allowlisted values, container"`

---

### Task 4: `capture_determinism` + `build_launch_block`

**Files:** Modify `src/probe/sdk/launch.py`; Test `tests/test_launch.py`

- [ ] **Step 1: Write failing tests**

```python
from probe.sdk.launch import LAUNCH_SCHEMA, build_launch_block, capture_determinism


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
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_launch.py -x -q`. Expected: FAIL, ImportError.

- [ ] **Step 3: Implement** (append to `launch.py`)

```python
_ARGV_SEED = re.compile(r"^--?([\w-]*seed[\w-]*?)(?:=(.*))?$", re.IGNORECASE)


def capture_determinism(
    argv: list[str] | None = None, config: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Seed evidence with provenance. `detected` = observed from the outside
    (env, argv, an already-imported framework); `declared` = the caller put it
    in config. Absence of seeds is recorded as an empty list — honest, not an
    error."""
    errors: list[str] = []
    seeds: list[dict[str, Any]] = []
    hashseed = os.environ.get("PYTHONHASHSEED")
    if hashseed is not None:
        seeds.append({
            "name": "PYTHONHASHSEED", "value": hashseed,
            "provenance": "detected", "source": "env",
        })
    args = list(argv) if argv is not None else list(sys.argv)
    for i, arg in enumerate(args):
        m = _ARGV_SEED.match(arg)
        if not m:
            continue
        value = m.group(2)
        if value is None and i + 1 < len(args) and not args[i + 1].startswith("-"):
            value = args[i + 1]
        seeds.append({
            "name": m.group(1), "value": value,
            "provenance": "detected", "source": "argv",
        })
    for key, value in sorted((config or {}).items()):
        if key == "seeds" and isinstance(value, dict):
            for sub, sval in sorted(value.items()):
                seeds.append({
                    "name": f"seeds.{sub}", "value": sval,
                    "provenance": "declared", "source": "config",
                })
        elif re.search(r"(^|_)seed(s)?$", key, re.IGNORECASE):
            seeds.append({
                "name": key, "value": value,
                "provenance": "declared", "source": "config",
            })
    # Frameworks are read from sys.modules, NEVER imported: importing torch
    # costs seconds and pulls CUDA context into a process that may not want it.
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            seeds.append({
                "name": "torch.initial_seed", "value": int(torch.initial_seed()),
                "provenance": "detected", "source": "torch",
            })
        except Exception as exc:
            errors.append(f"torch seed: {exc}")
    if "numpy" in sys.modules:
        seeds.append({
            "name": "numpy.random", "value": None,
            "provenance": "detected", "source": "numpy",
            "note": "generator present; state not captured",
        })
    return {"seeds": seeds}, errors


def build_launch_block(
    *,
    argv: list[str] | None = None,
    cwd: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the launch block. NEVER raises — a capture bug must not take
    down the run it is documenting (design principle: block claims, not runs)."""
    from probe import __version__

    errors: list[str] = []
    block: dict[str, Any] = {"schema": LAUNCH_SCHEMA, "probe_version": __version__}
    try:
        block["process"], errs = capture_process(argv=argv, cwd=cwd)
        errors += errs
    except Exception as exc:
        errors.append(f"process: {exc}")
    try:
        block["runtime"], errs = capture_runtime()
        errors += errs
    except Exception as exc:
        errors.append(f"runtime: {exc}")
    try:
        block["determinism"], errs = capture_determinism(argv=argv, config=config)
        errors += errs
    except Exception as exc:
        errors.append(f"determinism: {exc}")
    if errors:
        block["errors"] = errors
    return block
```

If `from probe import __version__` is not importable at module scope in tests, keep it inside the function (as written) — it resolves the installed dist lazily.

- [ ] **Step 4: Run** — `uv run pytest tests/test_launch.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(sdk): capture_determinism + build_launch_block"`

---

### Task 5: `capture_system` (stable identity → `execution_records.hardware`)

**Files:** Modify `src/probe/sdk/snapshot.py` (next to `capture_gpu`, line ~998); Test `tests/test_launch.py` (it's capture; file is fine)

- [ ] **Step 1: Write failing tests**

```python
from probe.sdk.snapshot import capture_system


def test_capture_system_shape():
    info = capture_system()
    assert info["os"]["platform"]
    assert info["os"]["machine"]
    assert info["cpu"]["count"] >= 1
    # cuda/cudnn only when torch is already loaded or nvcc exists — absent here
    # is legitimate; the key must then be missing, not null.
    assert info.get("cuda") is None or info["cuda"].get("runtime")
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_launch.py::test_capture_system_shape -x -q`. Expected: FAIL, ImportError.

- [ ] **Step 3: Implement** (in `snapshot.py`, after `capture_gpu`; add `import platform` at top; `re`, `subprocess`, `sys` are already imported — verify before adding)

```python
def capture_system() -> dict[str, Any]:
    """Machine-stable identity: OS, CPU/RAM, CUDA stack. Joins the GPU inventory
    under ``execution_records.hardware`` — hashed into env identity, which is
    correct because two runs on identical nodes must still share one record.
    Per-launch facts (hostname, env values) belong in the launch block instead."""
    info: dict[str, Any] = {
        "os": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
        },
        "cpu": {"count": os.cpu_count() or 0},
    }
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    info["os"]["distro"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    libc = platform.libc_ver()
    if libc[0]:
        info["os"]["libc"] = f"{libc[0]} {libc[1]}"
    try:
        names = os.sysconf_names
        if "SC_PAGE_SIZE" in names and "SC_PHYS_PAGES" in names:
            info["cpu"]["mem_total_bytes"] = (
                os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            )
    except (OSError, ValueError, AttributeError):
        pass
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            runtime = getattr(getattr(torch, "version", None), "cuda", None)
            if runtime:
                cuda: dict[str, Any] = {"runtime": runtime}
                cudnn = torch.backends.cudnn.version()
                if cudnn:
                    cuda["cudnn"] = cudnn
                info["cuda"] = cuda
        except Exception:
            pass
    else:
        try:
            out = subprocess.run(
                ["nvcc", "--version"], capture_output=True, text=True, timeout=5
            )
            m = re.search(r"release ([\d.]+)", out.stdout)
            if m:
                info["cuda"] = {"runtime": m.group(1)}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_launch.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(sdk): capture_system — OS/CPU/CUDA into hardware identity"`

---

### Task 6: Lockfile force-include in manifests

**Files:** Modify `src/probe/sdk/snapshot.py` (`capture_manifest` ~:485, `capture_directory_manifest` ~:389); Test `tests/test_snapshot_capture.py`, `tests/test_snapshot_nongit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_snapshot_capture.py`:

```python
def test_gitignored_lockfile_is_captured(repo):
    """A gitignored uv.lock must still enter the manifest as an uploadable blob —
    today it is silently lost (design doc: the dirty-lockfile case)."""
    (repo / "uv.lock").write_text("[lock]\nversion = 1\n")
    (repo / ".gitignore").write_text("uv.lock\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore lockfile")
    m = capture_manifest(str(repo))
    by_path = _by_path(m)
    assert by_path["uv.lock"]["source"] == "blob"
    assert by_path["uv.lock"]["lockfile"] is True


def test_tracked_lockfile_is_tagged(repo):
    (repo / "requirements.txt").write_text("httpx==0.27.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "reqs")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    m = capture_manifest(str(repo))
    entry = _by_path(m)["requirements.txt"]
    assert entry["source"] == "git"          # clean+pushed stays a reference
    assert entry["lockfile"] is True          # but identity still names it


def test_oversized_lockfile_reported_not_shipped(repo):
    (repo / "package-lock.json").write_text("x" * (1024 * 1024 + 1))
    (repo / ".gitignore").write_text("package-lock.json\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore")
    m = capture_manifest(str(repo))
    assert "package-lock.json" not in _by_path(m)
    assert {"path": "package-lock.json", "reason": "lockfile_too_large"} in m["skipped"]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_snapshot_capture.py -x -q`. Expected: FAIL (`KeyError: 'uv.lock'`).

- [ ] **Step 3: Implement.** In `snapshot.py`, near `SKIP_DIRS`:

```python
#: Root-level dependency descriptors captured as FILES, not just as the
#: enumerated interpreter packages — the lockfile is what a rebuild consumes,
#: and a dirty/gitignored one was previously lost with no record.
LOCKFILE_NAMES = (
    "uv.lock", "poetry.lock", "pyproject.toml", "environment.yml",
    "package-lock.json", "Cargo.lock",
)
DEFAULT_MAX_LOCKFILE_BYTES = 1024 * 1024


def _is_lockfile(name: str) -> bool:
    return name in LOCKFILE_NAMES or fnmatch.fnmatch(name, "requirements*.txt")
```

(Add `import fnmatch` to the module imports.) Then in `capture_manifest`, after the `include` handling and BEFORE the digest loop:

```python
    skipped: list[dict[str, str]] = []
    have = {e["path"] for e in entries}
    for name in sorted(os.listdir(cwd)):
        if not _is_lockfile(name):
            continue
        full = os.path.join(cwd, name)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        if name in have:
            continue  # tracked or included already; tagged below
        sha, size = _file_sha256(full)
        if size > DEFAULT_MAX_LOCKFILE_BYTES:
            skipped.append({"path": name, "reason": "lockfile_too_large"})
            continue
        entries.append({
            "path": name, "mode": "100644", "sha256": sha, "size": size,
            "source": "blob", "lockfile": True,
        })
    for e in entries:
        if "/" not in e["path"] and _is_lockfile(e["path"]):
            e["lockfile"] = True
    entries.sort(key=lambda e: e["path"])
```

and extend the returned dict with `"skipped": skipped` (git path previously had no `skipped` key; consumers already tolerate it — `Run.snapshot` reads it with `.get`). Apply the same tagging loop (root-level `_is_lockfile` → `"lockfile": True`) inside `capture_directory_manifest` before its digest loop — the walk already includes the files there; only the tag is needed.

- [ ] **Step 4: Run** — `uv run pytest tests/test_snapshot_capture.py tests/test_snapshot_nongit.py tests/test_snapshot_include.py tests/test_snapshot_upload.py -q`. Expected: PASS (nongit suite may need one added assertion mirroring `test_tracked_lockfile_is_tagged` for a walked lockfile — add it there).
- [ ] **Step 5: Commit** — `git commit -am "feat(snapshot): force-capture root lockfiles, tagged and size-capped"`

---

### Task 7: `Run.snapshot()` — launch block + lockfile deps + single PATCH

**Files:** Modify `src/probe/sdk/run.py` (`snapshot`, :1267); Test `tests/test_sdk.py` (or a new `tests/test_snapshot_launch.py` using conftest's `client` fixture + `open_run`)

- [ ] **Step 1: Write failing test** (new file `tests/test_snapshot_launch.py`)

```python
"""Run.snapshot() writes the launch block and lockfile identity."""
from __future__ import annotations

from tests.conftest import open_run


def test_snapshot_writes_launch_block(client, tmp_path, monkeypatch):
    (tmp_path / "train.py").write_text("print('hi')\n")
    (tmp_path / "uv.lock").write_text("[lock]\n")
    run = open_run(client, experiment="exp-launch")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False,
                 argv=["python", "train.py", "--seed", "9"])
    row = client.get_run(run.id)
    launch = (row.get("metadata") or {}).get("launch")
    assert launch["schema"] == "probe.launch/1"
    assert launch["process"]["argv"] == ["python", "train.py", "--seed", "9"]
    seeds = {s["name"]: s for s in launch["determinism"]["seeds"]}
    assert seeds["seed"]["value"] == "9"
    assert row.get("env_ref")


def test_snapshot_merges_metadata_not_clobbers(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-meta", metadata={"owner": "mahit"})
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    row = client.get_run(run.id)
    assert row["metadata"]["owner"] == "mahit"
    assert "launch" in row["metadata"]


def test_snapshot_deps_include_lockfile_hashes(client, tmp_path):
    (tmp_path / "uv.lock").write_text("[lock]\nversion = 1\n")
    run = open_run(client, experiment="exp-lock")
    snap = run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    locks = snap["deps"].get("lockfiles")
    assert locks and locks[0]["path"] == "uv.lock" and locks[0]["sha256"]
```

Adjust helper names to what `tests/conftest.py` actually exposes (`open_run(client, *, experiment=...)` exists at conftest:1643; `client.get_run` — if the accessor is named differently, e.g. `client.run_bundle(run.id)["run"]`, use that; the FakeApp mirrors PATCH metadata).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_snapshot_launch.py -x -q`. Expected: FAIL — `snapshot() got an unexpected keyword argument 'argv'`.

- [ ] **Step 3: Implement.** In `run.py`:

1. Add `from . import launch as _launch` next to the `_snapshot` import.
2. Add `argv: list[str] | None = None,` to the `snapshot()` signature (document: "the command this run executes, when the caller launches a child process — defaults to this process's argv").
3. Replace the env_ref PATCH block (`if content_hash is not None: data = self._client.write("PATCH", ...)`) with one combined patch:

```python
        # Launch ephemera ride the SAME patch as env_ref: one write, and the
        # metadata merge is client-side (read-modify-write) because RunPatch
        # REPLACES metadata. Single-writer per run is the operating assumption
        # (probe exec holds the run lock; SDK runs are process-bound).
        launch_block = _launch.build_launch_block(
            argv=argv, cwd=cwd, config=(self._data or {}).get("config"),
        )
        current_meta = dict((self._data or {}).get("metadata") or {})
        patch_body: dict = {"metadata": {**current_meta, "launch": launch_block}}
        if content_hash is not None:
            patch_body["env_ref"] = content_hash
        data = self._client.write("PATCH", f"/v1/runs/{self.id}", patch_body, strict=strict)
        if data:
            self._data = data
            if content_hash is not None and data.get("env_ref") != content_hash:
                ...  # keep the existing env_ref mismatch warn/raise block verbatim
```

4. Lockfile identity into deps, just before `ExecutionRecordCreate`:

```python
        lockfiles = sorted(
            ({"path": e["path"], "sha256": e["sha256"]}
             for e in manifest["entries"] if e.get("lockfile")),
            key=lambda item: item["path"],
        )
        if lockfiles:
            deps = {**deps, "lockfiles": lockfiles}
```

5. Hardware gains system identity: `hardware={"gpu": _snapshot.capture_gpu(), **_snapshot.capture_system()} if include_gpu else {}` — and add `"n_lockfiles": len(lockfiles)` plus `"launch_errors": launch_block.get("errors")` into the code-snapshot artifact meta dict (for `check_run`, Task 9). NOTE the ordering change: the combined PATCH must run BEFORE `log_artifact` (it already does — keep the current position of the patch block, just merged).
6. Return dict gains `"launch": launch_block`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_snapshot_launch.py tests/test_sdk.py tests/test_snapshot_upload.py -q`. Expected: PASS. The FakeApp must reflect metadata PATCH; if it drops it, extend the FakeApp's run-PATCH handler in `tests/conftest.py` to merge `metadata` into the stored row — mirroring the real server's RunPatch (verified present in `schema/openapi.json`).
- [ ] **Step 5: Commit** — `git commit -am "feat(sdk): snapshot writes launch block + lockfile identity in one patch"`

---

### Task 8: Auto-snapshot on `probe exec` and `Client.run()`

**Files:** Modify `src/probe/sdk/run.py` (`execute`, :1646), `src/probe/sdk/client.py` (`run`, :1484); Test `tests/test_snapshot_launch.py`

- [ ] **Step 1: Write failing tests**

```python
def test_execute_auto_snapshots(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print('ok')\n")
    run = open_run(client, experiment="exp-exec")
    run.execute(["python", "job.py"], cwd=str(tmp_path))
    row = client.get_run(run.id)
    launch = (row.get("metadata") or {}).get("launch")
    assert launch is not None
    assert launch["process"]["argv"] == ["python", "job.py"]


def test_execute_auto_snapshot_opt_out(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "0")
    run = open_run(client, experiment="exp-exec-off")
    run.execute(["python", "-c", "pass"], cwd=str(tmp_path))
    row = client.get_run(run.id)
    assert "launch" not in (row.get("metadata") or {})


def test_execute_survives_snapshot_failure(client, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("capture exploded")
    monkeypatch.setattr(type(open_run(client, experiment="exp-x")), "snapshot", boom)
    run = open_run(client, experiment="exp-boom")
    result = run.execute(["python", "-c", "pass"], cwd=str(tmp_path))
    assert result.returncode == 0  # block claims, never runs
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_snapshot_launch.py -x -q`. Expected: first test FAILS (`launch is None`).

- [ ] **Step 3: Implement.**

In `Run.execute()`, immediately after the `argv` empty-check and before `started_at = _now()`:

```python
        # Every executed run snapshots by default (design D3). detect_venv=True
        # because THIS interpreter is the launcher, not the environment being
        # recorded — see Run.snapshot's docstring. Failure never blocks the
        # child: strict mode is the only exception, and it is opt-in.
        if os.environ.get("PROBE_AUTO_SNAPSHOT", "1") != "0":
            try:
                self.snapshot(cwd=cwd, detect_venv=True, argv=argv)
            except Exception as exc:
                if os.environ.get("PROBE_REQUIRE_COMPLETE") == "1":
                    raise
                warnings.warn(
                    f"auto-snapshot failed; run continues uncaptured: {exc}",
                    stacklevel=2,
                )
```

(`warnings` is already imported in run.py.) In `Client.run()`, find the point where the `Run` handle is created and returned (the tail of the method past the on_conflict handling); add a `snapshot: bool | None = None` keyword to the signature (docstring: "None = follow PROBE_AUTO_SNAPSHOT (default on); False = skip; True = force") and before returning the handle:

```python
        auto = snapshot if snapshot is not None else (
            os.environ.get("PROBE_AUTO_SNAPSHOT", "1") != "0"
        )
        if auto:
            try:
                handle.snapshot()  # in-process: THIS interpreter is the env
            except Exception as exc:
                if os.environ.get("PROBE_REQUIRE_COMPLETE") == "1":
                    raise
                warnings.warn(
                    f"auto-snapshot failed; run continues uncaptured: {exc}",
                    stacklevel=2,
                )
        return handle
```

(Name the local variable to match the method's actual return variable.) A run opened via `client.run()` and then driven through `run.execute()` snapshots twice; the second is cheap by construction (same tree → same `tree_sha256` → same execution record via server dedupe, code-bytes skipped by the presign `have` check) and overwrites the launch block with the more specific child argv — desirable, note it in the docstring.

**Watch out:** conftest has autouse fixtures suppressing heartbeats/outbox; many existing tests call `client.run()` and will now attempt a snapshot against the FakeApp inside a git repo (the probe repo itself). If suites slow down or fail on unrelated tests, set `PROBE_AUTO_SNAPSHOT=0` in an autouse fixture in `tests/conftest.py` (next to `_no_background_heartbeat`) and have the Task-8 tests monkeypatch it back to `"1"`. Do this proactively — it keeps 550+ existing tests hermetic.

- [ ] **Step 4: Run the full suite** — `uv run pytest tests/ -q -x --ignore=tests/test_harbor_real_trial.py`. Expected: PASS (Docker/harbor-marked tests auto-skip).
- [ ] **Step 5: Commit** — `git commit -am "feat: auto-snapshot on probe exec and client.run (PROBE_AUTO_SNAPSHOT gate)"`

---

### Task 9: `check_run` — launch slots + advisories

New verdict inputs. Backward-compat rule (documented deviation, rationale in the design doc's honesty principle): a run with NO launch block at all predates capture-core → `advisories: ["launch_context"]`, state unchanged (otherwise every historical run flips to incomplete and exit-2 gates become noise during migration). A launch block that EXISTS but is missing a slot or recorded errors = capture genuinely failed → `missing: ["launch_<slot>"]`, state incomplete. `inputs_decision` and `notes` are judgment slots → advisories only.

**Files:** Modify `src/probe/sdk/client.py` (`check_run`, :2428), `src/probe/cli/main.py` (`run_check` docstring only); Test: new `tests/test_run_check_launch.py`

- [ ] **Step 1: Write failing tests**

```python
"""check_run: launch-slot verdicts and advisories."""
from __future__ import annotations

from tests.conftest import open_run


def _checked(client, run):
    return client.check_run(run.id)


def test_legacy_run_without_launch_is_advisory_not_incomplete(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-legacy")
    import os
    os.environ["PROBE_AUTO_SNAPSHOT"] = "0"
    try:
        run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    finally:
        del os.environ["PROBE_AUTO_SNAPSHOT"]
    # Simulate a pre-capture-core row: strip the launch block the snapshot wrote.
    client.transport.patch(f"/v1/runs/{run.id}", {"metadata": {}})
    result = _checked(client, run)
    assert "launch_context" in result["advisories"]
    assert result["state"] != "incomplete" or "launch_context" not in result["missing"]


def test_partial_launch_block_is_incomplete(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-partial")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    row = client.get_run(run.id)
    broken = dict(row["metadata"])
    broken["launch"] = {k: v for k, v in broken["launch"].items() if k != "determinism"}
    client.transport.patch(f"/v1/runs/{run.id}", {"metadata": broken})
    result = _checked(client, run)
    assert "launch_determinism" in result["missing"]
    assert result["state"] == "incomplete"


def test_complete_launch_block_stays_unverified(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-ok")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    result = _checked(client, run)
    assert not any(m.startswith("launch_") for m in result["missing"])
    assert result["state"] == "unverified"


def test_notes_and_inputs_decision_are_advisories(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-adv")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    result = _checked(client, run)
    assert "inputs_decision" in result["advisories"]
    assert "notes" in result["advisories"]
    assert "inputs_decision" not in result["missing"]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_run_check_launch.py -x -q`. Expected: FAIL, `KeyError: 'advisories'`.

- [ ] **Step 3: Implement.** In `check_run`, after the `pending_code_bytes` check and before the `verified = None` line:

```python
        advisories: list[str] = []
        launch = metadata.get("launch") or {}
        if not launch:
            # Pre-capture-core run: honest advisory, not a verdict flip —
            # otherwise every historical run reads incomplete and the exit-2
            # gate becomes noise during migration.
            advisories.append("launch_context")
        else:
            for slot in ("process", "runtime", "determinism"):
                if not launch.get(slot):
                    missing.append(f"launch_{slot}")
            if launch.get("errors"):
                advisories.append("launch_errors")
        n_lockfiles = snapshot_meta.get("n_lockfiles")
        if isinstance(n_lockfiles, int) and n_lockfiles == 0:
            advisories.append("no_lockfiles")
        if not any(a.get("kind") == "inputs_decision" for a in artifacts):
            advisories.append("inputs_decision")
        if not run.get("notes") and not any(a.get("kind") == "note" for a in artifacts):
            advisories.append("notes")
```

and add `"advisories": advisories,` to the returned dict. Update the `check_run` docstring: advisories are reported-not-verdict slots (judgment calls and legacy gaps); `missing` remains the verdict input. Update the CLI `run_check` help text with one line: "`advisories` lists reported-but-not-blocking gaps (notes, inputs-decision, legacy runs without launch context)."

- [ ] **Step 4: Run** — `uv run pytest tests/test_run_check_launch.py tests/test_cli.py -q` (plus any existing check_run tests: `grep -rl check_run tests/`). Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(sdk): run check learns launch slots + advisories"`

---

### Task 10: Strict mode (`PROBE_REQUIRE_COMPLETE`)

**Files:** Modify `src/probe/sdk/run.py` (`finish`, :1565), `src/probe/sdk/errors.py` (locate via `ls src/probe/sdk/` — the module `run.py` imports as `from . import errors`), `src/probe/cli/main.py` (`exec` command, :2582); Test `tests/test_run_check_launch.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest

from probe.sdk import errors as sdk_errors


def test_strict_finish_raises_on_incomplete(client, tmp_path, monkeypatch):
    run = open_run(client, experiment="exp-strict")  # no snapshot at all
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "0")
    monkeypatch.setenv("PROBE_REQUIRE_COMPLETE", "1")
    with pytest.raises(sdk_errors.CaptureIncomplete) as exc_info:
        run.finish("completed")
    assert "execution_record" in str(exc_info.value)


def test_strict_finish_passes_on_captured_run(client, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-strict-ok")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    monkeypatch.setenv("PROBE_REQUIRE_COMPLETE", "1")
    run.finish("completed")  # must not raise: advisories never block


def test_default_finish_never_checks(client, monkeypatch):
    run = open_run(client, experiment="exp-lax")
    monkeypatch.delenv("PROBE_REQUIRE_COMPLETE", raising=False)
    run.finish("completed")  # no snapshot, no gate, no error
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_run_check_launch.py -x -q`. Expected: FAIL — `AttributeError: CaptureIncomplete`.

- [ ] **Step 3: Implement.**

In the sdk errors module, alongside the existing `RosError` subclasses:

```python
class CaptureIncomplete(RosError):
    """PROBE_REQUIRE_COMPLETE=1 and `check_run` found the capture incomplete.

    Raised at finish()/exec-launch, never mid-run: strict mode gates the CLAIM
    that a run is done, not the run itself."""
```

(Match the module's actual base-class name — grep `class.*Error` there first.) In `Run.finish()`, at the top of the method before the status write:

```python
        if os.environ.get("PROBE_REQUIRE_COMPLETE") == "1":
            result = self._client.check_run(self.id)
            if result.get("state") == "incomplete":
                raise errors.CaptureIncomplete(
                    "run capture incomplete: " + ", ".join(result.get("missing", []))
                )
```

In the CLI `exec` command, add `strict: bool = typer.Option(False, "--strict", help="fail fast if capture is incomplete (sets PROBE_REQUIRE_COMPLETE=1)")` and at the top of the body: `if strict: os.environ["PROBE_REQUIRE_COMPLETE"] = "1"` (the env var propagates to the auto-snapshot gate in `execute` and to the child process via `process_env`).

- [ ] **Step 4: Run** — `uv run pytest tests/test_run_check_launch.py tests/test_lifecycle.py tests/test_cli.py -q`. Expected: PASS. **Watch out:** `Run.__exit__` calls `finish()`; the strict gate must not mask an in-flight exception — keep the gate as the FIRST statement so a strict failure in `__exit__` after a body exception still surfaces the body's exception (`__exit__` swallowing rules); if `test_fluent`/`test_lifecycle` show interference, move the gate to fire only when `exc_type is None` by checking nothing — simplest correct form: leave `finish()` as-is and rely on the raise; verify the suites.
- [ ] **Step 5: Commit** — `git commit -am "feat: PROBE_REQUIRE_COMPLETE strict gate on finish/exec"`

---

### Task 11: Release hygiene + full gate

**Files:** Modify `CHANGELOG.md`; version per repo convention (check how the last few releases bumped: `git log --oneline -15 -- pyproject.toml CHANGELOG.md` and follow it — this repo auto-releases from main, so the bump may be `pyproject.toml` version + CHANGELOG entry)

- [ ] **Step 1: CHANGELOG entry** under a new version heading, matching the file's existing voice:

```markdown
- feat: every `probe exec` / `client.run()` snapshots by default (`PROBE_AUTO_SNAPSHOT=0` opts out)
- feat: launch block (`metadata.launch`, schema `probe.launch/1`) — scrubbed argv, host, launcher chain, env-var names + allowlisted values, container context, seed evidence with provenance
- feat: OS/CPU/CUDA identity joins `execution_records.hardware`; root lockfiles captured as files, hashes join `deps.lockfiles`
- feat: `probe run check` learns launch slots + a non-blocking `advisories` list; `PROBE_REQUIRE_COMPLETE=1` / `probe exec --strict` gates finish on completeness
- perf: `pushed_base` batched from ~3 processes per remote branch to 2 total (2.6–6.6s → ~64ms on this repo)
```

- [ ] **Step 2: Full gate** — `uv run pytest tests/ -q` (harbor/Docker tests auto-skip), then `make lint` if the Makefile has it (else `uv run ruff check src tests`). Expected: all green.
- [ ] **Step 3: Live smoke (required before claiming done — standing rule):** from the worktree, against a real backend if `PROBE_TOKEN` is configured (`probe whoami` to verify): `probe run start` a scratch run in a scratch project, `probe exec RUN -- python -c "print(1)"`, then `probe run check RUN` and confirm the launch block appears and state is `unverified` with expected advisories. If no live token is available, run the CLI against a local uvicorn research-os (see that repo's README) and note in the PR which smoke ran.
- [ ] **Step 4: Commit + push + PR** — `git push -u origin feat/repro-capture-core`, open PR titled "feat: reproducibility capture core (launch block, auto-snapshot, check gates)" referencing `docs/2026-08-04-reproducibility-capture-enforcement-design.md`, noting Plans 2–3 follow.

---

## Self-review notes (spec → plan)

- D1 identity split → Tasks 5, 7 (hardware hashed; launch block unhashed metadata).
- D2 all four capture groups → Tasks 2–6.
- D3 auto-invocation + perf prereq → Tasks 1, 8.
- D4 completeness + strict → Tasks 9, 10 (advisories split documented as the one interpretation call: judgment slots and legacy runs report, capture-failure slots flip the verdict).
- D7 hygiene → Tasks 2 (scrub), 3 (allowlist), 6 (secret filters unchanged and still win over lockfile globs — `.env` is not in `LOCKFILE_NAMES`, no conflict).
- D8 testing → per-task TDD + Task 11 full gate + live smoke.
- D5 (pull surface) and D6 (skills) are Plans 2–3 by design; nothing here blocks them.

> **Amendment (2026-08-06):** Task 10 as executed was REVERSED by maintainer
> decision — the strict gate (`PROBE_REQUIRE_COMPLETE`, `--strict`,
> `CaptureIncomplete`) was removed and replaced with a non-blocking completion
> warning in `Run.finish()`. See design doc D4 (revised) for the surviving
> behavior. Runs are never blocked on capture.
