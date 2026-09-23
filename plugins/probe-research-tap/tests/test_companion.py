"""The Probe daemon worker: observe, decide, persist-then-publish, the lease."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tap import companion_api as api_mod
from tap import companion_lease as lease
from tap import companion_ledger as ledger_mod
from tap import companion_observe as observe
from tap import companion_supervisor as supervisor_mod
from tap import companion_worker as worker_mod

SID = "11111111-2222-3333-4444-555555555555"
RUN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PARENT = "99999999-8888-7777-6666-555555555555"
PROJECT = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"base_url": "https://api.example.test", "companion_token": "tok-1"}))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(config))
    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    monkeypatch.delenv(worker_mod.ENV_SHADOW, raising=False)
    monkeypatch.setattr(worker_mod, "_stop", False)
    yield


def _set_state(state: str) -> None:
    path = lease.sessions_dir() / f"{SID}.state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state)


def _claude_lines(*events: dict) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_use(call_id, command):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]},
    }


def _tool_result(call_id, text):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]},
    }


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# ---------------------------------------------------------------------------
# Observe
# ---------------------------------------------------------------------------


def test_claude_observation_finds_ids_run_starts_directed_and_redacts(tmp_path):
    raw = _claude_lines(
        _user("train the baseline"),
        _tool_use("c1", "probe run start --name baseline"),
        _tool_result("c1", f'{{"run_id": "{RUN}", "status": "running"}}'),
        _tool_use("c2", "probe notes push notes.md --directed"),
        _tool_result("c2", "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEYzz"),
    )
    path = tmp_path / "t.jsonl"
    path.write_bytes(raw)
    lines, end = observe.read_chunk(path, 0, max_bytes=1 << 20)
    assert end == len(raw)
    seen = observe.observe("claude_code", lines)
    assert seen.ids[RUN][0] == "run"
    assert seen.run_starts == {RUN}
    assert seen.directed and "--directed" in seen.directed[0][1]
    assert seen.redactions >= 1
    rendered = observe.render(seen.events)
    assert "wJalrXUtnFEMIK7MDENG" not in rendered
    assert "[user @" in rendered and "probe run start" in rendered


def test_observation_records_the_folders_commands_ran_in(tmp_path):
    raw = _claude_lines(
        _tool_use("c1", "mkdir -p ~/trials/x && cd ~/trials/x && python run.py"),
        _tool_result("c1", "wrote results/table.md"),
        _tool_use("c2", "cd \"/data/my runs\"; ls"),
        _tool_result("c2", "a b"),
        _tool_use("c3", "echo cd nowhere"),
        _tool_result("c3", "cd nowhere"),
    )
    path = tmp_path / "t.jsonl"
    path.write_bytes(raw)
    lines, _ = observe.read_chunk(path, 0, max_bytes=1 << 20)
    assert observe.observe("claude_code", lines).workdirs == ["~/trials/x", "/data/my runs"]


def test_codex_and_pi_parsers_read_tool_calls_and_results():
    codex = [
        (10, json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "k", "arguments": json.dumps({"command": ["probe", "run", "start"]})}}).encode()),
        (20, json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "k", "output": json.dumps({"output": f"run {RUN}"})}}).encode()),
    ]
    seen = observe.observe("codex", codex)
    assert seen.run_starts == {RUN}
    pi = [
        (10, json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "p", "name": "bash", "arguments": {"command": "probe run start"}}]}}).encode()),
        (20, json.dumps({"type": "message", "message": {"role": "toolResult", "toolCallId": "p", "content": [{"type": "text", "text": f"run_id: {RUN}"}]}}).encode()),
    ]
    assert observe.observe("pi", pi).run_starts == {RUN}


def test_read_chunk_withholds_a_partial_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2')
    lines, end = observe.read_chunk(path, 0, max_bytes=1 << 20)
    assert len(lines) == 1 and end == len(b'{"a": 1}\n')


# ---------------------------------------------------------------------------
# Lease and ledger
# ---------------------------------------------------------------------------


def test_lease_renew_and_release_write_the_versioned_format():
    assert lease.renew(SID, now=100.0, ttl=60)
    data = json.loads(lease.lease_path(SID).read_text())
    assert data == {"v": 1, "writer": "daemon", "pid": data["pid"], "expires_at": 160.0, "renewed_at": 100.0, "reason": None}
    lease.release(SID, lease.REASON_BUDGET, now=200.0)
    data = json.loads(lease.lease_path(SID).read_text())
    assert data["reason"] == "budget" and data["expires_at"] == 200.0


def test_ledger_refuses_a_newer_format(tmp_path):
    ledger = ledger_mod.Ledger(SID)
    ledger._set_meta("format_version", str(ledger_mod.LEDGER_VERSION + 1))
    ledger.close()
    with pytest.raises(ledger_mod.LedgerVersionError):
        ledger_mod.Ledger(SID)


def test_a_decided_key_is_never_decided_twice():
    ledger = ledger_mod.Ledger(SID)
    kwargs = dict(idem_key="k", cycle=1, kind="note", target_type="run", target_id=RUN, payload={}, evidence={}, status="pending")
    assert ledger.add_proposal(**kwargs)
    assert not ledger.add_proposal(**kwargs)


def test_device_spend_ceiling(monkeypatch):
    monkeypatch.setenv(ledger_mod.ENV_DAILY_TOKEN_CEILING, "100")
    spend = ledger_mod.DeviceSpend()
    spend.add(60)
    assert not spend.exhausted()
    spend.add(60)
    assert spend.exhausted()


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self, *, proposals=None, entities=None, lists=None):
        self.token = "tok-1"
        self.proposals = proposals or []
        self.entities = entities or {}
        self.lists = lists or {}
        self.writes: list[tuple] = []
        self.completions = 0

    def get(self, path, **params):
        if path in self.lists:
            return self.lists[path]
        if path in self.entities:
            return self.entities[path]
        raise api_mod.Rejected(404, None)

    def request(self, method, path, body=None, *, idem_key=None, params=None, timeout=30):
        self.writes.append((method, path, body, idem_key))
        if path.endswith("/artifacts/uploads"):
            return {"artifact_id": "art-1", "upload_url": "https://r2.example/put", "have": False}
        return {"id": "x"}

    def complete(self, messages, *, max_tokens=None, timeout=None):
        self.completions += 1
        self.messages = messages
        return {"content": json.dumps({"proposals": self.proposals}), "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}}

    def put_file(self, url, path, *, content_type, headers=None):
        self.writes.append(("PUT", url, str(path), None))


def _proposal(kind, op, attempts=0):
    return ledger_mod.Proposal(
        id=1, idem_key="pc1-x", kind=kind, target_type="run", target_id=RUN, payload={},
        evidence={"_op": list(op)}, status="pending", reason=None, attempts=attempts,
    )


def _decider(api, tmp_path, **overrides):
    kwargs = dict(
        known_ids={RUN: "run", PARENT: "run", PROJECT: "project"},
        run_starts={RUN},
        run_ends=set(),
        cwd=tmp_path,
        touched=set(),
        chunk_text="",
        range_start=0,
        range_end=1000,
        agent_ranges=(),
    )
    kwargs.update(overrides)
    return worker_mod.Decider(api, **kwargs)


def _raw(kind, target_type="run", target_id=RUN, **fields):
    return {"kind": kind, "target": {"type": target_type, "id": target_id}, "evidence": {"from": 10, "to": 20}, **fields}


def test_an_id_never_seen_in_the_session_is_held(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}})
    decider = _decider(api, tmp_path, known_ids={})
    with pytest.raises(worker_mod.Held, match="acted on"):
        decider.decide(_raw("note", title="t", body="b"))


def test_evidence_before_the_handover_is_held(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}})
    with pytest.raises(worker_mod.Held, match="agent owned"):
        _decider(api, tmp_path, agent_ranges=[(0, 50)]).decide(_raw("note", title="t", body="b"))


def test_note_is_a_prefixed_sub_note_and_is_held_when_the_title_exists(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": [], "limit_count": 20}})
    out = _decider(api, tmp_path).decide(_raw("note", title="lr sweep result", body="0.3 wins"))
    assert out["op"] == ("POST", f"/v1/runs/{RUN}/sub-notes", {"title": "companion: lr sweep result", "body": "0.3 wins"})
    api.lists[f"/v1/runs/{RUN}/sub-notes"] = {"sub_notes": [{"title": "companion: lr sweep result"}]}
    with pytest.raises(worker_mod.Held, match="already exists"):
        _decider(api, tmp_path).decide(_raw("note", title="lr sweep result", body="0.3 wins"))


def test_describe_only_fills_empty_fields(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN, "slug": "lr-sweep", "name": "Lr Sweep", "description": "set by a person"}})
    out = _decider(api, tmp_path).decide(_raw("describe", name="Learning-rate sweep", description="overwrite?"))
    assert out["op"][2] == {"name": "Learning-rate sweep", "authored_by": "agent"}


def test_tags_are_added_never_removed_built_from_a_fresh_read(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN, "tags": ["baseline"]}})
    decider = _decider(api, tmp_path)
    out = decider.decide(_raw("tag", tags=["baseline", "lr-sweep"]))
    assert out["op"] == ("TAG", f"/v1/runs/{RUN}", {"add": ["lr-sweep"]})
    # A second proposal in the same cycle builds on the first.
    assert decider.decide(_raw("tag", tags=["ablation"]))["op"][2] == {"add": ["ablation"]}
    # Someone tags the run before the daemon publishes: their tag survives.
    api.entities[f"/v1/runs/{RUN}"] = {"id": RUN, "tags": ["baseline", "by-a-person"]}
    worker_mod.publish(api, _proposal("tag", out["op"]))
    assert api.writes[-1][:3] == ("PATCH", f"/v1/runs/{RUN}", {"tags": ["baseline", "by-a-person", "lr-sweep"]})


def test_describe_is_rechecked_at_publish(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN, "slug": "x", "name": "Real", "description": ""}})
    out = _decider(api, tmp_path).decide(_raw("describe", description="what it is"))
    api.entities[f"/v1/runs/{RUN}"]["description"] = "a person wrote this meanwhile"
    with pytest.raises(worker_mod.AlreadyDone):
        worker_mod.publish(api, _proposal("describe", out["op"]))


def test_a_retried_note_is_checked_for_before_posting_again(tmp_path):
    op = ["POST", f"/v1/runs/{RUN}/sub-notes", {"title": "companion: t", "body": "b"}]
    api = FakeApi(lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": [{"title": "companion: t"}]}})
    with pytest.raises(worker_mod.AlreadyDone):
        worker_mod.publish(api, _proposal("note", op, attempts=1))
    assert not api.writes


def test_edge_points_from_the_new_run_to_its_parent(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}, f"/v1/runs/{PARENT}": {"id": PARENT}}, lists={f"/v1/runs/{RUN}/edges": []})
    out = _decider(api, tmp_path).decide(_raw("edge", source_run_id=PARENT, relation="forked_from", reason="resumed from step 400"))
    body = out["op"][2]
    assert (body["source_id"], body["target_id"], body["relation"]) == (RUN, PARENT, "forked_from")


def test_run_end_only_for_a_run_this_session_opened(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN, "status": "running"}})
    out = _decider(api, tmp_path).decide(_raw("run_end", status="completed"))
    assert out["op"] == ("PATCH", f"/v1/runs/{RUN}", {"status": "completed"})
    with pytest.raises(worker_mod.Held):
        _decider(api, tmp_path, run_starts=set()).decide(_raw("run_end", status="completed"))


def test_artifact_gates(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/artifacts": []})
    (work / "results.csv").write_text("step,loss\n1,0.5\n")
    (work / "plot.png").write_bytes(b"\x89PNG\x00\x00binary")
    (work / "data.db").write_bytes(b"SQLite format 3\x00")
    (work / "embeddings.npy").write_bytes(b"\x93NUMPY\x01\x00v\x00" + bytes(range(256)))
    (work / "run.creds").write_text("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEYzz\n")
    (work / "id_rsa").write_text("not a real key\n")
    (work / "empty.csv").write_text("")
    key_line = "aws_secret_access_key = " + "wJalrXUtnFEMIK7MDENG" + "bPxRfiCYEXAMPLEKEYzz\n"
    (work / "notes-latin1.txt").write_bytes(("caf\xe9 " + key_line).encode("latin-1"))
    (work / "nul-led.log").write_bytes(b"\x00" + key_line.encode())
    (work / "cache.sqlite").write_bytes(b"SQLite format 3\x00" + bytes(64) + key_line.encode() + bytes(64))
    (work / "bundle.zip").write_bytes(b"PK\x03\x04" + bytes(200))
    (work / "vault.kdbx").write_bytes(bytes(range(256)))
    (work / ".env").write_text("X=1\n")
    (work / ".cache").mkdir()
    (work / ".cache" / "out.csv").write_text("a\n")
    (work / "leak.txt").write_text("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEYzz\n")
    (work / "tokens.txt").write_text("x\n")
    (work / "read-only.csv").write_text("a\n")
    produced = ("python train.py\nsaved results.csv, plot.png, data.db, .env, .cache/out.csv, leak.txt, tokens.txt, "
                "embeddings.npy, run.creds, id_rsa, empty.csv, notes-latin1.txt, nul-led.log, cache.sqlite, "
                "bundle.zip, vault.kdbx")
    decider = _decider(api, tmp_path, cwd=work, produced_text=produced)
    out = decider.decide(_raw("artifact", path="results.csv"))
    assert out["op"][0] == "UPLOAD" and out["payload"]["name"] == "results.csv"
    assert decider.decide(_raw("artifact", path="plot.png"))["op"][0] == "UPLOAD"
    # Any kind of file: the extension decides nothing (a .npy or a .db the run made uploads).
    assert decider.decide(_raw("artifact", path="embeddings.npy"))["op"][0] == "UPLOAD"
    assert decider.decide(_raw("artifact", path="data.db"))["op"][0] == "UPLOAD"
    for bad, reason in (
        ("run.creds", "secret scanner"),  # text by content, whatever the suffix: scanned
        ("id_rsa", "not uploadable"),
        ("empty.csv", "empty"),
        # Every file is scanned, whatever its encoding or kind...
        ("notes-latin1.txt", "secret scanner"),
        ("nul-led.log", "secret scanner"),
        ("cache.sqlite", "secret scanner"),
        # ...and a container the scan cannot read into is the researcher's to upload.
        ("bundle.zip", "compressed"),
        ("vault.kdbx", "not uploadable"),
        (".env", "hidden"),
        (".cache/out.csv", "hidden"),
        ("leak.txt", "secret scanner"),
        ("tokens.txt", "not uploadable"),
        ("/etc/hosts", "did not produce"),
        ("read-only.csv", "did not produce"),  # only ever `cat`ed, never produced
    ):
        with pytest.raises(worker_mod.Held, match=reason):
            decider.decide(_raw("artifact", path=bad))
    # Where the session started does not matter: from the home folder it still uploads.
    monkeypatch.setattr(worker_mod.Path, "home", classmethod(lambda cls: work))
    assert decider.decide(_raw("artifact", path="results.csv"))["op"][0] == "UPLOAD"


def test_a_session_started_in_home_uploads_from_the_folder_it_worked_in(tmp_path, monkeypatch):
    """`claude` started in ~, the work done in ~/trials/x: a relative path the
    command printed resolves against the folder that command `cd`'d into."""
    home = tmp_path
    work = home / "trials" / "x"
    (work / "results").mkdir(parents=True)
    (work / "results" / "table.md").write_text("| c | acc |\n")
    monkeypatch.setattr(worker_mod.Path, "home", classmethod(lambda cls: home))
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/artifacts": []})
    decider = _decider(
        api, tmp_path, cwd=home, workdirs=["~/elsewhere", str(work)], produced_text="wrote results/table.md"
    )
    out = decider.decide(_raw("artifact", path="results/table.md"))
    assert out["op"][0] == "UPLOAD" and out["payload"]["path"] == str(work / "results" / "table.md")


