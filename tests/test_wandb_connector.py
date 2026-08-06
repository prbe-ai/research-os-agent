"""The W&B connector, against REAL wandb artifacts rather than a mocked format.

The fixtures here run ``wandb.init(mode="offline")`` and log known curves, then
assert the reader recovers them exactly. Mocking the binary log would have made
the two traps this module exists to survive untestable: they are properties of
what W&B actually writes, not of a schema anyone documented.

Everything needing the package is skip-guarded, so the suite still passes on a
machine without wandb. The credential, tier-reporting and Probe-mapping tests do
NOT need it and always run.
"""

from __future__ import annotations

import json
import shutil
import sys
import traceback
import types
from pathlib import Path

import pytest

from probe.connectors import wandb as wb


def _wandb_available() -> bool:
    try:
        import wandb  # noqa: F401
        from wandb.proto import wandb_internal_pb2  # noqa: F401
        from wandb.sdk.internal import datastore  # noqa: F401
    except Exception:
        return False
    return True


needs_wandb = pytest.mark.skipif(not _wandb_available(), reason="wandb not installed")


# --------------------------------------------------------------------------
# real fixtures
# --------------------------------------------------------------------------

#: The curves the generated run logs. Asserted back EXACTLY, so any silent
#: coercion in the reader shows up as a value mismatch rather than a shrug.
CURVES = {
    "train/loss": [1.0, 0.5, 0.25, 0.125, 0.0625],
    "eval/acc": [0.10, 0.25, 0.50, 0.75, 0.90],
    "nested.a": [2.0, 4.0, 6.0, 8.0, 10.0],
}
CONFIG = {"lr": 0.001, "batch_size": 32, "optimizer": "adamw"}


def _make_offline_run(directory, *, project="wb-proj", name="wb-run"):
    """Generate a REAL offline W&B run. Returns its run directory."""
    import wandb

    run = wandb.init(
        project=project,
        name=name,
        dir=str(directory),
        mode="offline",
        config=dict(CONFIG),
        settings=wandb.Settings(silent=True),
    )
    try:
        for step in range(5):
            run.log(
                {
                    "train/loss": CURVES["train/loss"][step],
                    "eval/acc": CURVES["eval/acc"][step],
                    "nested": {"a": CURVES["nested.a"][step]},
                    # A string is not a metric point; it must be dropped, not
                    # coerced, exactly as sdk/run.py::_as_metric_value does.
                    "note": "not a number",
                },
                step=step,
            )
    finally:
        run.finish()
    directory = Path(directory)
    dirs = sorted((directory / "wandb").glob("*run-*"))
    real = [d for d in dirs if d.is_dir() and not d.is_symlink()]
    assert real, f"wandb wrote no run directory under {directory}"
    return real[0]


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory):
    if not _wandb_available():
        pytest.skip("wandb not installed")
    return _make_offline_run(tmp_path_factory.mktemp("wb-offline"))


# --------------------------------------------------------------------------
# 1. detection
# --------------------------------------------------------------------------


def test_discovers_both_online_and_offline_run_directories(tmp_path):
    root = tmp_path / "wandb"
    online = root / "run-20260806_120000-aaa111"
    offline = root / "offline-run-20260806_130000-bbb222"
    for path in (online, offline):
        path.mkdir(parents=True)
        (path / f"run-{path.name.split('-')[-1]}.wandb").write_bytes(b"")

    found = {d.run_id: d for d in wb.discover_run_dirs(tmp_path)}
    assert set(found) == {"aaa111", "bbb222"}
    assert found["bbb222"].offline is True
    assert found["aaa111"].offline is False
    assert all(d.has_binary for d in found.values())


