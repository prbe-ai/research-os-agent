"""The prompts are the deliverable, so the invariants in them are tested.

The point of the fragments is that a rule cannot drift between passes. So the
tests here mostly assert that the SAME fragment reaches every prompt that needs
it, rather than checking one prompt's wording and letting three others rot.
"""

from __future__ import annotations

import pytest

from probe.cli import backfill_prompts as bp

ROOT = "/workspace/research"


def _classify(**kw):
    base = dict(root=ROOT, evidence_jsonl='{"path":"a.py","tier":"evidence"}',
                existing=[], truncated=False)
    base.update(kw)
    return bp.classify(**base)


def _import(**kw):
    base = dict(root=ROOT, project="odyssey", paths=["a.py", "b/c.csv"],
                manifest_path="/tmp/m.jsonl")
    base.update(kw)
    return bp.import_unit(**base)


# -- shared invariants reach every prompt that needs them --------------------


@pytest.mark.parametrize(
    "prompt,fragment",
    [
        (_import(), bp.NAMING),
        (_import(), bp.REFERENCES),
        (_import(), bp.DESCRIBE_ARTIFACTS),
        (_import(), bp.GROUPING),
        (_classify(), bp.REUSE),
        (_classify(), bp.DESCRIPTIONS),
    ],
)
def test_the_fragment_arrives_verbatim(prompt, fragment):
    assert fragment in prompt


def test_the_naming_rule_names_both_things_it_breaks():
    """It is not stylistic: the tree splits on '/' and the preview reads the
    extension, so a sentence in a name breaks both."""
    assert "splitting that name on '/'" in bp.NAMING
    assert "extension" in bp.NAMING
    assert "--notes" in bp.NAMING


def test_the_reference_threshold_is_generated_not_retyped():
    from probe.cli.backfill import REFERENCE_ABOVE_BYTES, human_bytes

    assert human_bytes(REFERENCE_ABOVE_BYTES) in bp.REFERENCES
    assert "--reference --allow-missing" in bp.REFERENCES
    assert "--hash" in bp.REFERENCES  # says NOT to pass it


# -- classify ----------------------------------------------------------------


def test_classify_says_the_question_is_per_file_not_per_folder():
    text = _classify()
    assert "A directory is not a project" in text
    assert "spills across directories" in text


def test_classify_uploads_nothing_and_says_so():
    assert "NOT uploading anything" in _classify()


def test_classify_lists_existing_projects_when_there_are_any():
    text = _classify(existing=["odyssey-infill-v3", "esm3-baseline"])
    assert "odyssey-infill-v3" in text and "esm3-baseline" in text
    assert "Prefer these" in text


def test_classify_says_so_when_nothing_exists_yet():
    assert "naming them for the first time" in _classify(existing=[])


def test_a_truncated_sample_budget_is_disclosed_to_the_agent():
    """An agent that believes it saw everything reports confidence it has not
    earned."""
    assert "sample budget was reached" not in _classify(truncated=False)
    text = _classify(truncated=True)
    assert "sample budget was reached" in text
    assert "low confidence" in text


def test_classify_demands_every_file_be_accounted_for():
    assert "exactly once in `assignments`" in _classify()


def test_classify_asks_for_an_unsure_list_because_that_is_what_gets_reviewed():
    text = _classify()
    assert '"unsure"' in text
    assert "the human reviews first" in text


def test_tail_files_must_name_what_decided_them():
    text = _classify()
    assert "TAIL FILES INHERIT" in text
    assert "guess wearing a rule's clothes" in text


def test_classify_carries_the_evidence_verbatim():
    assert '{"path":"zz.py","tier":"tail"}' in _classify(
        evidence_jsonl='{"path":"zz.py","tier":"tail"}'
    )


# -- import a unit -----------------------------------------------------------