def test_bytes_the_session_already_recorded_are_never_uploaded_again(tmp_path):
    """The agent's own SDK upload lands on the run it logged; the daemon, aiming
    at another entity under another name, must still see it."""
    work = tmp_path / "work"
    work.mkdir()
    plot = work / "plot.png"
    plot.write_bytes(b"\x89PNG same bytes")
    digest = worker_mod._sha256_file(plot)
    lists = {
        f"/v1/runs/{RUN}/artifacts": [],
        f"/v1/runs/{PARENT}/artifacts": [{"name": "results/plot.png", "content_hash": digest}],
    }
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists=lists)
    decider = _decider(api, tmp_path, cwd=work, produced_text="saved plot.png")
    with pytest.raises(worker_mod.Held, match=f"already recorded: these bytes are on run {PARENT}"):
        decider.decide(_raw("artifact", path="plot.png"))
    # A run whose artifacts cannot be read is skipped, not a reason to hold.
    lists.pop(f"/v1/runs/{PARENT}/artifacts")
    fresh = _decider(api, tmp_path, cwd=work, produced_text="saved plot.png")
    assert fresh.decide(_raw("artifact", path="plot.png"))["op"][0] == "UPLOAD"


def test_an_upload_sends_the_bytes_that_were_scanned(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "r.csv").write_text("a\n")
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/artifacts": []})
    out = _decider(api, tmp_path, cwd=work, touched={"r.csv"}).decide(_raw("artifact", path="r.csv"))
    worker_mod.publish(api, _proposal("artifact", out["op"]))
    methods = [w[0] for w in api.writes]
    assert methods == ["POST", "PUT", "POST"] and api.writes[-1][1] == "/v1/artifacts/art-1/confirm"
    (work / "r.csv").write_text("changed after deciding\n")
    with pytest.raises(api_mod.Rejected, match="changed"):
        worker_mod.publish(api, _proposal("artifact", out["op"]))


