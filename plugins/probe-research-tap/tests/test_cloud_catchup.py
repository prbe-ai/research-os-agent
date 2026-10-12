# tests/test_cloud_catchup.py
"""A local agent's session start catches up a connected cloud agent (Capy):
`python -m tap cloud-catchup` starts `probe <agent> sync --quiet` detached
when the last sync is old enough, and does nothing otherwise."""

import os
import stat
import time

import pytest

from tap import cloud_catchup
from tap.harness_registry import FAMILY_CLOUD, get_registry


@pytest.fixture
def cloud():
    rows = [h for h in get_registry().all() if h.family == FAMILY_CLOUD]
    assert rows, "the registry has a cloud row"
    return rows[0]


@pytest.fixture
def machine(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / "state"
    monkeypatch.setenv(cloud.capture.plugin_dir_env, str(state))
    marker = tmp_path / "ran.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "probe"
    probe.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return state, marker


def _wait_for(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    return None


def test_a_connected_cloud_agent_with_an_old_sync_is_synced(machine, cloud):
    state, marker = machine
    state.mkdir()
    (state / f"{cloud.id}.json").write_text("{}")
    old = time.time() - cloud_catchup.MIN_INTERVAL_S - 60
    (state / cloud_catchup.SYNC_STATE).write_text("{}")
    os.utime(state / cloud_catchup.SYNC_STATE, (old, old))
    assert cloud_catchup.main([]) == 0
    assert _wait_for(marker) == f"{cloud.id} sync --quiet"


def test_never_synced_counts_as_due(machine, cloud):
    state, marker = machine
    state.mkdir()
    (state / f"{cloud.id}.json").write_text("{}")
    cloud_catchup.main([])
    assert _wait_for(marker) == f"{cloud.id} sync --quiet"


@pytest.mark.parametrize("case", ["not_connected", "recent", "killswitch"])
def test_nothing_runs_when_not_due(machine, cloud, case):
    state, marker = machine
    state.mkdir()
    if case != "not_connected":
        (state / f"{cloud.id}.json").write_text("{}")
    if case == "recent":
        (state / cloud_catchup.SYNC_STATE).write_text("{}")
    if case == "killswitch":
        (state / cloud_catchup.KILLSWITCH).write_text("off\n")
    cloud_catchup.main([])
    assert _wait_for(marker, timeout=0.5) is None


def test_no_probe_on_path_is_fine(machine, cloud, monkeypatch):
    state, _ = machine
    state.mkdir()
    (state / f"{cloud.id}.json").write_text("{}")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert cloud_catchup.main([]) == 0