def test_the_latest_run_symlink_does_not_duplicate_a_run(tmp_path):
    """``wandb/latest-run`` is a symlink to a sibling run dir. Left unresolved it
    matches the run-dir pattern in its own right and imports every run twice."""
    root = tmp_path / "wandb"
    real = root / "offline-run-20260806_120000-abc123"
    real.mkdir(parents=True)
    (real / "run-abc123.wandb").write_bytes(b"")
    (root / "latest-run").symlink_to(real)
    (root / "run-20260806_120000-abc123").symlink_to(real)

    found = wb.discover_run_dirs(tmp_path)
    assert len(found) == 1, [str(d.path) for d in found]


def test_discovery_finds_runs_moved_out_of_the_wandb_folder(tmp_path):
    archived = tmp_path / "results" / "2026-08" / "offline-run-20260806_120000-zzz999"
    archived.mkdir(parents=True)
    assert [d.run_id for d in wb.discover_run_dirs(tmp_path)] == ["zzz999"]


def test_discovery_on_a_missing_root_is_an_error_not_an_empty_list(tmp_path):
    with pytest.raises(wb.WandbConnectorError):
        wb.discover_run_dirs(tmp_path / "nope")


def test_a_non_run_directory_is_not_a_run(tmp_path):
    (tmp_path / "checkpoints").mkdir()
    assert wb.discover_run_dirs(tmp_path) == []


# --------------------------------------------------------------------------
# 2. local read — the binary transaction log
# --------------------------------------------------------------------------


@needs_wandb
def test_reads_every_metric_series_exactly(offline_run):
    run = wb.read_local_run(offline_run)

    assert run.tier is wb.ReadTier.HISTORY
    assert run.tier.is_full_history
    assert run.project == "wb-proj"
    assert run.display_name == "wb-run"
    assert run.exit_code == 0

    for key, expected in CURVES.items():
        series = run.metrics[key]
        assert series.steps == (0, 1, 2, 3, 4), key
        assert series.values == tuple(expected), key


@needs_wandb
def test_the_nested_key_trap_does_not_collapse_the_series(offline_run):
    """W&B leaves ``item.key`` EMPTY on history items and fills ``nested_key``.

    First proves the trap is live in the installed wandb (every history item has
    a blank ``.key``), then proves the reader survived it: three distinct series,
    none of them named "", each with its own five points. A reader that used
    ``.key`` would produce exactly one series called "" holding 15 points.
    """
    import glob

    from wandb.proto import wandb_internal_pb2 as pb
    from wandb.sdk.internal import datastore

    path = glob.glob(str(offline_run / "*.wandb"))[0]
    store = datastore.DataStore()
    store.open_for_scan(path)
    plain_keys, nested_keys = set(), set()
    while True:
        raw = store.scan_data()
        if raw is None:
            break
        record = pb.Record()
        record.ParseFromString(raw)
        if record.WhichOneof("record_type") == "history":
            for item in record.history.item:
                plain_keys.add(item.key)
                nested_keys.add(tuple(item.nested_key))
    assert plain_keys == {""}, "the trap is gone; this test no longer guards anything"
    assert ("train/loss",) in nested_keys

    run = wb.read_local_run(offline_run)
    assert "" not in run.metrics
    assert set(run.metrics) == set(CURVES)
    assert [len(s) for s in run.metrics.values()] == [5, 5, 5]


@needs_wandb
def test_slashes_stay_in_a_key_and_dict_nesting_becomes_a_dotted_path(offline_run):
    """``train/loss`` is ONE nested_key element; ``{"nested": {"a": ...}}`` is two."""
    run = wb.read_local_run(offline_run)
    assert "train/loss" in run.metrics
    assert "nested.a" in run.metrics


@needs_wandb
def test_step_comes_from_the_record_envelope_not_a_counter(offline_run):
    run = wb.read_local_run(offline_run)
    assert run.metrics["train/loss"].as_points()[0] == (0, 1.0)
    assert run.metrics["train/loss"].as_points()[-1] == (4, 0.0625)


