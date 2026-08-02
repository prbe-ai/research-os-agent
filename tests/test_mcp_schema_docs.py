"""The `search_in` mapping table in the tool docstring must agree with the code.

The mapping lives in two places by necessity: ``_SEARCH_IN_TO_BACKEND`` is what
runs, and the table in ``search_knowledge``'s docstring is what AGENTS read --
and agents cannot read the dict. Nothing else ties them together: the schema
fixture pin in test_mcp_threading.py compares the docstring against its own
previous self, so it catches a CHANGE but never a DISAGREEMENT.

A stale mapping table is worse than none, because callers act on it. Same
problem, same solution as tests/test_skills_sync.py: fail in CI rather than
silently.

This checks PAIRS, not key presence. "every key appears somewhere in the
docstring" would pass a table that named all five values with all the wrong
backend corpora against them.
"""

from __future__ import annotations

import inspect
import re

import anyio

from probe.mcp.contract import BackendCorpus
from probe.mcp.server import create_server
from probe.mcp.service import _SEARCH_IN_TO_BACKEND


def _tool_description() -> str:
    tools = anyio.run(create_server(object()).list_tools)
    description = next(t for t in tools if t.name == "search_knowledge").description
    return inspect.cleandoc(description or "")


def _documented_pairs(description: str) -> dict[str, set[str]]:
    """Parse `a -> b + c` arrows out of the docstring table, ONE PER LINE.

    Line-scoped on purpose: a multiline regex reads `documents -> github + files`
    followed by `assets -> files` as one mapping with the next row's key swallowed
    into the target, which silently loses rows and made this test vacuously green.
    """
    backend_names = {c.value for c in BackendCorpus}
    tool_names = {str(v) for v in _SEARCH_IN_TO_BACKEND}
    pairs: dict[str, set[str]] = {}
    for line in description.splitlines():
        match = re.match(r"\s*([\w,\s]+?)\s*->\s*([\w\s+]+?)\s*$", line)
        if not match:
            continue
        sources = [w for w in re.split(r"[,\s]+", match.group(1)) if w in tool_names]
        targets = {w for w in re.split(r"[+\s]+", match.group(2)) if w in backend_names}
        if not sources or not targets:
            continue
        for source in sources:
            pairs.setdefault(source, set()).update(targets)
    return pairs


def test_the_docstring_table_matches_the_mapping_it_documents() -> None:
    expected = {value: {c.value for c in targets} for value, targets in _SEARCH_IN_TO_BACKEND.items()}
    documented = _documented_pairs(_tool_description())
    assert documented == expected, (
        "the search_in mapping table in search_knowledge's docstring disagrees "
        f"with _SEARCH_IN_TO_BACKEND.\n  documented: {documented}\n  actual:     {expected}"
    )


def test_the_parser_would_actually_catch_a_wrong_table() -> None:
    """Guards the guard. A parser that quietly matches nothing would make the
    test above vacuously green, which is exactly the failure mode it exists to
    prevent."""
    good = "  transcripts -> transcripts\n  documents -> github + files"
    assert _documented_pairs(good) == {
        "transcripts": {"transcripts"},
        "documents": {"github", "files"},
    }
    # A wrong association must not be read as the right one.
    wrong = "  transcripts -> github\n  documents -> files"
    assert _documented_pairs(wrong) != _documented_pairs(good)
    # Two mappings crammed onto one line parse as NEITHER, rather than as one
    # bogus merged row. Refusing beats guessing: the table is the contract.
    assert _documented_pairs("transcripts -> transcripts   documents -> files") == {}


def test_every_value_is_documented_at_all() -> None:
    """Adding a sixth value without touching the docstring is the drift this
    file exists to catch, and it would otherwise present as a passing suite."""
    documented = _documented_pairs(_tool_description())
    missing = set(_SEARCH_IN_TO_BACKEND) - set(documented)
    assert not missing, f"undocumented search_in values: {sorted(missing)}"
