"""A fenced caller's transcript, pinned to the file the Probe service accepted (`tap.transcripts.pinned`).

The service accepts a sandboxed caller's transcript only as a regular file of the machine's session
folder and names its identity (`dev:ino`). The recorder pins the path to it: every read opens it with
`O_NOFOLLOW | O_NONBLOCK` and checks the opened file is a regular file of that identity. A file
swapped for a link, a FIFO or another file after the check is never read (and a FIFO never blocks the
reader): the recorder stops for good (`EXIT_TRANSCRIPT_SWAPPED`, hosted: `REFUSED`). Each holder's pin
is its own: one recorder ending never unpins another's file.
"""

from __future__ import annotations

import json
import os
import threading
from unittest import mock

import pytest

from tap import hosted
from tap import main as tapmain
from tap.transcripts import pinned
from tap.transcripts.transcripts import read_new
from tests.test_durable_runtime import setup  # noqa: F401 -- the fixture

pytestmark = pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW")


def _identity(path) -> tuple[int, int]:
    found = os.stat(path)
    return found.st_dev, found.st_ino


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"n": 1}\n')
    yield path
    pinned._pins.pop(str(path), None)  # every holder's pin a test left


def test_an_unpinned_transcript_reads_as_before_even_through_a_link(tmp_path, transcript):
    link = tmp_path / "link.jsonl"
    link.symlink_to(transcript)
    assert read_new(link, 0).lines == [b'{"n": 1}']


def test_a_pinned_transcript_reads_while_it_is_the_accepted_file(transcript):
    pinned.hold_pin(transcript, _identity(transcript))
    pinned.check_pinned(transcript)
    assert read_new(transcript, 0).lines == [b'{"n": 1}']


def test_a_pinned_transcript_swapped_for_a_link_after_the_check_is_never_read(tmp_path, transcript):
    pinned.hold_pin(transcript, _identity(transcript))
    secret = tmp_path / "secret"
    secret.write_text('{"key": "secret"}\n')
    transcript.unlink()
    transcript.symlink_to(secret)
    with pytest.raises(pinned.TranscriptSwapped, match="symbolic link"):
        pinned.check_pinned(transcript)
    with pytest.raises(pinned.TranscriptSwapped):
        read_new(transcript, 0)


def test_a_pinned_transcript_replaced_by_another_file_is_never_read(tmp_path, transcript):
    pinned.hold_pin(transcript, _identity(transcript))
    other = tmp_path / "other.jsonl"
    other.write_text('{"key": "secret"}\n')
    os.replace(other, transcript)
    with pytest.raises(pinned.TranscriptSwapped, match="another file"):
        read_new(transcript, 0)


def test_one_recorder_ending_never_unpins_another_recorders_file(tmp_path, transcript):
    """#4278 review (P1): a resumed session's recorder overlaps its finishing one; both pin the same
    path. The finishing one exiting once removed the shared pin, and the survivor then opened by path
    and followed a link swapped in. Each holder releases its own pin only."""
    accepted = _identity(transcript)
    finishing = pinned.hold_pin(transcript, accepted)
    resumed = pinned.hold_pin(transcript, accepted)
    pinned.release_pin(transcript, finishing)
    secret = tmp_path / "secret"
    secret.write_text('{"key": "secret"}\n')
    transcript.unlink()
    transcript.symlink_to(secret)
    with pytest.raises(pinned.TranscriptSwapped, match="symbolic link"):
        read_new(transcript, 0)
    pinned.release_pin(transcript, finishing)  # twice: still only its own
    assert pinned.get_pin(transcript) == accepted, "the resumed recorder's pin stands"
    pinned.release_pin(transcript, resumed)
    assert pinned.get_pin(transcript) is None, "the last holder's release unpins it"


def test_a_pinned_transcript_swapped_for_a_fifo_never_blocks_its_reader(tmp_path, transcript):
    """#4278 review (P2): a FIFO in the accepted file's place, with no writer, blocked an open
    without O_NONBLOCK (the service's event loop, at the capture gate). A pinned open never waits:
    it refuses anything that is not a regular file."""
    pinned.hold_pin(transcript, _identity(transcript))
    transcript.unlink()
    os.mkfifo(transcript)
    outcome: list = []

    def read() -> None:
        try:
            pinned.check_pinned(transcript)
        except pinned.TranscriptSwapped as exc:
            outcome.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    if reader.is_alive():
        with open(os.open(transcript, os.O_WRONLY | os.O_NONBLOCK), "wb"):  # let the stuck open go
            pass
        pytest.fail("a pinned open waited on a FIFO")
    assert outcome and "regular file" in str(outcome[0])