def test_a_refused_upload_url_fails_the_upload_not_the_key(tmp_path, monkeypatch):
    import urllib.error

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "expired", {}, None)

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", refuse)
    f = tmp_path / "x.csv"
    f.write_text("a\n")
    with pytest.raises(api_mod.Rejected):
        api_mod.Api("https://api.example.test", "t").put_file("https://r2/put", f, content_type="text/csv")


# ---------------------------------------------------------------------------
# The worker: persist then publish, the lease, shadow, handback.
# ---------------------------------------------------------------------------


def _worker(tmp_path, api, transcript_bytes):
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(transcript_bytes)
    worker = worker_mod.Worker(session_id=SID, transcript=transcript, cwd=tmp_path, source="claude_code")
    worker.api = api
    return worker, transcript


def test_cycle_persists_then_publishes_and_advances_the_watermark(tmp_path):
    _set_state("daemon")
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    worker, transcript = _worker(tmp_path, api, b"")
    worker.take_lease()  # boundary at 0: the daemon owns everything after
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'), _assistant("loss plateaued at 0.41"))
    transcript.write_bytes(body)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "plateau", "body": "0.41"},
                     {"kind": "delete", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": 2}}]
    assert worker.cycle(shadow=False) == "2 decided"
    assert worker.ledger.watermark == len(body)
    pending = worker.ledger.pending()
    assert [p.kind for p in pending] == ["note"]
    held = worker.ledger.conn.execute("SELECT reason FROM proposals WHERE status = 'held'").fetchall()
    assert held == [("kind not allowed",)]
    worker.publish_pending(time.monotonic() + 10)
    assert api.writes[0][:2] == ("POST", f"/v1/runs/{RUN}/sub-notes")
    assert api.writes[0][3].startswith("pc1-")
    assert worker.ledger.published_count() == 1
    # A replayed cycle over the same range decides nothing new.
    worker.ledger.set_watermark(0)
    worker.cycle(shadow=False)
    assert worker.ledger.published_count() == 1 and not worker.ledger.pending()


def test_shadow_records_decisions_but_publishes_nothing(tmp_path):
    _set_state("full")
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _worker(tmp_path, api, body)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "t", "body": "b"}]
    worker.cycle(shadow=True)
    rows = worker.ledger.conn.execute("SELECT status, reason FROM proposals").fetchall()
    assert rows == [("held", "shadow")]
    assert not api.writes


