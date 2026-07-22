"""Crash-safe behavior of the fail-open JSONL queue."""

from __future__ import annotations

import os
import threading

from probe.sdk.client import Client
from probe.sdk.spool import Spool, default_dir


def test_append_flushes_and_fsyncs_before_return(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool")
    real_fsync = os.fsync
    calls: list[int] = []

    def recording_fsync(fd: int):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    spool.append("POST", "/v1/runs/r/metrics", {"points": []})

    assert calls  # file, plus directory on first creation
    assert spool.pending()[0].path == "/v1/runs/r/metrics"


def test_failed_flush_atomically_replaces_queue_with_unsent_remainder(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool")
    for index in range(3):
        spool.append("POST", f"/item/{index}", {"index": index})

    class Transport:
        calls = 0

        def request(self, method, path, json_body=None):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("offline")

    real_replace = os.replace
    replacements: list[tuple[str, str]] = []

    def recording_replace(source, destination):
        replacements.append((str(source), str(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    assert spool.flush(Transport()) == 1

    assert replacements and replacements[-1][1] == str(spool.file)
    assert [record.path for record in spool.pending()] == ["/item/1", "/item/2"]
    assert not list(spool.dir.glob(".pending.*.tmp"))


def test_spool_directory_can_live_on_shared_storage_via_env(tmp_path, monkeypatch):
    durable = tmp_path / "shared-pvc" / "probe-spool"
    monkeypatch.setenv("PROBE_SPOOL_DIR", str(durable))
    assert default_dir() == durable
    assert Spool().dir == durable


def test_append_does_not_wait_for_slow_network_replay(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.append("POST", "/old", None)
    started = threading.Event()
    release = threading.Event()

    class Transport:
        def request(self, method, path, json_body=None):
            started.set()
            assert release.wait(timeout=2)

    worker = threading.Thread(target=spool.flush, args=(Transport(),))
    worker.start()
    assert started.wait(timeout=2)
    # flush holds only its own flusher lock during the network call; the pending
    # append lock is free for a training-loop writer.
    spool.append("POST", "/new", None)
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert [record.path for record in spool.pending()] == ["/new"]


def test_flush_recovers_batch_moved_inflight_before_process_crash(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.append("POST", "/old", None)
    os.replace(spool.file, spool.inflight_file)  # prior process died here
    spool.append("POST", "/new", None)
    sent: list[str] = []

    class Transport:
        def request(self, method, path, json_body=None):
            sent.append(path)

    assert spool.flush(Transport()) == 2
    assert sent == ["/old", "/new"]
    assert spool.pending() == []


def test_client_rejects_two_spool_owners(client, tmp_path):
    try:
        Client(
            settings=client.settings,
            transport=client.transport,
            spool=Spool(tmp_path / "one"),
            spool_dir=tmp_path / "two",
        )
    except ValueError as exc:
        assert "spool or spool_dir" in str(exc)
    else:
        raise AssertionError("expected ambiguous spool configuration to fail")
