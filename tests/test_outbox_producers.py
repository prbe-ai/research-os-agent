"""Parity F4 (docs/2026-08-04-outbox-miles-parity.md): the producer registry.

Every registered writer stamps ops with producer_id + a per-producer
sequence; the registry keeps the high-water mark and explicit capture gaps.
Sequence allocation reads the registry UNDER THE APPEND LOCK, so an id
deliberately shared across processes (the CLI's per-host one) can never mint
the same sequence twice.
"""

from __future__ import annotations

import pytest

from probe.sdk.journal import Journal

from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)


def journal_at(tmp_path, **kw) -> Journal:
    return Journal(tmp_path / "outbox", **kw)


def _seqs(journal) -> list[tuple[str | None, int | None]]:
    return [
        (op.get("producer_id"), op.get("producer_sequence"))
        for _, op in journal.pending()
    ]


def test_registered_producer_stamps_ops_and_tracks_the_high_water(tmp_path):
    journal = journal_at(tmp_path)
    journal.register_producer("train:host:1:aa", role="training")
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"n": 1})
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"n": 2})
    assert _seqs(journal) == [("train:host:1:aa", 1), ("train:host:1:aa", 2)]
    (record,) = journal.producer_report()
    assert record["last_sequence"] == 2
    assert record["role"] == "training"
    assert record["state"] == "open"


def test_reregistering_resumes_the_sequence_line(tmp_path):
    first = journal_at(tmp_path)
    first.register_producer("train:host:1:aa")
    first.append_http("POST", "/v1/runs/r-1/metrics", {})
    # A restart (new process, same identity) continues, never rewinds.
    second = journal_at(tmp_path)
    second.register_producer("train:host:1:aa")
    second.append_http("POST", "/v1/runs/r-1/metrics", {})
    assert [seq for _, seq in _seqs(second)] == [1, 2]


def test_shared_id_across_instances_never_duplicates_sequences(tmp_path):
    a = journal_at(tmp_path)
    b = journal_at(tmp_path)
    a.register_producer("cli:host")
    b.register_producer("cli:host")
    a.append_http("POST", "/v1/runs/r-1/metrics", {})
    b.append_http("POST", "/v1/runs/r-1/metrics", {})
    a.append_http("POST", "/v1/runs/r-1/metrics", {})
    assert [seq for _, seq in _seqs(a)] == [1, 2, 3]
    (record,) = a.producer_report()
    assert record["last_sequence"] == 3


def test_two_producers_keep_independent_lines(tmp_path):
    a = journal_at(tmp_path)
    b = journal_at(tmp_path)
    a.register_producer("rank0:host:1:aa", role="training")
    b.register_producer("rollout:host:2:bb", role="rollout")
    a.append_http("POST", "/v1/runs/r-1/metrics", {})
    b.append_http("POST", "/v1/runs/r-1/metrics", {})
    a.append_http("POST", "/v1/runs/r-1/metrics", {})
    assert _seqs(a) == [
        ("rank0:host:1:aa", 1),
        ("rollout:host:2:bb", 1),
        ("rank0:host:1:aa", 2),
    ]
    report = {p["producer_id"]: p for p in a.producer_report()}
    assert report["rank0:host:1:aa"]["last_sequence"] == 2
    assert report["rollout:host:2:bb"]["last_sequence"] == 1


def test_capture_gap_burns_a_sequence_and_records_why(tmp_path):
    journal = journal_at(tmp_path)
    journal.register_producer("train:host:1:aa")
    journal.append_http("POST", "/v1/runs/r-1/metrics", {})
    journal.note_capture_gap("metric payload failed to serialize")
    journal.append_http("POST", "/v1/runs/r-1/metrics", {})
    # The hole is VISIBLE: op seqs 1 and 3, gap record owns 2.
    assert [seq for _, seq in _seqs(journal)] == [1, 3]
    (record,) = journal.producer_report()
    (gap,) = record["gaps"]
    assert gap["sequence"] == 2
    assert "serialize" in gap["reason"]


def test_seal_marks_a_clean_close(tmp_path):
    journal = journal_at(tmp_path)
    journal.register_producer("train:host:1:aa")
    journal.seal_producer()
    (record,) = journal.producer_report()
    assert record["state"] == "closed"
    assert record["closed_at"]


def test_unregistered_journals_stamp_nothing(tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/metrics", {})
    ((_, op),) = ((p, o) for p, o in journal.pending())
    assert "producer_id" not in op and "producer_sequence" not in op
    assert journal.producer_report() == []


def test_async_client_registers_stamps_and_seals(app, tmp_path):
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write("POST", "/v1/runs/r-1/metrics", {"points": []})
    ((_, op),) = ((p, o) for p, o in client.journal.pending())
    assert op["producer_id"].startswith("sdk:")
    assert op["producer_sequence"] == 1
    (record,) = client.journal.producer_report()
    assert record["role"] == "sdk" and record["state"] == "open"
    client.close()
    (record,) = client.journal.producer_report()
    assert record["state"] == "closed", "a clean close must not read as a crash"


def test_sync_clients_do_not_register(app, tmp_path):
    client = make_client(app, tmp_spool=tmp_path / "outbox")
    assert client.journal.producer_report() == []
    client.close()