def test_leaving_the_daemon_state_releases_the_lease_and_exits(tmp_path):
    _set_state("daemon")
    worker, _ = _worker(tmp_path, FakeApi(), b"")
    worker.take_lease()
    _set_state("full")
    assert worker.run() == 0
    data = json.loads(lease.lease_path(SID).read_text())
    assert data["reason"] == lease.REASON_STOPPED
    rows = ledger_mod.Ledger(SID).conn.execute("SELECT from_writer, to_writer, reason FROM boundaries ORDER BY id").fetchall()
    assert rows == [("agent", "daemon", "daemon"), ("daemon", "agent", "disable")]


def test_no_key_hands_writing_back_as_unauthorized(tmp_path, monkeypatch):
    _set_state("daemon")
    config = Path(json.loads(json.dumps(str(tmp_path / "config.json"))))
    config.write_text(json.dumps({"base_url": "https://api.example.test"}))
    worker, _ = _worker(tmp_path, FakeApi(), b"")
    monkeypatch.setattr(worker_mod, "_sleep", lambda s: setattr(worker_mod, "_stop", True))
    worker.run()
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_UNAUTHORIZED


def test_a_revoked_key_releases_the_lease_and_is_not_retried(tmp_path, monkeypatch):
    _set_state("daemon")

    class Revoked(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            raise api_mod.Unauthorized(401, None)

    api = Revoked(entities={f"/v1/runs/{RUN}": {"id": RUN}})
    worker, transcript = _worker(tmp_path, api, b"")
    worker.take_lease()
    transcript.write_bytes(_claude_lines(_assistant("something happened")))
    worker.ledger.set_json("undecided_since", time.time() - 1000)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1
        if calls["n"] > 2:
            worker_mod._stop = True

    monkeypatch.setattr(worker_mod, "_sleep", sleep)
    worker.run()
    assert worker.blocked_token == "tok-1"
    # The reason survives the exit: the next prompt says WHY recording came back.
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_UNAUTHORIZED
    reasons = [r for (r,) in ledger_mod.Ledger(SID).conn.execute("SELECT reason FROM boundaries ORDER BY id")]
    assert reasons[:2] == ["daemon", "fallback"]


def test_supervisor_spawns_only_in_the_daemon_state(tmp_path, monkeypatch):
    spawned = []

    class FakePopen:
        def __init__(self, argv, **kw):
            spawned.append(argv)
            self.pid = 4242
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(supervisor_mod.subprocess, "Popen", FakePopen)
    sup = supervisor_mod.Supervisor(session_id=SID, transcript=tmp_path / "t.jsonl", cwd=tmp_path)
    _set_state("full")
    sup.poll()
    assert not spawned
    _set_state("daemon")
    sup.poll()
    sup.poll()
    assert len(spawned) == 1 and spawned[0][2:4] == ["tap", "companion"]


def test_prompt_carries_the_closed_id_set_and_the_rules():
    messages = worker_mod.build_messages(
        chunk_text="[user @1]\nhi", prior_context="", known_ids={RUN: "run"}, decided=[], feedback=[], run_starts=[RUN], cwd="/w"
    )
    assert "The set is closed" in messages[0]["content"]
    assert RUN in messages[1]["content"]


def test_a_refused_prompt_is_skipped_not_retried_forever(tmp_path):
    _set_state("daemon")

    class Refuses(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            raise api_mod.Rejected(422, {"detail": "too large"})

    worker, transcript = _worker(tmp_path, Refuses(), b"")
    worker.take_lease()
    body = _claude_lines(_assistant("a lot happened"))
    transcript.write_bytes(body)
    assert worker.cycle(shadow=False) == "skipped"
    assert worker.ledger.watermark == len(body)
    outcome = worker.ledger.conn.execute("SELECT outcome FROM cycles").fetchone()[0]
    assert outcome.startswith("skipped")


# ---------------------------------------------------------------------------
# Review fixes: grounding, failure modes, the lease, the switch, the supervisor.
# ---------------------------------------------------------------------------

OTHER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def _mcp(call_id, tool, args):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": call_id, "name": tool, "input": args}]}}


def test_only_ids_the_session_acted_on_are_write_targets():
    lines = [
        (10, json.dumps(_tool_use("a", "probe run list")).encode()),
        (20, json.dumps(_tool_result("a", f"{OTHER}  someone else's run")).encode()),
        (30, json.dumps(_tool_use("b", "cat README.md")).encode()),
        (40, json.dumps(_tool_result("b", f"project {PARENT}")).encode()),
        (50, json.dumps(_mcp("c", "mcp__plugin_probe-research_probe-research__entity", {"ref": PROJECT})).encode()),
        (60, json.dumps(_tool_use("d", f"probe run get {RUN}")).encode()),
    ]
    ids = observe.observe("claude_code", lines).ids
    assert OTHER not in ids and PARENT not in ids  # listed / read, not acted on
    assert PROJECT in ids and RUN in ids  # named by the agent itself


def test_grepping_for_run_start_is_not_a_run_start():
    lines = [
        (10, json.dumps(_tool_use("a", "grep 'probe run start' logs/")).encode()),
        (20, json.dumps(_tool_result("a", f"run {RUN}")).encode()),
        (30, json.dumps(_tool_use("b", "probe --base-url https://x run start")).encode()),
        (40, json.dumps(_tool_result("b", f"run {OTHER}")).encode()),
    ]
    assert observe.observe("claude_code", lines).run_starts == {OTHER}


def _live_worker(tmp_path, api, body=b""):
    _set_state("daemon")
    worker, transcript = _worker(tmp_path, api, b"")
    worker.take_lease()
    transcript.write_bytes(body)
    return worker, transcript


def test_an_unusable_answer_is_a_failed_cycle_not_an_empty_one(tmp_path):
    class Empty(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            return {"content": "", "finish_reason": "length", "usage": {}}

    worker, _ = _live_worker(tmp_path, Empty(), _claude_lines(_assistant("work happened")))
    with pytest.raises(api_mod.Retryable, match="length"):
        worker.cycle(shadow=False)
    assert worker.ledger.watermark == 0  # the range is retried, not skipped


def test_proposals_past_the_cap_are_held_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "MAX_PROPOSALS_PER_CYCLE", 1)
    body = _claude_lines(_assistant("two things"))
    api = FakeApi()
    api.proposals = [{"kind": "note", "title": "a"}, {"kind": "note", "title": "b"}]
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    reasons = sorted(r for (r,) in worker.ledger.conn.execute("SELECT reason FROM proposals"))
    assert "over the per-cycle cap" in reasons and len(reasons) == 2


def _run_until(worker, monkeypatch, turns=2):
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1
        if calls["n"] >= turns:
            worker_mod._stop = True

    monkeypatch.setattr(worker_mod, "_sleep", sleep)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)


def test_a_missing_gateway_route_hands_writing_back(tmp_path, monkeypatch):
    class NoRoute(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            raise api_mod.Rejected(404, {"detail": "Not Found"})

    worker, _ = _live_worker(tmp_path, NoRoute(), _claude_lines(_assistant("x")))
    worker.ledger.set_json("undecided_since", time.time() - 1000)
    _run_until(worker, monkeypatch, turns=1)
    worker.run()
    data = json.loads(lease.lease_path(SID).read_text())
    assert data["reason"] == lease.REASON_GATEWAY


def test_refused_prompts_in_a_row_hand_writing_back(tmp_path, monkeypatch):
    class Refuses(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            raise api_mod.Rejected(422, {"detail": {"code": "gateway_rejected"}})

    worker, transcript = _live_worker(tmp_path, Refuses(), b"")
    monkeypatch.setattr(worker_mod, "MAX_SKIPPED_CYCLES", 1)
    transcript.write_bytes(_claude_lines(_assistant("x")))
    worker.ledger.set_json("undecided_since", time.time() - 1000)
    _run_until(worker, monkeypatch, turns=1)
    worker.run()
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_GATEWAY


def _note_on_run(end):
    return {"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": end}, "title": f"t{end}", "body": "b"}


def test_a_lapsed_lease_publishes_nothing_and_the_debt_is_decided_on_retake(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, transcript = _live_worker(tmp_path, api, body)
    api.proposals = [_note_on_run(len(body))]
    worker.cycle(shadow=False)
    decided_through = worker.ledger.watermark
    debt_body = body + _claude_lines(_assistant("work while the daemon stalled; the agent was refused here"))
    transcript.write_bytes(debt_body)
    worker.lease_expires_at = time.time() - 1  # the machine slept
    assert worker.publish_pending(time.monotonic() + 10) == worker_mod.PUBLISH_OK
    assert not api.writes  # not one write after the lapse
    assert not worker.live
    assert worker.ledger.get_json("owed_until") == len(debt_body)
    # The agent records its own span, then the daemon comes back.
    full = debt_body + _claude_lines(_assistant("the agent recorded this itself"))
    transcript.write_bytes(full)
    worker.take_lease()
    assert worker.ledger.watermark == decided_through  # the debt comes first
    assert [len(debt_body), len(full)] in worker._agent_ranges()
    api.proposals = [_note_on_run(len(debt_body))]
    worker.cycle(shadow=False)
    # The debt range was decided AND its proposal is a real write, not held.
    assert worker.ledger.watermark == len(debt_body)
    assert [p.payload["title"] for p in worker.ledger.pending()][-1] == f"companion: t{len(debt_body)}"
    # Then the agent's own span is jumped over, never decided.
    api.proposals = [_note_on_run(len(full))]
    assert worker.cycle(shadow=False) == "empty"
    assert worker.ledger.watermark == len(full)


def test_a_second_handback_before_the_debt_is_paid_keeps_the_agents_span(tmp_path):
    worker, transcript = _live_worker(tmp_path, FakeApi(), b"")
    a = _claude_lines(_assistant("one"))
    transcript.write_bytes(a)
    worker.release(lease.REASON_GATEWAY, "fallback", debt=True)  # owes [0, len(a))
    b = a + _claude_lines(_assistant("the agent's"))
    transcript.write_bytes(b)
    worker.take_lease()  # agent owned [len(a), len(b))
    worker.release(lease.REASON_GATEWAY, "fallback", debt=True)  # still owes [0, len(a)) ...
    c = b + _claude_lines(_assistant("the agent's again"))
    transcript.write_bytes(c)
    worker.take_lease()
    ranges = worker._agent_ranges()
    assert [len(a), len(b)] in ranges
    # ...and the agent's span stays the agent's: nothing in [len(a), len(b)) is owed.
    assert all(not (r[0] < len(b) <= r[1]) or r == [len(a), len(b)] for r in ranges)


def test_moving_the_switch_to_read_holds_undelivered_writes(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _live_worker(tmp_path, api, body)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "t", "body": "b"}]
    worker.cycle(shadow=False)
    _set_state("read-only")
    worker._leave_daemon("read-only")
    assert not api.writes
    reasons = [r for (r,) in worker.ledger.conn.execute("SELECT reason FROM proposals")]
    assert reasons == ["switch moved to read-only"]


def test_moving_the_switch_to_on_delivers_what_was_decided(tmp_path):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _live_worker(tmp_path, api, body)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "t", "body": "b"}]
    worker.cycle(shadow=False)
    _set_state("full")  # the researcher really moved it
    worker._leave_daemon("full")
    assert [w[0] for w in api.writes] == ["POST"]
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_STOPPED


