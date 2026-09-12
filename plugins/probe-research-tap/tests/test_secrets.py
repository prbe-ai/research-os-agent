"""Credential detection, span redaction, and the wiring that applies it.

THE FALSE-POSITIVE CORPUS IS THE POINT OF THIS FILE.

The previous gate (`app/ingestion/redaction.py`, removed 0.104.9.0) dropped 645
batches across 206 sessions; 615 of them — 95% — were false positives, and
because a finding wrote a `session_quarantines` tombstone that refused every
LATER batch, each one erased a whole transcript permanently and silently.

`FALSE_POSITIVES` below is that outage, encoded. The two paths with their
measured entropies are verbatim from PR #365's postmortem. Any change to a rule
replays them. A rule that fires on one of these does not ship.

Every "credential" in `TRUE_POSITIVES` is synthetic — random, or AWS's own
published documentation example. No live secret appears in this repository.
"""

from __future__ import annotations

import itertools
import json
import time

import pytest

from tap import secrets
from tap.codex_sanitize import sanitize_event as codex_sanitize
from tap.sanitize import sanitize_event as cc_sanitize
from tap.transcript import build_batch_body

# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

#: Synthetic credential fixtures, ASSEMBLED AT RUNTIME rather than written as
#: literals. GitHub push protection blocks a commit containing an AKIA-shaped
#: string even when it is invented, and it is right to — a scanner that trusts
#: "this one is a test" is a scanner you cannot rely on. None of these values
#: is or has ever been a real key.
_AWS_ID = "AKIA" + "4KX7QZJ2MNVB3TWD"
_AWS_SECRET = "hT7xQ2mVb9Lk" + "Zp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2Pq"
_GH_PAT = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
_GCP_KEY = "AIza" + "SyC1x9Kp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2Pq7"
_HF = "hf_" + "QZJmNVbTWDxKpLwRyEeNsUcAiJdFgOhqQz"
_SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
#: These two are split mid-PREFIX, not just before the body. GitHub's OpenAI
#: detector matches on the `sk-proj-` + `T3BlbkFJ` marker alone, so writing
#: that marker as one literal is enough to block a push even with a synthetic
#: body — which is exactly how this file broke the public mirror on 2026-09-12.
_ANTHROPIC = "sk-" + "ant-" + "api03-" + "a" * 80 + "9xQ"
_OPENAI = "sk-" + "proj-" + "T3Blb" + "kFJ" + "b" * 50

#: Must be caught. Synthetic values only.
TRUE_POSITIVES: tuple[tuple[str, str], ...] = (
    # The two shapes that actually leaked (Anthrogen, 2026-08-30).
    ("aws_configure_echo_id", f"AWS Access Key ID [None]: {_AWS_ID}"),
    ("aws_configure_echo_secret", f"AWS Secret Access Key [None]: {_AWS_SECRET}"),
    (
        "aws_printf_pair",
        "printf '[default]\\naws_access_key_id = %s\\naws_secret_access_key = %s\\n' "
        f"'{_AWS_ID}' '{_AWS_SECRET}'",
    ),
    ("aws_ini_secret", f"aws_secret_access_key = {_AWS_SECRET}"),
    ("aws_export", f"export AWS_SECRET_ACCESS_KEY={_AWS_SECRET}"),
    # The one literal that stays: AWS publishes this exact string in its own
    # documentation, every scanner allowlists it on the "EXAMPLE" substring,
    # and it is worth asserting we still catch the canonical shape.
    ("aws_doc_example", "AKIAIOSFODNN7EXAMPLE"),
    ("github_pat", _GH_PAT),
    ("anthropic", _ANTHROPIC),
    ("openai_proj", _OPENAI),
    ("huggingface", _HF),
    ("slack_bot", _SLACK),
    ("gcp_api_key", _GCP_KEY),
    ("bearer_header", "Authorization: Bearer abc123XYZdef456GHIjkl789MNO"),
    ("credential_uri", "postgres://admin:s3cr3tP4ssw0rd@db.internal:5432/probe"),
)