@needs_wandb
def test_underscore_keys_are_metadata_not_metrics(offline_run):
    run = wb.read_local_run(offline_run)
    assert not [k for k in run.metrics if k.startswith("_")]
    assert run.wandb_meta.get("_step") == 4


@needs_wandb
def test_non_numeric_values_are_dropped_not_coerced(offline_run):
    run = wb.read_local_run(offline_run)
    assert "note" not in run.metrics


@needs_wandb
def test_config_survives_the_round_trip_without_wandb_bookkeeping(offline_run):
    run = wb.read_local_run(offline_run)
    assert run.config["lr"] == 0.001
    assert run.config["batch_size"] == 32
    assert run.config["optimizer"] == "adamw"
    assert "_wandb" not in run.config


@needs_wandb
def test_read_local_runs_walks_a_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("wb-tree")
    _make_offline_run(root, project="a", name="one")
    _make_offline_run(root, project="b", name="two")
    runs = wb.read_local_runs(root)
    assert len(runs) == 2
    assert {r.display_name for r in runs} == {"one", "two"}
    assert all(r.tier is wb.ReadTier.HISTORY for r in runs)


# --------------------------------------------------------------------------
# 2b. degradation
# --------------------------------------------------------------------------


@needs_wandb
def test_a_truncated_log_keeps_what_decoded_and_says_it_was_truncated(
    offline_run, tmp_path
):
    """A run killed mid-write is the normal shape of a crashed job. Discarding
    the whole curve because the tail is missing would be the wrong trade."""
    victim = tmp_path / "offline-run-20260806_120000-trunc1"
    shutil.copytree(offline_run, victim)
    binary = next(victim.glob("*.wandb"))
    data = binary.read_bytes()
    binary.write_bytes(data[: int(len(data) * 0.6)])

    run = wb.read_local_run(victim)
    assert run.tier is wb.ReadTier.HISTORY
    assert run.warnings, "a truncated read that reports nothing is the real bug"
    assert any("mid-record" in w or "kept the" in w for w in run.warnings)
    # Whatever survived is a genuine PREFIX of the real curve, not a scramble.
    survived = run.metrics["train/loss"].as_points()
    assert 0 < len(survived) <= 5
    assert survived == [(i, CURVES["train/loss"][i]) for i in range(len(survived))]


@needs_wandb
def test_a_garbage_log_falls_back_to_the_summary_and_reports_the_tier(tmp_path):
    victim = tmp_path / "offline-run-20260806_120000-junk01"
    victim.mkdir(parents=True)
    (victim / "run-junk01.wandb").write_bytes(b"this is not a wandb file" * 40)
    (victim / "wandb-summary.json").write_text(
        json.dumps({"train/loss": 0.0625, "eval/acc": 0.9, "_step": 4})
    )

    run = wb.read_local_run(victim)
    assert run.tier is wb.ReadTier.SUMMARY
    assert run.metrics["train/loss"].as_points() == [(4, 0.0625)]
    assert any("fell back" in w for w in run.warnings)


def test_a_run_with_no_binary_is_summary_tier_and_says_so(tmp_path):
    """The load-bearing assertion of this whole module: a summary-only import
    must NEVER be reportable as curve coverage."""
    run_dir = tmp_path / "offline-run-20260806_120000-sumonly"
    run_dir.mkdir(parents=True)
    (run_dir / "wandb-summary.json").write_text(
        json.dumps({"train/loss": 0.0625, "eval/acc": 0.9, "_step": 4, "_runtime": 12.5})
    )

    run = wb.read_local_run(run_dir)
    assert run.tier is wb.ReadTier.SUMMARY
    assert run.tier.is_full_history is False
    assert set(run.metrics) == {"train/loss", "eval/acc"}
    assert run.metrics["eval/acc"].as_points() == [(4, 0.9)]
    assert run.wandb_meta["_runtime"] == 12.5
    note = run.coverage_note()
    assert "FINAL VALUES ONLY" in note and "intermediate steps are absent" in note
    assert any("no run-<id>.wandb" in w for w in run.warnings)