def test_publishing_backs_off_and_gives_up(tmp_path, monkeypatch):
    class Down(FakeApi):
        def request(self, method, path, body=None, *, idem_key=None, params=None, timeout=30):
            raise api_mod.Retryable(503, None)

    api = Down(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _live_worker(tmp_path, api, body)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "t", "body": "b"}]
    worker.cycle(shadow=False)
    assert worker.publish_pending(time.monotonic() + 10) == worker_mod.PUBLISH_FAILING
    assert worker.ledger.pending()[0].attempts == 1
    assert worker.publish_pending(time.monotonic() + 10) == worker_mod.PUBLISH_WAITING
    assert worker.ledger.pending()[0].attempts == 1
    monkeypatch.setattr(worker_mod, "publish_backoff", lambda attempts: 0.0)
    for _ in range(worker_mod.PUBLISH_ATTEMPTS):
        worker.publish_pending(time.monotonic() + 10)
    assert not worker.ledger.pending()
    assert worker.ledger.conn.execute("SELECT status FROM proposals").fetchone()[0] == "failed"


def test_shadow_never_runs_outside_on(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_mod.ENV_SHADOW, "1")
    sup = supervisor_mod.Supervisor(session_id=SID, transcript=tmp_path / "t.jsonl", cwd=tmp_path)
    for state, wanted in (("full", True), ("read-only", False), ("off", False)):
        _set_state(state)
        assert sup.wanted() is wanted, state
    worker, _ = _worker(tmp_path, FakeApi(), b"")
    _set_state("off")
    assert worker.run() == 0 and worker.api.completions == 0