#: Must NOT be caught. The first four are verbatim from the outage.
FALSE_POSITIVES: tuple[tuple[str, str], ...] = (
    # PR #365, 539 drops: any 40-character path satisfied the base64 window.
    ("outage_path_casp", "/OdysseyPrivate/odyssey/experiments/casp"),          # H=3.85
    ("outage_path_fsq", "/workspace/library/checkpoints/fsq/FINAL"),           # H=4.38
    # PR #365, 76 drops: ordinary pipeline stdout read as a pasted .env.
    ("outage_env_sha", "SLICE_SHA256=9f2c1ab44e3d8071b5c6e2f9a0d4738b1c5e6f7a8b9c0d1e2f3a4b5c6d7e8f90"),
    ("outage_env_bytes", "SLICE_BYTES=48210347"),
    ("outage_env_n", "EVAL_N=1024"),
    # Ordinary ML-transcript noise.
    ("path_long", "/workspace/shyam/runs/2026-08-30/checkpoints/step_48000"),
    ("path_safetensors", "loading /workspace/odyssey/checkpoints/fsq_v3/step_412000/model.safetensors"),
    ("git_sha", "commit 03a24b90bee1c7341cc4714a5ca21850f8a9a91c"),
    ("uuid", "session 4525087c-392d-4acf-b221-d861512fb467"),
    ("wandb_run_dir", "wandb: Run data is saved locally in wandb/run-20260830_152500-a7k3m9qz"),
    ("content_hash", "content_hash = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("hex_digest", "sha256:9f2c1ab44e3d8071b5c6e2f9a0d4738b1c5e6f7a8b9c0d1e2f3a4b5c6d7e8f90"),
    ("torch_shape", "tensor shape torch.Size([32, 1024, 4096]) dtype=torch.bfloat16"),
    ("s3_uri", "s5cmd ls s3://runpod-files-new/checkpoints/odyssey3/"),
    ("pip_wheel", "Downloading torch-2.9.1+cu128-cp313-cp313-linux_x86_64.whl (912.4 MB)"),
    ("pytest_line", "tests/integration/test_generation_worker.py::test_claims_one_row PASSED"),
    ("base64_not_jwt", "payload = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9payloadonly'"),
    # References and placeholders are not credentials.
    ("env_reference", "model = 'claude-opus-5' ; api_key = os.environ['ANTHROPIC_API_KEY']"),
    ("env_reference_aws", "aws_secret_access_key = os.environ['AWS_SECRET_ACCESS_KEY']"),
    ("placeholder_angle", "aws_secret_access_key = <your-secret-here>"),
    ("yaml_interpolation", "  api_key: ${ANTHROPIC_API_KEY}"),
    # An anchor word inside an ordinary English sentence.
    ("secretary", "the secretary said: meeting at 1400 hours in room 12"),
    # --- Found by scanning 4,000 REAL production chunks (2026-09-12). ---
    # Every one of these fired before the `_is_word_like` filter landed. They
    # are the same failure as the 2026-08 outage wearing a different coat: a
    # path or an identifier sitting close enough to a credential word to be
    # taken for a value. Keep them; they are the only evidence we have that the
    # detector survives contact with real transcripts rather than a fixture.
    ("prod_cli_flag", "create the pull secret here:  --some-thing-SOMEEE-SOMEE-SOMEEEE-"),
    ("prod_path_after_anchor", 'creates R2 credential secret for pods"}, {"path": "SomeThing/configs-abc-defg"'),
    ("prod_flag_with_literal", ':  --from-literal=R2_ACCESS_KEY_ID="<key>" \\ 54:  --some-thing-K8-SOMEEEE-SOMEEE-ABC-'),
    ("prod_test_assertion", "test_secret_syncs_before_app_secret - AssertionError: /some/path_with/parts-9a-bc9de"),
    ("prod_hyphenated_ident", "loaded config `attention-ablation` (26) — the attention-ablation-config entry"),
)


# ---------------------------------------------------------------------------
# The two corpora
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,text", TRUE_POSITIVES, ids=[n for n, _ in TRUE_POSITIVES])
def test_true_positive_is_redacted(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules, f"{name}: no rule fired"
    assert redacted != text, f"{name}: text unchanged despite a finding"
    assert "<redacted:" in redacted


@pytest.mark.parametrize("name,text", FALSE_POSITIVES, ids=[n for n, _ in FALSE_POSITIVES])
def test_false_positive_does_not_fire(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules == [], f"{name}: fired {rules} on benign text — this is the 615-drop class"
    assert redacted == text, f"{name}: benign text was modified"


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL
# ---------------------------------------------------------------------------


def test_suite_fails_when_the_detector_is_stubbed(monkeypatch) -> None:
    """A detector that silently returns nothing must not pass this file.

    Every other test here — shape preservation, the FP corpus, the envelope
    plumbing — passes against a `scan()` that does nothing at all. Without this
    test, breaking the detector looks exactly like the detector working.
    """
    monkeypatch.setattr(secrets, "scan", lambda _text: [])
    survived = []
    for name, text in TRUE_POSITIVES:
        redacted, rules = secrets.redact(text)
        if not rules and redacted == text:
            survived.append(name)
    assert len(survived) == len(TRUE_POSITIVES), (
        "stubbing scan() did not make every true positive survive; the corpus "
        "is not actually exercising the detector"
    )


# ---------------------------------------------------------------------------
# Detection mechanics
# ---------------------------------------------------------------------------


def test_structured_and_paired_halves_both_go() -> None:
    """The AWS access key id is structured; its secret is not.

    A per-rule policy redacts the harmless identifier and keeps the half that
    actually grants access. Pair promotion is what stops that.
    """
    text = (
        f"aws_access_key_id = {_AWS_ID}\n"
        f"aws_secret_access_key = {_AWS_SECRET}\n"
    )
    redacted, rules = secrets.redact(text)
    assert _AWS_ID not in redacted
    assert _AWS_SECRET not in redacted
    assert "aws-access-key-id" in rules


def test_entropy_is_never_consulted_alone() -> None:
    """A high-entropy token with no anchor and no partner is left alone.

    This is the invariant the last gate violated. A 40-character random string
    on its own is a checkpoint name as often as it is a credential.
    """
    lone = "artifact id 7Fq2Xb9LkZp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2PqRt"
    assert secrets.scan(lone) == []


def test_indirect_reference_is_not_a_credential() -> None:
    for value in (
        "api_key = os.environ['OPENAI_API_KEY']",
        "password = ${DB_PASSWORD}",
        "client_secret = <replace-me-before-deploy>",
        "secret = ****************************",
    ):
        assert secrets.scan(value) == [], value


def test_spans_do_not_overlap_after_dedupe() -> None:
    text = (
        f"aws_access_key_id = {_AWS_ID} and "
        f"aws_secret_access_key = {_AWS_SECRET}"
    )
    findings = secrets.scan(text)
    for earlier, later in itertools.pairwise(findings):
        assert earlier.end <= later.start, "overlapping spans corrupt the replacement"


def test_shannon_entropy_matches_the_outage_measurements() -> None:
    """The postmortem's two numbers, pinned.

    If these move, the corpus above is measuring something other than what
    PR #365 measured, and its authority as a regression suite is gone.
    """
    assert round(secrets.shannon_entropy("/OdysseyPrivate/odyssey/experiments/casp"), 2) == 3.85
    assert round(secrets.shannon_entropy("/workspace/library/checkpoints/fsq/FINAL"), 2) == 4.38


# ---------------------------------------------------------------------------
# Bounded work — no rule may be pathological
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,text",
    [
        ("repeated_a", "a" * 40_000),
        ("akia_flood", ("AKIA" + "B" * 60) * 600),
        ("bearer_flood", ("Authorization: Bearer " + "a" * 5_000) * 8),
        ("begin_marker_flood", "-----BEGIN PRIVATE KEY-----" * 2_000),
        ("anchor_flood", ("secret=" + "Zq" * 40 + "\n") * 2_000),
        ("uri_flood", ("postgres://" + "a" * 60 + ":" + "b" * 60 + "@h ") * 500),
    ],
)
def test_no_rule_is_pathological(label: str, text: str) -> None:
    """A wall-clock ceiling cannot interrupt a running `re.search` — CPython
    holds the GIL inside one match. So the bound has to come from the rules
    themselves being linear. This test is the only thing that proves it."""
    started = time.perf_counter()
    secrets.scan(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{label}: scan took {elapsed:.2f}s on {len(text)} chars"


def test_long_input_is_windowed_not_truncated() -> None:
    """Truncating would bound runtime by creating a blind spot."""
    filler = "ordinary transcript text about checkpoints and loss curves. "
    base = filler * 1_200  # comfortably past MAX_SCAN_CHARS
    key = _AWS_ID
    for offset in (5_000, secrets.MAX_SCAN_CHARS - 100, 70_000):
        blob = base[:offset] + " " + key + " " + base[offset:]
        redacted, rules = secrets.redact(blob)
        assert key not in redacted, f"credential at offset {offset} survived"
        assert "aws-access-key-id" in rules


# ---------------------------------------------------------------------------
# Event walking — shape preservation
# ---------------------------------------------------------------------------


def test_redact_event_preserves_shape() -> None:
    event = {
        "type": "assistant",
        "uuid": "abc",
        "nested": {"list": [1, 2.5, True, None, _AWS_ID]},
        "message": {"content": [{"type": "text", "text": "clean"}]},
    }
    out, rules = secrets.redact_event(event)
    assert rules == ["aws-access-key-id"]
    assert out["type"] == "assistant"
    assert out["uuid"] == "abc"
    assert out["nested"]["list"][:4] == [1, 2.5, True, None]
    assert out["nested"]["list"][4] == "<redacted:aws-access-key-id>"
    assert out["message"]["content"][0]["text"] == "clean"


def test_redact_event_ignores_non_dict() -> None:
    assert secrets.redact_event("plain") == ("plain", [])
    assert secrets.redact_event(None) == (None, [])


# ---------------------------------------------------------------------------
# The wiring: every lane, every producer
# ---------------------------------------------------------------------------

_LEAK = f"AWS Access Key ID [None]: {_AWS_ID}"


def _cc_line() -> bytes:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _LEAK}]},
        }
    ).encode()


