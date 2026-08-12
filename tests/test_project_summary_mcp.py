"""The MCP contract for dashboard-visible project Markdown."""

from __future__ import annotations

from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource


def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def test_project_summary_is_a_discoverable_purpose_shaped_view(client) -> None:
    project = client.create_project(
        "visible-summary", summary_markdown="# Stable context\n\nUse the small model."
    )
    service = _service(client)

    card = service.get_entity(f"project:{project['id']}")
    assert "summary" in card["data"]["available_views"]

    summary = service.get_entity(f"project:{project['id']}", view="summary")["data"][
        "project_summary"
    ]
    assert summary["summary_markdown"].startswith("# Stable context")
    assert "server-owned AI narrative" in summary["rendering"]
    assert "Only summary_markdown" in summary["ownership"]
    assert "Whole-document, last-write-wins" in summary["write_semantics"]
    assert summary["write_with"] == "probe project set PROJECT --summary @PROJECT.md"


def test_blank_project_summary_is_an_explicit_empty_editable_document(client) -> None:
    project = client.create_project("blank-summary")
    summary = _service(client).get_entity(f"project:{project['id']}", view="summary")["data"][
        "project_summary"
    ]

    assert summary["summary_markdown"] == ""
    assert "read immediately before editing" in summary["write_semantics"]