def test_a_legacy_history_jsonl_is_a_real_curve_and_upgrades_the_tier(tmp_path):
    run_dir = tmp_path / "offline-run-20260806_120000-legacy1"
    run_dir.mkdir(parents=True)
    (run_dir / "wandb-history.jsonl").write_text(
        "\n".join(
            json.dumps({"_step": i, "train/loss": 1.0 / (i + 1), "note": "skip"})
            for i in range(4)
        )
    )
    (run_dir / "wandb-summary.json").write_text(json.dumps({"train/loss": 0.25, "_step": 3}))

    run = wb.read_local_run(run_dir)
    assert run.tier is wb.ReadTier.HISTORY
    assert run.metrics["train/loss"].as_points() == [
        (0, 1.0),
        (1, 0.5),
        (2, 1 / 3),
        (3, 0.25),
    ]
    assert "note" not in run.metrics


def test_a_corrupt_jsonl_line_is_skipped_rather_than_fatal(tmp_path):
    run_dir = tmp_path / "offline-run-20260806_120000-badline"
    run_dir.mkdir(parents=True)
    (run_dir / "wandb-history.jsonl").write_text(
        '{"_step": 0, "loss": 1.0}\n{not json at all\n{"_step": 1, "loss": 0.5}\n'
    )
    run = wb.read_local_run(run_dir)
    assert run.metrics["loss"].as_points() == [(0, 1.0), (1, 0.5)]
    assert any("not JSON" in w for w in run.warnings)


def test_an_unusable_wandb_internal_degrades_to_the_summary_tier(tmp_path, monkeypatch):
    """The whole reason the internal import is confined to one function.

    Simulates wandb being absent (or its internals renamed) with a real binary
    log present: the read must fall back to the summary and REPORT the tier
    rather than crashing or claiming curve coverage it does not have.
    """
    run_dir = tmp_path / "offline-run-20260806_120000-noint1"
    run_dir.mkdir(parents=True)
    (run_dir / "run-noint1.wandb").write_bytes(b"\x00" * 64)
    (run_dir / "wandb-summary.json").write_text(json.dumps({"train/loss": 0.5, "_step": 9}))

    def _no_datastore(_path):
        raise wb.WandbConnectorError("wandb.sdk.internal.datastore is unavailable")

    monkeypatch.setattr(wb, "_open_datastore", _no_datastore)

    run = wb.read_local_run(run_dir)
    assert run.tier is wb.ReadTier.SUMMARY
    assert run.metrics["train/loss"].as_points() == [(9, 0.5)]
    assert any("datastore is unavailable" in w for w in run.warnings)


def test_a_run_directory_with_nothing_readable_reports_tier_none(tmp_path):
    run_dir = tmp_path / "offline-run-20260806_120000-empty1"
    run_dir.mkdir(parents=True)
    run = wb.read_local_run(run_dir)
    assert run.tier is wb.ReadTier.NONE
    assert run.metrics == {}
    assert "no metric data recovered" in run.coverage_note()


def test_reading_something_that_is_not_a_run_directory_errors_clearly(tmp_path):
    (tmp_path / "notarun").mkdir()
    with pytest.raises(wb.WandbConnectorError) as excinfo:
        wb.read_local_run(tmp_path / "notarun")
    assert "offline-run-" in str(excinfo.value)


def test_one_unreadable_run_does_not_stop_the_others(tmp_path):
    good = tmp_path / "offline-run-20260806_120000-good111"
    good.mkdir(parents=True)
    (good / "wandb-summary.json").write_text(json.dumps({"loss": 1.0, "_step": 0}))
    bad = tmp_path / "offline-run-20260806_120000-bad1111"
    bad.mkdir(parents=True)

    runs = {r.run_id: r for r in wb.read_local_runs(tmp_path)}
    assert runs["good111"].tier is wb.ReadTier.SUMMARY
    assert runs["bad1111"].tier is wb.ReadTier.NONE


