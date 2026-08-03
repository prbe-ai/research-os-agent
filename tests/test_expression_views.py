"""The authoring surface for computed metrics: `probe.expr`, derived writes, views.

Two layers, deliberately different: an EXPRESSION VIEW is a formula stored as an
AST and evaluated at read time (no points stored), while a DERIVED METRIC is
whatever Python computed, pushed as real points with provenance. The AST is
closed on purpose — anything it cannot say belongs in the derived path — so
these tests hold both doors open and check they stay distinguishable.

tests/test_parity.py proves the six view routes are reachable. These prove the
bodies are shaped right.
"""

from __future__ import annotations

import json

import pytest

from probe import cli, expr
from tests.conftest import make_client, open_run


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


# -- the builder ------------------------------------------------------------
def test_operators_build_the_ast_an_agent_would_have_hand_written():
    spec = expr.spec(expr.series("train/loss") / expr.series("train/entropy"))
    assert spec["expression"] == {
        "op": "binary",
        "fn": "div",
        "left": {"op": "series", "kind": "model", "key": "train/loss"},
        "right": {"op": "series", "kind": "model", "key": "train/entropy"},
    }


def test_bare_numbers_coerce_to_constants_on_both_sides():
    """`100 - series` has to work, not just `series - 100` — an agent writing a
    percentage-error view reaches for the reflected form first."""
    right = expr.spec(expr.series("eval/acc") * 100)["expression"]
    assert right["right"] == {"op": "const", "value": 100.0}

    left = expr.spec(100 - expr.series("eval/err"))["expression"]
    assert left["fn"] == "sub"
    assert left["left"] == {"op": "const", "value": 100.0}
    assert left["right"]["key"] == "eval/err"


def test_a_bool_is_refused_rather_than_silently_becoming_one():
    with pytest.raises(TypeError, match="not bool"):
        expr.series("train/loss") * True


def test_unary_and_smoothing_compose_and_chain():
    spec = expr.log(expr.series("eval/val_loss")).ema(factor=0.9)
    node = spec.spec()["expression"]
    assert node["op"] == "smooth" and node["fn"] == "ema" and node["factor"] == 0.9
    assert node["operand"] == {
        "op": "unary",
        "fn": "log",
        "operand": {"op": "series", "kind": "model", "key": "eval/val_loss"},
    }


def test_dimensions_pin_which_series_a_leaf_means():
    node = expr.spec(expr.series("train/loss", dimensions={"rank": 0}))["expression"]
    assert node["dimensions"] == {"rank": 0}


def test_an_expr_is_immutable_so_a_shared_subtree_is_safe_to_reuse():
    base = expr.series("train/loss")
    base / expr.series("train/entropy")
    assert base.node == {"op": "series", "kind": "model", "key": "train/loss"}
    base.node["key"] = "mutated"
    assert base.node["key"] == "train/loss"


def test_spec_accepts_a_raw_dict_and_an_already_wrapped_mapping():
    """An agent that emitted JSON should not have to rebuild it through the
    builder — the spec file IS the interchange format."""
    node = {"op": "series", "kind": "model", "key": "train/loss"}
    assert expr.spec(node) == expr.spec({"expression": node})


def test_a_malformed_spec_fails_here_not_as_a_server_422():
    with pytest.raises(Exception) as excinfo:
        expr.spec({"op": "series", "key": "train/loss"})  # no kind
    assert "kind" in str(excinfo.value)


# -- the widened operator set ----------------------------------------------
def test_power_and_modulo_ride_the_python_operators():
    a, b = expr.series("a"), expr.series("b")
    assert expr.spec(a**2)["expression"]["fn"] == "pow"
    assert expr.spec(2**a)["expression"]["left"] == {"op": "const", "value": 2.0}
    assert expr.spec(a % b)["expression"]["fn"] == "mod"