def test_a_pinned_transcript_reads_with_blocking_cleared(transcript):
    """O_NONBLOCK is for the open only: the regular file's descriptor reads as any open file's."""
    import fcntl

    pinned.hold_pin(transcript, _identity(transcript))
    with pinned.open_transcript(transcript) as handle:
        assert not fcntl.fcntl(handle.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK
        assert handle.read() == b'{"n": 1}\n'


def test_a_missing_pinned_transcript_is_waited_for_not_called_swapped(transcript):
    pinned.hold_pin(transcript, _identity(transcript))
    transcript.unlink()
    pinned.check_pinned(transcript)  # the recorder's own missing-file handling decides


def test_the_identity_the_service_names_is_dev_colon_ino():
    assert pinned.parse_identity("12:345") == (12, 345)
    for bad in ("12", "a:b", "1:2:3", "", None, 12):
        assert pinned.parse_identity(bad) is None


@pytest.mark.parametrize("setup", ["watch"], indirect=True)
def test_the_recorder_stops_when_its_pinned_transcript_is_swapped_mid_session(setup, monkeypatch):
    """The first pass uploads the accepted file; the file is swapped for a link to another file
    between passes; the next pass reads nothing of it and the loop stops."""
    config, storage, wire = setup
    secret = config.transcript_path.with_name("secret.jsonl")
    secret.write_text(json.dumps(dict(type="user", sessionId=config.session_id,
                                      message=dict(role="user", content="SECRET-TEXT"))) + "\n")
    pinned.hold_pin(config.transcript_path, _identity(config.transcript_path))

    def swap(_seconds):
        config.transcript_path.unlink()
        config.transcript_path.symlink_to(secret)

    monkeypatch.setattr(tapmain.time, "sleep", swap)
    try:
        with pytest.raises(pinned.TranscriptSwapped):
            tapmain._run_durable_loop(config, storage)
    finally:
        pinned._pins.pop(str(config.transcript_path), None)
    sent = b"".join(v if isinstance(v, bytes) else v.encode() for v in wire.accepted.values()).decode()
    assert "turn 0" in sent, "the accepted file was recorded"
    assert "SECRET-TEXT" not in sent


def test_main_stops_for_good_on_a_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", "http://synthetic.invalid")
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "synthetic")
    transcript = tmp_path / "source.jsonl"
    transcript.write_text("{}\n")
    found = os.stat(transcript)
    seen = {}

    def run(config, storage, **_kw):
        seen["pinned"] = pinned.get_pin(config.transcript_path)
        raise pinned.TranscriptSwapped("swapped")

    argv = ["--session-id", "synthetic", "--transcript", str(transcript), "--cwd", str(tmp_path),
            "--transcript-identity", f"{found.st_dev}:{found.st_ino}"]
    with mock.patch.object(tapmain, "_run_durable_loop", side_effect=run):
        assert tapmain.main(argv) == tapmain.EXIT_TRANSCRIPT_SWAPPED
    assert seen["pinned"] == (found.st_dev, found.st_ino)
    assert pinned.get_pin(transcript) is None, "its own pin released on the way out"
    with pytest.raises(SystemExit):
        tapmain.main(["--session-id", "s", "--transcript", str(transcript), "--cwd", str(tmp_path),
                      "--transcript-identity", "not-an-identity"])


def test_a_hosted_recorder_whose_transcript_was_swapped_is_refused_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ)
    controls = hosted.Controls()
    seen = {}

    def fake_main(argv, *, hosted=None):
        seen["argv"] = argv
        return tapmain.EXIT_TRANSCRIPT_SWAPPED

    monkeypatch.setattr(tapmain, "main", fake_main)
    session = {"sid": "s-1", "cwd": str(tmp_path), "transcript": str(tmp_path / "s-1.jsonl"),
               "transcript_dir": None, "transcript_identity": "1:2",
               "env": {"PROBE_RESEARCH_TAP_PLUGIN_DIR": str(tmp_path / "plugin")}}
    result = []
    thread = threading.Thread(target=lambda: result.append(hosted.record(session, controls)))
    thread.start()
    thread.join(10)
    assert result[0][0] == hosted.REFUSED
    assert seen["argv"][-2:] == ["--transcript-identity", "1:2"]


def test_tap_start_hands_the_identity_to_the_service_with_the_session(tmp_path, monkeypatch):
    """`tap start --transcript-identity` (the service's own start for a fenced caller) asks
    `session.record` with it, so the service's recorder pins the file."""
    from tap import record_client
    from tap import start as tap_start

    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "synthetic")
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{}\n")
    asked = []
    monkeypatch.setattr(record_client, "try_record", lambda args: asked.append(args) or True)
    code = tap_start.main(["--session-id", "s-1", "--cwd", str(tmp_path), "--transcript", str(transcript),
                           "--transcript-identity", "12:345"])
    assert code == tap_start.EXIT_OK
    assert record_client._params(asked[0])["transcript_identity"] == "12:345"
    plain = tap_start.main(["--session-id", "s-2", "--cwd", str(tmp_path), "--transcript", str(transcript)])
    assert plain == tap_start.EXIT_OK and "transcript_identity" not in record_client._params(asked[1])