def test_the_supervisor_honours_do_not_respawn(tmp_path, monkeypatch):
    spawned = []

    class Exits:
        def __init__(self, argv, **kw):
            spawned.append(kw.get("start_new_session"))
            self.pid, self.returncode = 1, worker_mod.EXIT_DO_NOT_RESPAWN

        def poll(self):
            return self.returncode

    monkeypatch.setattr(supervisor_mod.subprocess, "Popen", Exits)
    sup = supervisor_mod.Supervisor(session_id=SID, transcript=tmp_path / "t.jsonl", cwd=tmp_path)
    _set_state("daemon")
    for _ in range(4):
        sup.poll()
    assert spawned == [True]  # its own process group, and never again in this state
    _set_state("full")
    sup.poll()
    _set_state("daemon")
    sup.poll()
    assert len(spawned) == 2  # the switch moved: try again


def test_finish_runs_one_last_cycle_then_the_conclusions_pass_and_hands_back(tmp_path, monkeypatch):
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _live_worker(tmp_path, api, body)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": len(body)}, "title": "t", "body": "b"}]
    assert worker.finish() == 0
    # The last cycle, then the conclusions pass over the whole session: the same
    # note proposed twice is ONE write (its idempotency key is the same).
    assert api.completions == 2 and [w[0] for w in api.writes] == ["POST"]
    reasons = [r for (r,) in ledger_mod.Ledger(SID).conn.execute("SELECT reason FROM boundaries ORDER BY id")]
    assert reasons[-1] == "session_end"
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_STOPPED


