"""Parity F5 (docs/2026-08-04-outbox-miles-parity.md): redaction at capture,
plus run-scoped dead-letter retry (F6's journal half).

The scrubber is the Miles one, ported to probe.sdk.redaction as the single
source; the adapter folds onto it at the P6 rebase.
"""

from __future__ import annotations


import pytest

from probe.sdk import errors
from probe.sdk.journal import Journal
from probe.sdk.redaction import default_scrub, is_sensitive_key, scrub_string

from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)


# -- the scrubber ------------------------------------------------------------


def test_sensitive_keys_are_redacted_deep():
    scrubbed = default_scrub(
        {
            "wandb_key": "wb-abc123",
            "nested": {"refresh_token": "rt-1", "lr": 0.001},
            "list": [{"password": "hunter2"}, "plain"],
        }
    )
    assert scrubbed["wandb_key"] == "<redacted>"
    assert scrubbed["nested"]["refresh_token"] == "<redacted>"
    assert scrubbed["nested"]["lr"] == 0.001
    assert scrubbed["list"][0]["password"] == "<redacted>"
    assert scrubbed["list"][1] == "plain"


def test_tokenizer_vocabulary_survives():
    # eos_token is DATA (a tokenizer entry), not a credential.
    scrubbed = default_scrub({"eos_token": "</s>", "auth_token": "at-1"})
    assert scrubbed["eos_token"] == "</s>"
    assert scrubbed["auth_token"] == "<redacted>"
    assert not is_sensitive_key("pad_token") and is_sensitive_key("api_key")


def test_credentialed_uris_and_query_secrets_are_scrubbed():
    assert (
        scrub_string("postgres://user:hunter2@db.internal/probe")
        == "postgres://<redacted>@db.internal/probe"
    )
    scrubbed = scrub_string("https://api.example.com/v1?api_key=k-123&page=2")
    assert "k-123" not in scrubbed and "page=2" in scrubbed


def test_unserializable_values_degrade_to_repr():
    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    assert default_scrub({"config": Opaque()})["config"] == "<Opaque>"


# -- the client hook ---------------------------------------------------------


def _everything(journal_dir) -> str:
    return "".join(
        p.read_text() for p in journal_dir.rglob("*.json") if p.is_file()
    )


def test_redact_true_scrubs_async_ops_at_rest(app, tmp_path):
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, redact=True
    )
    client.write(
        "POST", "/v1/runs/r-1/metrics", {"config": {"wandb_key": "wb-secret-1"}}
    )
    on_disk = _everything(tmp_path / "outbox")
    assert "wb-secret-1" not in on_disk and "<redacted>" in on_disk
    client.close()


def test_redact_callable_is_honored(app, tmp_path):
    client = make_client(
        app,
        tmp_spool=tmp_path / "outbox",
        async_writes=True,
        redact=lambda body: {"scrubbed": True},
    )
    client.write("POST", "/v1/runs/r-1/metrics", {"anything": "at all"})
    ((_, op),) = ((p, o) for p, o in client.journal.pending())
    assert op["body"] == {"scrubbed": True}
    client.close()


def test_sync_fail_open_spool_is_scrubbed_too(app, tmp_path, monkeypatch):
    client = make_client(app, tmp_spool=tmp_path / "outbox", redact=True)

    def down(*a, **kw):
        raise errors.TransportError("net down")

    monkeypatch.setattr(client.transport, "request", down)
    client.write("POST", "/v1/runs/r-1/metrics", {"password": "hunter2"})
    on_disk = _everything(tmp_path / "outbox")
    assert "hunter2" not in on_disk
    client.close()


def test_no_redact_leaves_bodies_untouched(app, tmp_path):
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write("POST", "/v1/runs/r-1/metrics", {"eos_token": "</s>", "note": "x"})
    ((_, op),) = ((p, o) for p, o in client.journal.pending())
    assert op["body"] == {"eos_token": "</s>", "note": "x"}
    client.close()


# -- run-scoped retry --------------------------------------------------------


def test_retry_failed_scoped_to_one_run(tmp_path):
    journal = Journal(tmp_path / "outbox")
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"n": 1})
    journal.append_http("POST", "/v1/runs/r-2/metrics", {"n": 2})
    # Dead-letter both by hand (the drain paths are covered elsewhere).
    for path, _ in journal.pending():
        path.rename(journal.failed_dir / path.name)
    assert len(journal.failed()) == 2

    moved = journal.retry_failed(run_ref="r-1")

    assert moved == 1
    ((_, requeued),) = ((p, o) for p, o in journal.pending())
    assert requeued["run_ref"] == "r-1"
    assert [op["run_ref"] for _, op in journal.failed()] == ["r-2"]