def test_comparisons_build_masks_not_booleans():
    """`a > b` has to be a 0/1 CURVE — it composes with arithmetic and feeds
    cond. Returning a bool would silently collapse a whole series to one value."""
    node = expr.spec(expr.series("a") > expr.series("b"))["expression"]
    assert node["op"] == "cmp" and node["fn"] == "gt"


def test_an_expr_refuses_to_be_truthy():
    """Follows from the above: `if a > b:` is a bug, so it raises rather than
    evaluating an always-true object."""
    with pytest.raises(TypeError, match="curve, not a condition"):
        bool(expr.series("a") > expr.series("b"))


def test_cond_expresses_the_divide_by_zero_guard():
    a, b = expr.series("a"), expr.series("b")
    node = expr.spec(expr.cond(b != 0, a / b, 0))["expression"]
    assert node["op"] == "cond"
    assert node["when"]["fn"] == "ne"
    assert node["then"]["fn"] == "div"
    assert node["otherwise"] == {"op": "const", "value": 0.0}


def test_clamp_and_coalesce_chain_off_an_expr():
    a = expr.series("a")
    assert expr.spec(a.clamp(0, 1))["expression"]["op"] == "clamp"
    assert expr.spec(a.coalesce(-1))["expression"]["op"] == "coalesce"


@pytest.mark.parametrize(
    "builder",
    ["log2", "log1p", "expm1", "cbrt", "reciprocal", "sign", "floor", "ceil",
     "trunc", "sigmoid", "tanh", "relu", "sin", "cos", "tan", "asin", "acos",
     "atan", "sinh", "cosh"],
)
def test_every_unary_builder_validates(builder):
    node = expr.spec(getattr(expr, builder)(expr.series("a")))["expression"]
    assert node["op"] == "unary" and node["fn"] == builder


@pytest.mark.parametrize(
    ("builder", "fn"),
    [("min_", "min"), ("max_", "max"), ("mod", "mod"), ("hypot", "hypot"),
     ("atan2", "atan2"), ("pow_", "pow")],
)
def test_every_binary_builder_validates(builder, fn):
    node = expr.spec(getattr(expr, builder)(expr.series("a"), expr.series("b")))["expression"]
    assert node["op"] == "binary" and node["fn"] == fn


def test_builtin_shadowing_names_carry_a_trailing_underscore():
    """min/max/round/pow are builtins; shadowing them in a module an agent does
    `from probe.expr import *` on would be hostile."""
    for name in ("min_", "max_", "round_", "pow_"):
        assert name in expr.__all__
    for name in ("min", "max", "round", "pow"):
        assert name not in expr.__all__


def test_an_unknown_operator_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown cmp fn"):
        expr.cmp("approximately", expr.series("a"), 1)


# -- views through the SDK --------------------------------------------------
def test_create_list_rename_and_delete_a_view(client, app):
    run = open_run(client, experiment="views")
    view = run.create_view("loss_ratio", expr.series("train/loss") / expr.series("train/entropy"))

    assert view["name"] == "loss_ratio"
    assert view["spec"]["expression"]["fn"] == "div"
    assert [v["name"] for v in run.views()] == ["loss_ratio"]

    renamed = client.update_view(view["id"], name="train/loss_ratio")
    assert renamed["name"] == "train/loss_ratio"
    assert renamed["spec"] == view["spec"], "a rename must not touch the expression"

    client.delete_view(view["id"])
    assert run.views() == []


def test_view_data_carries_the_disclosures_not_just_points(client):
    run = open_run(client, experiment="views-data")
    view = run.create_view("ratio", expr.series("a") / expr.series("b"))
    data = run.view_data(view["id"], max_points=500)
    assert [p["value"] for p in data["points"]] == [1.5, 1.25]
    # The fields that tell a reader the curve is incomplete.
    assert data["missing_inputs"] == []
    assert data["dropped_nonfinite"] == 0
    assert data["truncated"] is False