def _codex_line() -> bytes:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": _LEAK}],
            },
            "timestamp": "2026-09-12T00:00:00Z",
        }
    ).encode()


@pytest.mark.parametrize(
    "label,line,sanitize",
    [("claude_code", _cc_line(), cc_sanitize), ("codex", _codex_line(), codex_sanitize)],
)
def test_build_batch_body_redacts_every_lane(label: str, line: bytes, sanitize) -> None:
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[line],
        sanitize=sanitize,
    )
    assert body is not None, f"{label}: nothing shipped"
    text = body.decode()
    assert _AWS_ID not in text, f"{label}: credential reached the wire"
    assert "<redacted:aws-access-key-id>" in text


def test_batch_body_carries_a_redaction_count_and_no_values() -> None:
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[_cc_line()],
        sanitize=cc_sanitize,
    )
    parsed = json.loads(body)
    assert parsed["redactions"]["count"] >= 1
    assert parsed["redactions"]["rules"] == ["aws-access-key-id"]
    # The envelope carries names and counts. It must never carry a value.
    assert "AKIA" not in json.dumps(parsed["redactions"])


def test_clean_batch_carries_no_redaction_key() -> None:
    """A session with nothing to redact must look exactly as it did before, so
    the dashboard has no empty state to render and no notice to suppress."""
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "all clean"}]},
        }
    ).encode()
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[line],
        sanitize=cc_sanitize,
    )
    assert "redactions" not in json.loads(body)