# --------------------------------------------------------------------------
# 3. credentials
# --------------------------------------------------------------------------


@pytest.fixture
def no_ambient_credentials(monkeypatch):
    """No env key, no netrc. The config file is already isolated by conftest."""
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    monkeypatch.setattr(wb, "_netrc_key", lambda: None)


def test_no_credential_anywhere_is_an_actionable_error(no_ambient_credentials):
    assert wb.resolve_api_key() is None
    with pytest.raises(wb.WandbCredentialsMissing) as excinfo:
        wb.require_api_key()
    message = str(excinfo.value)
    assert "wandb.ai/authorize" in message
    assert "WANDB_API_KEY" in message
    assert "wandb login" in message
    assert "store_api_key" in message


def test_credential_precedence_is_env_then_netrc_then_config(monkeypatch):
    wb.store_api_key("from-config")
    monkeypatch.setattr(wb, "_netrc_key", lambda: None)
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    assert wb.resolve_api_key() == "from-config"

    monkeypatch.setattr(wb, "_netrc_key", lambda: "from-netrc")
    assert wb.resolve_api_key() == "from-netrc"

    monkeypatch.setenv("WANDB_API_KEY", "from-env")
    assert wb.resolve_api_key() == "from-env"
    assert wb.resolve_api_key("explicit") == "explicit"


def test_the_stored_key_lands_in_the_probe_config_under_a_redacted_name():
    """Stored like every other probe credential — and under the exact spelling
    ``sdk/redaction.py`` already classes as sensitive, so a captured payload
    carrying it is scrubbed without this module doing anything."""
    from probe.sdk import config as sdk_config
    from probe.sdk.redaction import default_scrub, is_sensitive_key

    path = wb.store_api_key("wb-secret-key-value")
    stored = json.loads(path.read_text())
    context = stored["contexts"][stored["current_context"]]
    assert context[wb.CONFIG_KEY] == "wb-secret-key-value"
    assert sdk_config.load_context().get(wb.CONFIG_KEY) == "wb-secret-key-value"

    assert is_sensitive_key(wb.CONFIG_KEY)
    scrubbed = default_scrub({wb.CONFIG_KEY: "wb-secret-key-value"})
    assert scrubbed == {wb.CONFIG_KEY: "<redacted>"}


def test_logout_clears_the_stored_wandb_key_with_everything_else():
    from probe.sdk import config as sdk_config

    wb.store_api_key("wb-secret-key-value")
    sdk_config.clear_context()
    assert sdk_config.load_context().get(wb.CONFIG_KEY) is None


def test_an_empty_key_is_refused_rather_than_stored():
    with pytest.raises(wb.WandbConnectorError):
        wb.store_api_key("   ")


def test_api_key_status_reports_presence_and_origin_but_never_the_key(monkeypatch):
    monkeypatch.setenv("WANDB_API_KEY", "wb-secret-key-value")
    monkeypatch.setattr(wb, "_netrc_key", lambda: None)
    status = wb.api_key_status()
    assert status == {
        "configured": True,
        "source": "env",
        "sources": {"env": True, "netrc": False, "config": False},
    }
    assert "wb-secret-key-value" not in json.dumps(status)