def test_preview_evaluates_without_saving(client):
    run = open_run(client, experiment="views-preview")
    result = run.preview_view(expr.series("a") * 2)
    assert result["view_id"] is None
    assert result["points"]
    assert run.views() == [], "preview must not persist anything"


def test_update_view_with_nothing_to_change_is_refused(client):
    with pytest.raises(ValueError, match="name or a spec"):
        client.update_view("some-id")


# -- derived metrics --------------------------------------------------------
def test_log_derived_marks_the_batch_and_carries_provenance(client, app):
    run = open_run(client, experiment="derived")
    run.log_derived(
        {"eval/reward_auc": 12.5},
        step=100,
        producer="claude-code",
        note="trapezoidal integral of eval/mean_reward",
        inputs=["eval/mean_reward"],
        code_ref="scripts/auc.py@abc123",
    )
    batch = app.metric_batches_posted[-1]
    assert batch["origin"] == "derived"
    assert batch["provenance"]["producer"] == "claude-code"
    assert batch["provenance"]["code_ref"] == "scripts/auc.py@abc123"
    # A bare key resolves to a real selector rather than being rejected.
    assert batch["provenance"]["inputs"] == [{"key": "eval/mean_reward", "kind": "model"}]
    assert batch["points"][0]["step_index"] == 100


def test_provenance_records_only_what_the_caller_declared(client, app):
    """An input selector's READ-side defaults (smoothing_factor, x_axis...) have
    nothing to do with lineage; stored provenance must not imply they were
    chosen."""
    run = open_run(client, experiment="derived-clean")
    run.log_derived({"m": 1.0}, step=0, producer="p", inputs=["src"])
    selector = app.metric_batches_posted[-1]["provenance"]["inputs"][0]
    assert set(selector) == {"key", "kind"}
    assert "note" not in app.metric_batches_posted[-1]["provenance"]


def test_an_ordinary_log_stays_byte_identical(client, app):
    """origin/provenance must not ride along on the hot path — a logged batch
    has to serialize exactly as it always did."""
    run = open_run(client, experiment="logged")
    run.log({"loss": 0.5}, step=1)
    batch = app.metric_batches_posted[-1]
    assert "origin" not in batch and "provenance" not in batch


def test_agg_is_declared_through_the_model_now_not_injected_after(client, app):
    run = open_run(client, experiment="agg")
    run.log({"tokens": 12}, step=1, agg="sum")
    assert app.metric_batches_posted[-1]["points"][0]["agg"] == "sum"


def test_log_derived_series_pushes_a_whole_curve_in_one_request(client, app):
    run = open_run(client, experiment="derived-curve")
    before = len(app.metric_batches_posted)
    run.log_derived_series(
        "eval/reward_auc",
        {0: 0.0, 10: 4.5, 20: 9.25},
        producer="score_auc.py",
    )
    assert len(app.metric_batches_posted) == before + 1, "a backfill is ONE round trip"
    batch = app.metric_batches_posted[-1]
    assert [p["step_index"] for p in batch["points"]] == [0, 10, 20]
    assert batch["origin"] == "derived"


def test_log_derived_series_accepts_pairs_as_well_as_a_mapping(client, app):
    run = open_run(client, experiment="derived-pairs")
    run.log_derived_series("m", [(0, 1.0), (1, 2.0)], producer="p")
    assert [p["value"] for p in app.metric_batches_posted[-1]["points"]] == [1.0, 2.0]


