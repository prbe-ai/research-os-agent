"""`notes` on runs and run groups (research-os 0096), through the SDK.

The regen alone did not make this field reachable. The SDK builds its request
bodies as hand-written dicts rather than from the generated models, so a widened
schema shows up in `probe._generated.models` and nowhere a caller can touch. The
tests below are about the WIRING, not the types.

`notes` is not a second `description`. A description says what the run is; notes
is what a later reader should distrust about it ("suspect, the dataloader was
stale"). With one field the two compete, which is why the server carries both.
"""

from __future__ import annotations

import pytest



def test_create_run_sends_notes_and_leaves_description_alone(client, app):
    exp = app.seed_experiment("e")
    run = client.create_run(
        exp["id"],
        "baseline",
        description="GRPO on bird-sql, 3 epochs",
        notes="suspect: the dataloader was stale for the first 400 steps",
        heartbeat=False,
    )
    row = client.get_run(run.id)
    assert row["description"] == "GRPO on bird-sql, 3 epochs"
    assert row["notes"] == "suspect: the dataloader was stale for the first 400 steps"


def test_create_project_run_sends_notes(client, app):
    """The project-direct door is a separate body-builder call site; wiring one
    and not the other is the obvious way for this to be half-done."""
    project = client.create_project("p")
    run = client.create_project_run(
        project["id"], "direct", notes="ran against the stale shard", heartbeat=False
    )
    assert client.get_run(run.id)["notes"] == "ran against the stale shard"


def test_update_run_writes_notes_post_hoc_without_touching_description(client, app):
    """The door that matters most: a run's caveat is nearly always learned after
    the run has finished."""
    exp = app.seed_experiment("e")
    run = client.create_run(exp["id"], "r", description="what it is", heartbeat=False)

    client.update_run(run.id, notes="numbers stand after the re-run")
    row = client.get_run(run.id)
    assert row["notes"] == "numbers stand after the re-run"
    assert row["description"] == "what it is"


def test_update_run_requires_at_least_one_field(client, app):
    exp = app.seed_experiment("e")
    run = client.create_run(exp["id"], "r", heartbeat=False)
    with pytest.raises(ValueError, match="name/description/notes"):
        client.update_run(run.id)


def test_group_notes_round_trip_at_create_and_patch(client, app):
    """`name` is in the group's uniqueness key within the experiment, which is the
    whole reason this field exists -- prose appended to the name mints a second
    group instead of describing the one that is there."""
    exp = app.seed_experiment("e")
    group = client.create_group(
        exp["id"], "lr-sweep", notes="varies lr over 5 values; wd fixed at 0.01"
    )
    assert group["notes"] == "varies lr over 5 values; wd fixed at 0.01"
    assert client.get_group(group["id"])["notes"] == group["notes"]

    patched = client.update_group(group["id"], notes="abandoned: lr grid centred wrong")
    assert patched["notes"] == "abandoned: lr grid centred wrong"
    # A notes-only PATCH must not disturb the name it exists to keep prose out of.
    assert patched["name"] == "lr-sweep"


def test_update_group_requires_at_least_one_field(client, app):
    exp = app.seed_experiment("e")
    group = client.create_group(exp["id"], "g")
    with pytest.raises(ValueError, match="name/spec/notes"):
        client.update_group(group["id"])


def test_omitting_notes_sends_no_key_at_all(client, app):
    """Absent must mean "leave alone", never "clear" -- so the field has to be
    absent from the BODY, not present as null. A PATCH sending {"notes": None}
    would read as an explicit clear on a server that honours nulls."""
    exp = app.seed_experiment("e")
    run = client.create_run(exp["id"], "r", heartbeat=False)
    before = len(app.requests)
    client.update_run(run.id, name="renamed")
    sent = app.requests[before].read().decode()
    assert "notes" not in sent


@pytest.mark.parametrize(
    "call",
    [
        "create_run",
        "create_project_run",
        "update_run",
        "create_group",
        "update_group",
    ],
)
def test_a_pre_0096_backend_drops_notes_and_the_sdk_says_so(call, app, client):
    """The silent drop this warning exists for: none of these schemas forbid extra
    fields, so an older backend ACCEPTS `notes`, ignores it, and answers 2xx. The
    caveat vanishes and the caller is told it succeeded.

    It warns rather than raises because create has already made the entity by the
    time the response is in hand -- raising there would leave a run on the server
    and an exception in the caller's lap, which is worse than a dropped note.
    """
    app.stores_entity_notes = False
    project = client.create_project("p")
    exp = app.seed_experiment("e", project_id=project["id"])

    with pytest.warns(UserWarning, match="predates `notes`"):
        if call == "create_run":
            client.create_run(exp["id"], "r", notes="lost", heartbeat=False)
        elif call == "create_project_run":
            client.create_project_run(project["id"], "r", notes="lost", heartbeat=False)
        elif call == "update_run":
            run = client.create_run(exp["id"], "r", heartbeat=False)
            client.update_run(run.id, notes="lost")
        elif call == "create_group":
            client.create_group(exp["id"], "g", notes="lost")
        else:
            group = client.create_group(exp["id"], "g")
            client.update_group(group["id"], notes="lost")


def test_no_warning_when_the_backend_stores_notes(client, recwarn, app):
    exp = app.seed_experiment("e")
    client.create_run(exp["id"], "r", notes="kept", heartbeat=False)
    assert not [w for w in recwarn if "predates `notes`" in str(w.message)]


def test_no_warning_when_the_caller_sent_no_notes(recwarn, app, client):
    """A pre-0096 backend is only a problem for a caller that actually wrote a
    note. Warning on every run creation against an old backend would be noise."""
    app.stores_entity_notes = False
    exp = app.seed_experiment("e")
    client.create_run(exp["id"], "r", heartbeat=False)
    assert not [w for w in recwarn if "predates `notes`" in str(w.message)]