def test_the_unit_prompt_pins_the_project_and_forbids_creating_one():
    text = _import(project="esm3-baseline")
    assert "esm3-baseline" in text
    assert "do not create any project" in text


def test_the_unit_prompt_lists_exactly_its_own_files():
    text = _import(paths=["michael/train.py", "shared/rows.csv"])
    assert "michael/train.py" in text and "shared/rows.csv" in text
    assert "and only these, are yours" in text


def test_the_agent_writes_a_manifest_rather_than_uploading_per_file():
    text = _import(manifest_path="/tmp/unit-7.jsonl")
    assert "/tmp/unit-7.jsonl" in text
    assert "Do not run\n`probe artifact add` yourself" in text or (
        "Do not run" in text and "probe artifact add" in text
    )


def test_a_file_with_nothing_to_say_still_goes_in_the_manifest():
    """The manifest IS the upload list, so omitting a file drops it from the
    import entirely."""
    text = _import()
    assert "a file you leave out is a\nfile that does not get imported" in text or (
        "leave out" in text and "does not get imported" in text
    )


def test_units_are_told_not_to_write_project_notes():
    text = _import()
    assert "Do NOT write the project's notes" in text
    assert "once, at the end" in text


def test_the_unit_prompt_reports_its_own_row_count():
    assert "2 files" in _import(paths=["a", "b"])


# -- map w&b -----------------------------------------------------------------


def test_wandb_mapping_prefers_projects_that_already_exist():
    text = bp.map_wandb(inventory="{}", projects=["odyssey-infill-v3"])
    assert "already exist" in text and "odyssey-infill-v3" in text
    assert "prefer them, strongly" in text


def test_wandb_mapping_warns_against_splitting_one_line_of_research():
    text = bp.map_wandb(inventory="{}", projects=["a"])
    assert "splits one line of research in half" in text


def test_wandb_mapping_handles_having_no_projects_yet():
    assert "(none yet)" in bp.map_wandb(inventory="{}", projects=[])


def test_wandb_mapping_carries_the_inventory():
    assert '{"entity":"anthrogen"}' in bp.map_wandb(
        inventory='{"entity":"anthrogen"}', projects=[]
    )


# -- notes -------------------------------------------------------------------


def test_the_notes_writer_is_told_it_is_the_only_writer():
    text = bp.write_notes(project="p", root=ROOT, landed=10, manifests=["/tmp/a.jsonl"])
    assert "only writer" in text
    assert "--append" in text  # says it does not need it


def test_the_notes_writer_gets_the_manifests_so_it_knows_what_landed():
    text = bp.write_notes(project="p", root=ROOT, landed=3, manifests=["/tmp/a.jsonl", "/tmp/b.jsonl"])
    assert "/tmp/a.jsonl" in text and "/tmp/b.jsonl" in text


def test_the_notes_writer_is_asked_for_gaps_not_just_a_summary():
    text = bp.write_notes(project="p", root=ROOT, landed=3, manifests=[])
    assert "Be honest about gaps" in text
    assert "what a reader should not trust" in text


def test_the_notes_writer_does_not_re_describe_individual_files():
    text = bp.write_notes(project="p", root=ROOT, landed=3, manifests=[])
    assert "already\ncarry their own notes" in text or "carry their own notes" in text


# -- no prompt names a door that does not open -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        _classify(),
        _import(),
        bp.map_wandb(inventory="{}", projects=["a"]),
        bp.write_notes(project="p", root=ROOT, landed=1, manifests=[]),
    ],
)
def test_every_probe_command_named_in_a_prompt_actually_exists(text):
    """A prompt that names a verb the CLI does not have sends an unattended
    agent into a retry loop it cannot get out of."""
    import re

    from typer.main import get_command

    from probe.cli.main import app

    known = set(get_command(app).commands)
    for verb in set(re.findall(r"probe ([a-z][a-z-]*)", text)):
        assert verb in known, f"prompt names `probe {verb}`, which does not exist"