def test_the_api_key_never_appears_in_an_exception_or_a_log(monkeypatch, caplog):
    """W&B's HTTP layer echoes the request back in some errors, so an exception
    string is a real leak path into logs, tracebacks and bug reports."""
    secret = "wb-0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("WANDB_API_KEY", secret)

    fake = types.ModuleType("wandb")

    def _api(*_args, **kwargs):
        raise RuntimeError(f"401 Unauthorized for key={kwargs.get('api_key')}")

    fake.Api = _api
    monkeypatch.setitem(sys.modules, "wandb", fake)

    with caplog.at_level("DEBUG"):
        with pytest.raises(wb.WandbConnectorError) as excinfo:
            wb.fetch_hosted_runs("ent", "proj")

    # The FORMATTED traceback is the real leak surface, not just str(exc):
    # `raise ... from exc` keeps the original on __cause__ and Python prints the
    # whole chain, so the unredacted wandb message would appear anyway.
    traceback_text = "".join(
        traceback.format_exception(
            type(excinfo.value), excinfo.value, excinfo.value.__traceback__
        )
    )
    assert secret not in traceback_text
    assert secret not in str(excinfo.value)
    assert secret not in caplog.text
    assert excinfo.value.__cause__ is None
    assert "<redacted>" in str(excinfo.value)


def test_a_listing_failure_is_redacted_too(monkeypatch):
    secret = "wb-0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("WANDB_API_KEY", secret)

    class _Api:
        def runs(self, path):
            raise RuntimeError(f"GET {path}?apiKey={secret} -> 500")

    fake = types.ModuleType("wandb")
    fake.Api = lambda *a, **k: _Api()
    monkeypatch.setitem(sys.modules, "wandb", fake)

    with pytest.raises(wb.WandbConnectorError) as excinfo:
        wb.fetch_hosted_runs("ent", "proj")
    assert secret not in str(excinfo.value)


# --------------------------------------------------------------------------
# 3b. hosted pull, against a fake of the DOCUMENTED public API
# --------------------------------------------------------------------------


class _FakeHostedRun:
    def __init__(self, run_id, rows, *, name="hosted", project="p", entity="e"):
        self.id = run_id
        self.name = name
        self.project = project
        self.entity = entity
        self.config = {"lr": 0.01, "_wandb": {"x": 1}}
        self._rows = rows

    def scan_history(self):
        return iter(self._rows)


@pytest.fixture
def fake_wandb_api(monkeypatch):
    rows = [
        {"_step": i, "_runtime": float(i), "train/loss": 1.0 / (i + 1), "tag": "skip"}
        for i in range(4)
    ]
    handles = [_FakeHostedRun("h1", rows), _FakeHostedRun("h2", rows, name="other")]

    class _Api:
        def runs(self, path):
            assert path == "ent/proj"
            return handles

        def run(self, path):
            return handles[0]

    fake = types.ModuleType("wandb")
    fake.Api = lambda *a, **k: _Api()
    monkeypatch.setitem(sys.modules, "wandb", fake)
    monkeypatch.setenv("WANDB_API_KEY", "wb-key")
    return handles


def test_hosted_pull_uses_scan_history_and_normalizes_like_the_local_reader(
    fake_wandb_api,
):
    runs = wb.fetch_hosted_runs("ent", "proj")
    assert [r.run_id for r in runs] == ["h1", "h2"]
    first = runs[0]
    assert first.tier is wb.ReadTier.HISTORY
    assert first.source == "wandb.ai"
    assert first.qualified_name == "e/p/h1"
    assert first.metrics["train/loss"].as_points() == [
        (0, 1.0),
        (1, 0.5),
        (2, 1 / 3),
        (3, 0.25),
    ]
    assert "tag" not in first.metrics
    assert "_runtime" not in first.metrics
    assert first.config == {"lr": 0.01}


def test_hosted_pull_can_be_narrowed_to_specific_run_ids(fake_wandb_api):
    assert [r.run_id for r in wb.fetch_hosted_runs("ent", "proj", run_ids=["h2"])] == ["h2"]


def test_fetch_hosted_run_reads_one(fake_wandb_api):
    assert wb.fetch_hosted_run("ent", "proj", "h1").run_id == "h1"