# -- the CLI ----------------------------------------------------------------
# Driven through `cli.main(argv)` — the same seam the process entrypoint uses, so
# argument parsing and the exit-code mapping are under test too, not bypassed.
def test_cli_creates_a_view_from_a_spec_file(wired, tmp_path, capsys):
    """--spec-file is the "a script generated this" path, and the reason the
    dashboard needs no expression editor."""
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-views")
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(expr.spec(expr.series("train/loss") / expr.series("train/entropy"))))

    assert cli.main(["views", "create", run.id, "loss_ratio", "--spec-file", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "loss_ratio"
    assert out["spec"]["expression"]["fn"] == "div"


def test_cli_spec_file_reads_stdin(wired, tmp_path, capsys, monkeypatch):
    """A generator script pipes straight in; no temp file in the loop."""
    import io

    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-stdin")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(expr.spec(expr.series("train/loss")))))
    assert cli.main(["views", "create", run.id, "piped", "--spec-file", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "piped"


def test_cli_refuses_both_or_neither_spec_source(tmp_path):
    assert cli.main(["views", "create", "r", "n", "--spec", "{}", "--spec-file", "x.json"]) == 2
    assert cli.main(["views", "create", "r", "n"]) == 2


def test_cli_names_a_bad_spec_instead_of_posting_it(wired, capsys):
    code = cli.main(["views", "create", "r", "n", "--spec", '{"op": "series", "key": "k"}'])
    assert code == 2
    assert "invalid expression spec" in capsys.readouterr().err
    assert wired.views == {}, "nothing may reach the server"


def test_cli_lists_and_shows_a_view_by_name(wired, tmp_path, capsys):
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-show")
    run.create_view("loss_ratio", expr.series("a") / expr.series("b"))

    assert cli.main(["views", "list", run.id]) == 0
    assert [v["name"] for v in json.loads(capsys.readouterr().out)] == ["loss_ratio"]

    assert cli.main(["views", "show", run.id, "loss_ratio"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "loss_ratio"


def test_cli_show_names_the_alternatives_when_it_misses(wired, tmp_path, capsys):
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-miss")
    run.create_view("loss_ratio", expr.series("a") / expr.series("b"))

    assert cli.main(["views", "show", run.id, "nope"]) == 2
    assert "loss_ratio" in capsys.readouterr().err


def test_cli_previews_without_saving_then_deletes(wired, tmp_path, capsys):
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-preview")
    spec = json.dumps(expr.spec(expr.series("a") * 2))

    assert cli.main(["views", "preview", run.id, "--spec", spec]) == 0
    assert json.loads(capsys.readouterr().out)["points"]
    assert wired.views.get(run.id, []) == []

    view = run.create_view("doomed", expr.series("a"))
    assert cli.main(["views", "delete", view["id"]]) == 0
    assert run.views() == []


def test_cli_renames_a_view_without_touching_its_expression(wired, tmp_path, capsys):
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-rename")
    view = run.create_view("old", expr.series("a") / expr.series("b"))

    assert cli.main(["views", "rename", view["id"], "new"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "new"
    assert out["spec"] == view["spec"]


def test_cli_derived_log_requires_producer_and_step(wired, capsys):
    assert cli.main(["log", "r", "m=1", "--step", "1", "--derived"]) == 2
    assert "--producer" in capsys.readouterr().err
    assert cli.main(["log", "r", "m=1", "--derived", "--producer", "p"]) == 2
    assert "--step" in capsys.readouterr().err


def test_cli_producer_without_derived_is_refused(wired, capsys):
    """Silently logging it as a MEASURED metric would be the bad failure."""
    assert cli.main(["log", "r", "m=1", "--step", "1", "--producer", "p"]) == 2
    assert "only applies to --derived" in capsys.readouterr().err


def test_cli_derived_log_writes_provenance(wired, tmp_path, capsys):
    client = make_client(wired, tmp_spool=tmp_path / "s")
    run = open_run(client, experiment="cli-derived")
    code = cli.main([
        "log", run.id, "eval/reward_auc=12.5", "--step", "100",
        "--derived", "--producer", "claude-code", "--note", "post-hoc",
        "--input", "eval/mean_reward", "--code-ref", "scripts/auc.py@abc",
    ])
    assert code == 0
    batch = wired.metric_batches_posted[-1]
    assert batch["origin"] == "derived"
    assert batch["provenance"]["producer"] == "claude-code"
    assert batch["provenance"]["inputs"][0]["key"] == "eval/mean_reward"
    assert "derived metric" in capsys.readouterr().out
