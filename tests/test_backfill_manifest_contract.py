"""The manifest contract: what backfill's prompt tells an agent to WRITE must be
what `artifact add --from-manifest` READS.

The producer and the consumer were built by different lanes that never saw each
other's code. A textually clean merge does not make them agree, and the first
disagreement was invisible: rows parsed fine and landed with the wrong names.
"""

from __future__ import annotations

import re

from probe.cli import backfill_prompts as bp


def _row_keys_the_prompt_asks_for() -> set[str]:
    """The JSON keys `import_unit` instructs an agent to emit."""
    text = bp.import_unit(
        root="/drive", project="p", paths=["a.py"], manifest_path="/tmp/m.jsonl"
    )
    # The row template is the first indented JSON object after the write
    # instruction. Anchored on the manifest path so a reworded preamble does not
    # silently make this test read nothing and pass.
    after = text.split("Write JSONL to:", 1)[1]
    start = after.index('{"path"')
    block = after[start : after.index("}", start) + 1]
    assert '"path"' in block, "the row template moved; this test is reading nothing"
    return set(re.findall(r'"(\w+)":', block))


def test_the_prompt_only_asks_for_keys_the_reader_accepts():
    """An unknown key FAILS the row, so a prompt naming one nobody reads would
    make every unit enqueue nothing while reporting success."""
    from probe.cli.main import _MANIFEST_KEYS  # noqa: PLC0415

    assert _row_keys_the_prompt_asks_for() <= set(_MANIFEST_KEYS)


def test_an_artifact_name_defaults_to_the_relative_path_not_the_basename(
    tmp_path, monkeypatch
):
    """`backfill.py` states the contract the dashboard depends on: the name is
    the relative path, because the folder tree is built by splitting it on '/'.
    The prompt never asks the agent for `name`, so the default IS the contract.

    Note the chdir: a manifest row's `path` resolves against the CWD, so the
    enqueue has to run with the imported folder as its working directory. That
    is not incidental -- with the wrong cwd every row fails "is not a regular
    file", which is at least loud.
    """
    from probe.cli.main import _plan_manifest_row  # noqa: PLC0415

    target = tmp_path / "michael" / "odyssey" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("lr: 3e-4")
    monkeypatch.chdir(tmp_path)

    row = {"path": "michael/odyssey/config.yaml", "notes": "the odyssey config"}
    from probe.sdk.client import Anchor  # noqa: PLC0415

    planned = _plan_manifest_row(
        row, default_anchor=(Anchor.PROJECT, "p-1"), reference_over=10**9
    )
    assert planned["name"] == "michael/odyssey/config.yaml", (
        "a manifest name must keep its directories, or every import flattens to "
        "one folder and every config.yaml collides"
    )


def test_the_prompt_does_not_ask_the_agent_for_a_name():
    """If it ever starts to, the default above stops being load-bearing and this
    test should be revisited rather than silently kept green."""
    assert "name" not in _row_keys_the_prompt_asks_for()


def test_the_prompt_emits_parseable_jsonl_shape():
    text = bp.import_unit(
        root="/drive", project="p", paths=["a.py"], manifest_path="/tmp/m.jsonl"
    )
    assert "JSONL" in text
    for key in ("path", "notes", "reference"):
        assert f'"{key}"' in text


def test_an_absolute_path_still_falls_back_to_the_basename(tmp_path, monkeypatch):
    """An absolute path says where a file sits on the machine that wrote the
    manifest. That is not a name anyone wants in a dashboard, and it leaks a
    local layout into shared data."""
    from probe.cli.main import _plan_manifest_row  # noqa: PLC0415
    from probe.sdk.client import Anchor  # noqa: PLC0415

    target = tmp_path / "deep" / "shard.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    planned = _plan_manifest_row(
        {"path": str(target)},
        default_anchor=(Anchor.PROJECT, "p-1"),
        reference_over=10**9,
    )
    assert planned["name"] == "shard.bin"