def test_hosted_pull_without_a_key_raises_before_touching_the_network(
    no_ambient_credentials, monkeypatch
):
    def _explode(*_a, **_k):
        raise AssertionError("must not reach the wandb API without a key")

    fake = types.ModuleType("wandb")
    fake.Api = _explode
    monkeypatch.setitem(sys.modules, "wandb", fake)
    with pytest.raises(wb.WandbCredentialsMissing):
        wb.fetch_hosted_runs("ent", "proj")


# --------------------------------------------------------------------------
# 4. mapping onto Probe
# --------------------------------------------------------------------------


def _summary_run(**kwargs):
    run = wb.WandbRun(
        run_id="abc123",
        project="wb-proj",
        entity="wb-ent",
        display_name="sweep-7",
        tier=wb.ReadTier.SUMMARY,
        metrics={"train/loss": wb.MetricSeries("train/loss", (4,), (0.0625,))},
        warnings=["fell back from the transaction log: no run-<id>.wandb"],
        source="/tmp/x",
    )
    for key, value in kwargs.items():
        setattr(run, key, value)
    return run


def _history_run(**kwargs):
    run = wb.WandbRun(
        run_id="abc123",
        project="wb-proj",
        entity="wb-ent",
        display_name="sweep-7",
        tier=wb.ReadTier.HISTORY,
        config={"lr": 0.001, "hf_token": "must-not-leak"},
        metrics={
            "train/loss": wb.MetricSeries(
                "train/loss", tuple(range(5)), tuple(CURVES["train/loss"])
            ),
            "eval/acc": wb.MetricSeries(
                "eval/acc", tuple(range(5)), tuple(CURVES["eval/acc"])
            ),
        },
        exit_code=0,
        source="/tmp/x",
    )
    for key, value in kwargs.items():
        setattr(run, key, value)
    return run


def test_the_target_project_is_an_input_and_is_never_invented(client):
    """W&B import runs AFTER a file import and may land in a project that
    already exists, so the caller owns the mapping."""
    with pytest.raises(wb.WandbConnectorError) as excinfo:
        wb.import_wandb_run(client, _history_run(), project="")
    assert "explicit target Probe project" in str(excinfo.value)


def test_import_opens_a_project_direct_run_with_no_invented_experiment(client, app):
    client.create_project("folding")
    result = wb.import_wandb_run(client, _history_run(), project="folding")

    row = app.runs[result.probe_run_id]
    assert row["experiment_id"] is None
    assert row["project_id"] == client.resolve_project("folding")["id"]
    assert row["name"] == "sweep-7"
    assert row["external_id"] == "wandb:wb-ent/wb-proj/abc123"


def test_the_whole_curve_rides_in_one_request_per_metric(client, app):
    client.create_project("folding")
    before = len(app.metric_batches_posted)
    result = wb.import_wandb_run(client, _history_run(), project="folding")

    posted = app.metric_batches_posted[before:]
    assert len(posted) == 2, "one batch per metric key — never one per step"
    assert result.requests == 2
    assert result.points_written == 10
    keys = {batch["points"][0]["key"] for batch in posted}
    assert keys == {"train/loss", "eval/acc"}
    loss = next(b for b in posted if b["points"][0]["key"] == "train/loss")
    assert [p["step_index"] for p in loss["points"]] == [0, 1, 2, 3, 4]
    assert [p["value"] for p in loss["points"]] == CURVES["train/loss"]


def test_imported_metrics_are_derived_and_name_their_producer(client, app):
    """They were measured by W&B and transcribed here. Anything else would let a
    reader mistake a transcription for this run's own telemetry."""
    client.create_project("folding")
    wb.import_wandb_run(client, _history_run(), project="folding")

    batch = app.metric_batches_posted[-1]
    assert batch["origin"] == "derived"
    assert batch["provenance"]["producer"] == wb.DEFAULT_PRODUCER
    assert "full step history" in batch["provenance"]["note"]
    assert "wb-ent/wb-proj/abc123" in batch["provenance"]["note"]