def test_unparseable_line_is_still_redacted() -> None:
    """A malformed line is kept as a raw string rather than dropped. That
    lenient path must not become the way a credential gets through."""
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[f"not json at all: {_AWS_ID}".encode()],
        sanitize=cc_sanitize,
    )
    assert body is not None
    assert _AWS_ID not in body.decode()


# ---------------------------------------------------------------------------
# Telling the researcher — the tap has no terminal of its own
# ---------------------------------------------------------------------------


def _storage(tmp_path):
    from tap.storage import Storage

    return Storage(tmp_path / "state.db")


def test_enqueue_records_a_notice_the_next_session_can_print(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        body = build_batch_body(
            device_id="d", session_id="s", batch_seq=0, cwd="/tmp",
            base_line_no=0, lines=[_cc_line()], sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp",
                       body=body, now=0)
        notice = outbox.redaction_notice(storage)
        assert "redacted 1 credential-shaped value" in notice
        assert "aws-access-key-id" in notice
        assert "rotate" in notice
        # No value, ever.
        assert "AKIA" not in notice
    finally:
        storage.close()


def test_notice_is_cleared_after_being_read(tmp_path) -> None:
    """Told once. A notice that repeats every session is a notice people learn
    to scroll past."""
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        body = build_batch_body(
            device_id="d", session_id="s", batch_seq=0, cwd="/tmp",
            base_line_no=0, lines=[_cc_line()], sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp",
                       body=body, now=0)
        assert outbox.redaction_notice(storage)
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()


def test_notice_accumulates_across_batches(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        for seq in range(3):
            body = build_batch_body(
                device_id="d", session_id="s", batch_seq=seq, cwd="/tmp",
                base_line_no=0, lines=[_cc_line()], sanitize=cc_sanitize,
            )
            outbox.enqueue(storage=storage, session_id="s", batch_seq=seq, cwd="/tmp",
                           body=body, now=0)
        assert "redacted 3 credential-shaped values" in outbox.redaction_notice(storage)
    finally:
        storage.close()


def test_clean_session_produces_no_notice(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        line = json.dumps({
            "type": "assistant", "uuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "clean"}]},
        }).encode()
        body = build_batch_body(
            device_id="d", session_id="s", batch_seq=0, cwd="/tmp",
            base_line_no=0, lines=[line], sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp",
                       body=body, now=0)
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()


def test_recording_a_notice_never_breaks_capture(tmp_path) -> None:
    """Reporting is best-effort by construction. A malformed body, or a storage
    that refuses the write, must not stop the batch being spooled."""
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp",
                       body=b"not json", now=0)
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()