def test_the_worker_finishes_when_tap_watch_is_gone(tmp_path, monkeypatch):
    worker, _ = _live_worker(tmp_path, FakeApi(), b"")
    monkeypatch.setattr(worker_mod.os, "getppid", lambda: worker.parent_pid + 1)
    assert worker.run() == 0
    assert json.loads(lease.lease_path(SID).read_text())["reason"] == lease.REASON_STOPPED



def test_waiting_out_backoff_does_not_reset_the_failure_count(tmp_path, monkeypatch):
    class Down(FakeApi):
        def request(self, method, path, body=None, *, idem_key=None, params=None, timeout=30):
            raise api_mod.Retryable(503, None)

    api = Down(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/sub-notes": {"sub_notes": []}})
    body = _claude_lines(_tool_use("c1", "probe run start"), _tool_result("c1", f'{{"run_id": "{RUN}"}}'))
    worker, _ = _live_worker(tmp_path, api, body)
    api.proposals = [_note_on_run(len(body))]
    worker.cycle(shadow=False)
    clock = {"t": time.time()}
    monkeypatch.setattr(worker_mod.time, "time", lambda: clock["t"])
    worker.lease_expires_at = clock["t"] + 10_000
    turns = {"n": 0}

    def sleep(_s):
        turns["n"] += 1
        clock["t"] += 20  # a pass every 20s: most of them inside a backoff window
        worker.lease_expires_at = clock["t"] + 10_000
        if turns["n"] > 12 or not worker.live:
            worker_mod._stop = True

    monkeypatch.setattr(worker_mod, "_sleep", sleep)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    worker.run()
    assert worker.released_reason == lease.REASON_GATEWAY  # writes kept failing: handed back


def test_a_range_the_model_cannot_answer_is_skipped_not_retried_forever(tmp_path):
    class Truncates(FakeApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            return {"content": '{"proposals": [', "finish_reason": "length", "usage": {}}

    body = _claude_lines(_assistant("a huge range"))
    worker, _ = _live_worker(tmp_path, Truncates(), body)
    for _ in range(worker_mod.MAX_UNUSABLE_ANSWERS - 1):
        with pytest.raises(worker_mod.UnusableAnswer):
            worker.cycle(shadow=False)
        assert worker.ledger.watermark == 0
    assert worker.cycle(shadow=False) == "skipped"
    assert worker.ledger.watermark == len(body)


def test_an_input_named_on_the_command_line_is_not_produced(tmp_path):
    lines = [
        (10, json.dumps(_tool_use("a", "python eval.py --data data/customers.parquet")).encode()),
        (20, json.dumps(_tool_result("a", "accuracy 0.91, wrote results/report.png")).encode()),
        (30, json.dumps(_tool_use("b", "bash -lc 'cd data && cat notes.csv'")).encode()),
        (40, json.dumps(_tool_result("b", "see private.csv")).encode()),
    ]
    produced = observe.observe("claude_code", lines).produced_text
    assert "results/report.png" in produced
    assert "customers.parquet" not in produced and "private.csv" not in produced