def test_a_summary_only_import_carries_its_tier_into_the_provenance(client, app):
    """The load-bearing case: one point per metric must never read as a curve."""
    client.create_project("folding")
    result = wb.import_wandb_run(client, _summary_run(), project="folding")

    assert result.tier is wb.ReadTier.SUMMARY
    assert result.points_written == 1
    assert result.warnings
    note = app.metric_batches_posted[-1]["provenance"]["note"]
    assert "FINAL VALUES ONLY" in note
    assert app.runs[result.probe_run_id]["metadata"]["wandb_tier"] == "summary"


def test_import_links_the_wandb_run_id(client, app):
    client.create_project("folding")
    result = wb.import_wandb_run(client, _history_run(), project="folding")
    assert app.runs[result.probe_run_id]["foreign_keys"] == {
        "wandb_run_id": "abc123",
        "wandb_project": "wb-proj",
        "wandb_entity": "wb-ent",
    }


def test_a_secret_in_the_wandb_config_is_scrubbed_on_the_way_in(client, app):
    """``Client(redact=True)`` is opt-in; a connector must not need it to avoid
    importing somebody's HF token into Probe."""
    client.create_project("folding")
    result = wb.import_wandb_run(client, _history_run(), project="folding")
    config = app.runs[result.probe_run_id]["config"]
    assert config["lr"] == 0.001
    assert config["hf_token"] == "<redacted>"


def test_a_run_with_nothing_to_import_is_refused_with_its_reason(client):
    client.create_project("folding")
    empty = wb.WandbRun(run_id="x", tier=wb.ReadTier.NONE, warnings=["log was garbage"])
    with pytest.raises(wb.WandbConnectorError) as excinfo:
        wb.import_wandb_run(client, empty, project="folding")
    assert "log was garbage" in str(excinfo.value)


def test_metric_selection_names_what_is_missing(client):
    client.create_project("folding")
    with pytest.raises(wb.WandbConnectorError) as excinfo:
        wb.import_wandb_run(
            client, _history_run(), project="folding", metrics=["train/loss", "nope"]
        )
    assert "nope" in str(excinfo.value)
    assert "eval/acc" in str(excinfo.value)


def test_a_nonzero_wandb_exit_code_imports_as_a_failed_run(client, app):
    client.create_project("folding")
    result = wb.import_wandb_run(client, _history_run(exit_code=1), project="folding")
    assert app.runs[result.probe_run_id]["status"] == "failed"


def test_dry_run_reports_the_plan_without_opening_a_probe_run(client, app):
    client.create_project("folding")
    before = len(app.runs)
    result = wb.import_wandb_run(client, _history_run(), project="folding", dry_run=True)
    assert len(app.runs) == before
    assert result.requests == 2 and result.points_written == 10


@needs_wandb
def test_end_to_end_a_real_offline_run_lands_in_probe_with_its_curves(
    client, app, offline_run
):
    client.create_project("folding")
    run = wb.read_local_run(offline_run)
    result = wb.import_wandb_run(client, run, project="folding")

    assert result.tier is wb.ReadTier.HISTORY
    assert result.metrics_written == 3
    assert result.points_written == 15
    posted = {b["points"][0]["key"]: b for b in app.metric_batches_posted[-3:]}
    assert [p["value"] for p in posted["train/loss"]["points"]] == CURVES["train/loss"]
    assert app.runs[result.probe_run_id]["foreign_keys"]["wandb_run_id"] == run.run_id


@needs_wandb
def test_import_local_runs_imports_a_whole_tree(client, app, tmp_path_factory):
    root = tmp_path_factory.mktemp("wb-import-tree")
    _make_offline_run(root, project="a", name="one")
    _make_offline_run(root, project="b", name="two")
    client.create_project("folding")

    results = wb.import_local_runs(client, root, project="folding")
    assert len(results) == 2
    assert all(r.tier is wb.ReadTier.HISTORY for r in results)
