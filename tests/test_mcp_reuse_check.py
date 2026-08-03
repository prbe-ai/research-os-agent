"""The reuse-before-you-create seam: `get_entity(ref="artifact:<name>")`.

This is the one MCP call the server instructions, the tool description and the
`start-research-work` skill all MANDATE by name, calling duplicate identities the
most expensive avoidable error in the system.

It was mandated for a release while nothing stood behind it. research-os #143/#144
retired the asset registry into artifacts and agent #57 deleted the MCP asset
views, but every instruction site kept saying `asset:<name>`. The ref then fell
into `source.get`'s guess-every-getter loop, `get_experiment()` raised a raw 422
`uuid_parsing` on a non-UUID name, and the tool description told the agent that an
error means "a new identity is licensed" -- so the guard against duplicates
licensed one on every single call, and the instruction eval scored it a pass
because the right tool name still appeared first in the trace.

These tests pin the seam end to end so it cannot rot back: the ref resolves, both
empty answers stay distinguishable, and a retired ref kind fails LOUDLY.
"""

from __future__ import annotations

import pytest

from probe.mcp.contract import EnvelopeState
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import errors
from probe.sdk.client import Anchor


@pytest.fixture
def service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def _share(client, tmp_path, name: str) -> dict:
    blob = tmp_path / name
    blob.write_bytes(b"x")
    return client.upload_file(Anchor.SHARED, None, name, str(blob))


def _version(client, artifact_id: str, n: int) -> dict:
    """Append version `n` as a pointer. `uri` (not content_hash) because the client
    requires exactly one source and a bare hash is not one."""
    return client.create_artifact_version(artifact_id, uri=f"r2://bucket/v{n}")


# -- resolution ---------------------------------------------------------------
def test_artifact_ref_resolves_by_name(service, client, tmp_path):
    """The whole point: a NAME, not an id. There is no GET /v1/artifacts/{id}."""
    shared = _share(client, tmp_path, "exec-accuracy.py")

    out = service.get_entity(ref="artifact:exec-accuracy.py")

    assert out["data"]["entity_type"] == "artifact"
    assert out["data"]["entity"]["id"] == shared["id"]
    assert out["data"]["available_views"] == ["card", "versions"]


def test_a_prefix_match_is_not_a_match(service, client, tmp_path):
    """`prefix` narrows server-side; the EXACT match is ours. Resolving "score" to
    "score_v2" would reuse a different artifact, which is the same class of
    unreproducibility as duplicating one."""
    _share(client, tmp_path, "score_v2.py")

    with pytest.raises(errors.NotFoundError):
        service.get_entity(ref="artifact:score.py")


def test_a_duplicated_name_is_loud_and_is_not_not_found(service, client, tmp_path, app):
    """Two shared artifacts under one name is the disease this check exists to
    prevent, already present. It must NOT read as not-found -- an agent takes
    not-found as licence to create, which would make a third."""
    first = _share(client, tmp_path, "dupe.py")
    twin = dict(app.artifacts["shared:team"][0])
    twin["id"] = "00000000-0000-0000-0000-0000000000ff"
    app.artifacts["shared:team"].append(twin)

    with pytest.raises(errors.ValidationError) as excinfo:
        service.get_entity(ref="artifact:dupe.py")

    message = str(excinfo.value)
    assert first["id"] in message and twin["id"] in message
    assert "already duplicated" in message


# -- the two empty answers, which are OPPOSITES -------------------------------
def test_versions_returns_the_chain(service, client, tmp_path):
    shared = _share(client, tmp_path, "bird.jsonl")
    _version(client, shared["id"], 1)
    _version(client, shared["id"], 2)

    out = service.get_entity(ref="artifact:bird.jsonl", view="versions")

    assert [v["version"] for v in out["data"]["versions"]] == [1, 2]
    assert out["completeness"]["state"] == EnvelopeState.COMPLETE


def test_an_unsatisfiable_requirement_is_no_match_and_still_shows_the_ceiling(
    service, client, tmp_path
):
    """The failure this whole seam exists to prevent. `no_match` means the artifact
    EXISTS and your requirement is too high -- pin a new version of the SAME
    identity. Returning an empty list here would be indistinguishable from an empty
    artifact, and that confusion is what opens a second identity."""
    shared = _share(client, tmp_path, "scorer.py")
    _version(client, shared["id"], 1)

    out = service.get_entity(
        ref="artifact:scorer.py", view="versions", filters={"requirement": ">=2"}
    )

    assert out["completeness"]["state"] == EnvelopeState.NO_MATCH
    # The versions that DO exist ride along: that is the real ceiling, visible.
    assert [v["version"] for v in out["data"]["versions"]] == [1]
    assert out["data"]["satisfied_by"] is None


def test_no_match_is_not_the_same_state_as_a_satisfied_requirement(
    service, client, tmp_path
):
    shared = _share(client, tmp_path, "ok.py")
    _version(client, shared["id"], 1)
    _version(client, shared["id"], 2)

    out = service.get_entity(
        ref="artifact:ok.py", view="versions", filters={"requirement": ">=2"}
    )

    assert out["completeness"]["state"] == EnvelopeState.COMPLETE
    assert [v["version"] for v in out["data"]["versions"]] == [2]


def test_semver_shaped_requirement_is_rejected_not_silently_unmatched(
    service, client, tmp_path
):
    """Versions are monotonic integers. `">=2.0"` is the obvious way to write this
    and it is not a version here -- answering `no_match` would be a THIRD kind of
    nothing, indistinguishable from a real ceiling."""
    shared = _share(client, tmp_path, "semver.py")
    _version(client, shared["id"], 1)

    with pytest.raises(errors.ValidationError, match="monotonic integers"):
        service.get_entity(
            ref="artifact:semver.py", view="versions", filters={"requirement": ">=2.0"}
        )


# -- the resolver no longer guesses -------------------------------------------
def test_a_retired_ref_kind_says_so_instead_of_leaking_a_uuid_parse_error(service):
    """The exact regression. `asset:<name>` used to reach get_experiment(), whose
    path validator raised 422 uuid_parsing -- an error naming `experiment_id` for a
    call that never mentioned an experiment, and nothing that said assets are gone.
    """
    with pytest.raises(errors.ValidationError) as excinfo:
        service.get_entity(ref="asset:bird-bench-split", view="versions")

    message = str(excinfo.value)
    assert "unknown ref kind" in message
    assert "artifact:<name>" in message
    assert "uuid" not in message.lower()


def test_a_bare_non_uuid_id_reports_no_match_not_a_parse_failure():
    """Same leak by the other door: the bare-id fallback caught only NotFoundError,
    so a 422 from whichever getter happened to run first escaped as the answer.

    Stubbed rather than run against the FakeApp on purpose -- that fake AUTO-CREATES
    a run for any id it has not seen, so it can never produce the 404/422 this path
    is about. A fake kinder than the backend would certify the bug as fixed.
    """

    class _AllReject:
        def __getattr__(self, _name):
            def _reject(_value):
                raise errors.ValidationError("Input should be a valid UUID", status=422)

            return _reject

    source = ResearchOSSource(_AllReject())

    with pytest.raises(errors.NotFoundError, match="no run, experiment, project"):
        source.get("not-a-uuid-at-all")


def test_an_unsupported_view_on_an_artifact_names_what_is_supported(
    service, client, tmp_path
):
    _share(client, tmp_path, "t.py")

    with pytest.raises(errors.ValidationError, match=r"artifact supports \['card', 'versions'\]"):
        service.get_entity(ref="artifact:t.py", view="trajectory")
