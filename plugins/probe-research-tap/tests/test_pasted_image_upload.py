"""Pasted images on the wire: the tap's own urllib clients against a real server.

The gateway's media door (`POST /ingest/v1/sessions/{source}/{session_id}/media`,
research-os #2647) answers with a presigned PUT; the tap sends the image's
bytes there with exactly the signed headers and never its bearer token, then
posts the batch naming the image. `defaults.pasted_images: false` in the Probe
config (the wizard's Settings row) turns it off for the next batch.

The server below is the gateway and the store in one: it refuses a PUT whose
length or SHA-256 differs from the signed headers, as R2 / MinIO do.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import config as cfg
from tap import main as tapmain
from tap.session_journal import Journal, Wire
from tap.storage import Storage

CUSTOMER = "tenant-synthetic"
PNG = b"\x89PNG\r\n\x1a\n" + hashlib.sha256(b"synthetic screenshot").digest() * 64
SHA = hashlib.sha256(PNG).hexdigest()


class Server:
    def __init__(self, *, media: bool = True):
        self.media = media
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.stored: dict[str, bytes] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _answer(self, status: int, body: dict | None = None) -> None:
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                server.requests.append(("GET", self.path, dict(self.headers), b""))
                parts = self.path.split("?")[0].split("/")
                accepts = {"protocols": [2, 3], "fragment_versions": [1, 2], "events": True}
                if server.media:
                    accepts["media"] = True
                self._answer(200, dict(
                    protocol_version=2, customer_id=CUSTOMER, source=parts[4], session_id=parts[5],
                    state="absent", receipts=[], accepts=accepts, uploader_device_id="device-synthetic",
                ))

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.requests.append(("POST", self.path, dict(self.headers), body))
                data = json.loads(body)
                if self.path.endswith("/media"):
                    sid = self.path.split("/")[5]
                    if data["sha256"] in server.stored:
                        return self._answer(200, {"status": "have"})
                    port = self.server.server_address[1]
                    return self._answer(200, {
                        "status": "upload", "method": "PUT",
                        "url": f"http://127.0.0.1:{port}/storage/{sid}/{data['sha256']}?X-Amz-Signature=synthetic",
                        "headers": {
                            "Content-Length": str(data["size_bytes"]),
                            "Content-Type": data["media_type"],
                            "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(data["sha256"])).decode(),
                        },
                        "expires_at": "2026-10-12T09:15:00Z",
                    })
                receipt = {k: data[k] for k in (
                    "batch_seq", "source_byte_end", "source_line_end", "event_end", "prefix_sha256",
                )}
                receipt.update(body_sha256=hashlib.sha256(body).hexdigest(), finalized=bool(data.get("finalize")))
                self._answer(202, dict(status="accepted", protocol_version=data["protocol_version"], receipt=receipt))

            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.requests.append(("PUT", self.path, dict(self.headers), body))
                sha = self.path.split("?")[0].rsplit("/", 1)[-1]
                checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
                if self.headers.get("x-amz-checksum-sha256") != checksum or hashlib.sha256(body).hexdigest() != sha:
                    return self._answer(400, {"Code": "XAmzContentChecksumMismatch"})
                server.stored[sha] = body
                self._answer(200)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def of(self, method: str) -> list[tuple[str, str, dict, bytes]]:
        return [r for r in self.requests if r[0] == method]

    def batches(self) -> list[dict]:
        return [json.loads(r[3]) for r in self.of("POST") if not r[1].endswith("/media")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


def _pasted_transcript(directory: Path, sid: str) -> Path:
    path = directory / f"{sid}.jsonl"
    lines = [
        {"type": "user", "sessionId": sid, "uuid": "u-1", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(PNG).decode()}},
            {"type": "text", "text": "why is this plot empty?"},
        ]}},
        {"type": "user", "sessionId": sid, "uuid": "u-2", "message": {"role": "user", "content": "and now?"}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def test_the_media_door_gets_the_bearer_and_storage_never_does(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    wire = Wire(server.url, "synthetic-token", "claude_code", timeout=5)
    journal = Journal(server.url, CUSTOMER, "claude_code")
    sid = str(uuid4())
    path = _pasted_transcript(tmp_path, sid)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=str(tmp_path))
    staged = journal.stage(sid, cwd=str(tmp_path), pasted_images=True)
    assert journal.deliver(sid, wire)

    (door,) = [r for r in server.of("POST") if r[1].endswith("/media")]
    assert door[1] == f"/ingest/v1/sessions/claude_code/{sid}/media"
    assert door[2]["Authorization"] == "Bearer synthetic-token"
    assert json.loads(door[3]) == {"sha256": SHA, "size_bytes": len(PNG), "media_type": "image/png"}

    (put,) = server.of("PUT")
    assert put[3] == PNG and server.stored == {SHA: PNG}
    assert "Authorization" not in put[2] and "Cookie" not in put[2]
    assert put[2]["Content-Type"] == "image/png" and put[2]["Content-Length"] == str(len(PNG))

    methods = [r[0] + (" media" if r[1].endswith("/media") else "") for r in server.requests]
    assert methods == ["GET", "POST media", "PUT", "POST"], "the bytes go before the batch naming them"
    (batch,) = server.batches()
    assert json.dumps(batch).encode() and staged == server.of("POST")[-1][3]
    assert base64.b64encode(PNG).decode()[:64] not in staged.decode()
    (part,) = [p for f in batch["fragments"] for p in f.get("parts", []) if p["type"] == "image"]
    assert part["sha256"] == SHA and batch["fragment_version"] == 2
    journal.close()


@pytest.fixture
def daemon(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", server.url)
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])

    def stop(_seconds):
        tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", stop)
    sid = str(uuid4())
    config = SimpleNamespace(
        session_id=sid, transcript_path=_pasted_transcript(tmp_path, sid), cwd=tmp_path,
        token="synthetic-token", active_interval_s=1, idle_interval_s=1,
        shutdown_sentinel=tmp_path / "shutdown",
    )
    storage = Storage(tmp_path / "legacy.db")
    yield config, storage, tmp_path / "config.json"
    storage.close()


@pytest.mark.parametrize(
    "config_text,uploads",
    [
        pytest.param(None, True, id="no-config-file"),
        pytest.param({"version": 2, "contexts": {}}, True, id="key-absent"),
        pytest.param({"defaults": {"pasted_images": True}}, True, id="on"),
        pytest.param({"defaults": {"pasted_images": False}}, False, id="off"),
        pytest.param("{not json", True, id="unreadable"),
    ],
)
def test_the_daemon_reads_the_settings_toggle(daemon, server, config_text, uploads):
    config, storage, config_path = daemon
    if config_text is not None:
        config_path.write_text(config_text if isinstance(config_text, str) else json.dumps(config_text))
    assert cfg.pasted_images_on() is uploads
    assert tapmain._run_durable_loop(config, storage) == 0
    batch, *rest = server.batches()
    assert all(b.get("finalize") for b in rest)  # the stop's finalize, nothing else
    (image,) = [
        block for event in batch["events"]
        for block in event["raw"]["message"]["content"] if isinstance(block, dict) and block.get("type") == "image"
    ]
    if uploads:
        assert image["sha256"] == SHA and server.stored == {SHA: PNG}
    else:
        assert image == {"type": "image", "mimeType": "image/png", "bytes": len(base64.b64encode(PNG))}
        assert server.of("PUT") == [] and not [r for r in server.requests if r[1].endswith("/media")]
