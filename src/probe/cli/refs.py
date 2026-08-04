"""Turning what a person typed into the id an endpoint will accept.

Every mutating ``/v1/{kind}/{id}`` route types its path param as ``format: uuid``
(see ``schema/openapi.json``), so a slug reaches the server as a 422 about UUID
parsing rather than a lookup. Slugs and petnames are the handles people actually
remember, so they are resolved here -- once, in one place, the same way for every
verb that takes a ref.

The reason this is a module and not four one-liners next to their commands: the
resolution rule is a SAFETY property, not a convenience. ``project delete`` and
``project get`` disagreeing about what ``X`` means is how a ref that reads fine in
a dry run destroys something else for real. One resolver means a ref cannot mean
two things depending on which verb consumed it.

The bug this exists to prevent
------------------------------
The previous rule was "parse it as a UUID; if that works it IS an id". A project
whose slug is itself UUID-shaped was therefore unreachable by slug, AND -- far
worse -- naming it silently addressed whichever project owned that UUID as its id.
Observed 2026-08-04 with two live projects:

    A: slug ``parity-smoke-18cdeb5``  id ``6fa49e87-...-9f477c0d880e``
    B: slug ``6fa49e87-...-9f477c0d880e``  id ``17acbbb2-...-0d44b23bc558``

``probe project delete 6fa49e87-...``, meaning B by slug, would have permanently
deleted A. No error, no warning, nothing to undo. So a ref that matches both an id
and a slug is now REFUSED (:class:`AmbiguousRef`) and the caller has to say which
they meant with ``--by-id`` / ``--by-slug``. Silently preferring either one is what
the incident was.

Where the ambiguity can and cannot arise
----------------------------------------
Only where the human handle is free-form enough to be UUID-shaped:

* **project / experiment** -- slug is derived from a user-supplied name, so it can
  be anything. Both need the disambiguator.
* **run** -- the handle is the server-minted petname ``short_id``
  (``tunneling-sambar-254``). A caller cannot choose it and its shape is not a
  UUID, so there is nothing to collide. ``GET /v1/runs/{run_ref}`` already accepts
  either form, so resolution is one server-side call and the server owns the tie.
* **artifact** -- no ``GET /v1/artifacts/{id}`` and no by-name index exists, and a
  name is anchor-scoped rather than unique, so there is no second form to be
  ambiguous WITH. Artifacts stay id-only on purpose; this is not an oversight to
  be "fixed" by scanning listings, which would reintroduce a cap-bounded scan.

Absence is absence
------------------
Slug lookups go through the SDK's ``resolve_*``, which is a server-side
``?slug=`` filter on a UNIQUE column: 0 or 1 row, no pagination. The rule this
replaces scanned ``list_projects(limit=200)``, so a slug on project 201+ reported
``no project with id or slug X`` -- a false absence indistinguishable from a real
one, and the kind of answer someone acts on by creating a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from ..sdk.errors import NotFoundError

# The handle that is not the id, per kind. Runs are excluded deliberately: their
# short_id is server-minted, so it is resolved server-side (see resolve_run).
SLUG_KINDS = ("project", "experiment")


class AmbiguousRef(Exception):
    """A ref names one entity by id and a DIFFERENT one by slug.

    Carries both candidates so the caller can print what each choice would hit --
    the operator is one keystroke from an irreversible verb and "ambiguous" alone
    does not tell them which is which.
    """

    def __init__(self, kind: str, ref: str, by_id: dict, by_slug: dict) -> None:
        self.kind, self.ref, self.by_id, self.by_slug = kind, ref, by_id, by_slug
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"{self.ref!r} is BOTH a {self.kind} id and a {self.kind} slug, and they are "
            f"different {self.kind}s:\n"
            f"  --by-id    {describe(self.kind, self.by_id)}\n"
            f"  --by-slug  {describe(self.kind, self.by_slug)}\n"
            f"re-run with the one you meant."
        )


@dataclass(frozen=True)
class Ref:
    """A resolved ref: the id an endpoint takes, and what to show a human.

    ``label`` exists so a destructive prompt can name what is about to die rather
    than echoing back the string that was typed -- which, in the ambiguous case,
    is precisely the string that does not identify it.
    """

    id: str
    label: str
    row: dict = field(default_factory=dict)


def describe(kind: str, row: dict) -> str:
    """``project "Parity smoke" - parity-smoke-18cdeb5 - 6fa49e87-...``.

    Name AND handle AND id, because each answers a different question: the name is
    what the operator recognises, the handle is what they typed, and the id is the
    thing that is actually about to be addressed.
    """
    parts: list[str] = []
    name = row.get("name")
    if name:
        parts.append(f'"{name}"')
    handle = row.get("slug") or row.get("short_id")
    if handle and handle != name:
        parts.append(str(handle))
    parts.append(str(row.get("id", "?")))
    return f"{kind} " + " - ".join(parts)


def is_uuid(ref: str) -> bool:
    """Whether ``ref`` could be an id. Deliberately permissive.

    ``UUID()`` also accepts the dashless and ``urn:`` spellings. Counting those as
    id-shaped only ever adds an ambiguity CHECK, never skips one, so erring
    permissive here fails safe.
    """
    try:
        UUID(ref)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _by_id(client, kind: str, ref: str) -> dict | None:
    """The entity whose id is ``ref``, or None. No request when ref is not id-shaped."""
    if not is_uuid(ref):
        return None
    try:
        return getattr(client, f"get_{kind}")(ref)
    except NotFoundError:
        return None


def _by_slug(client, kind: str, ref: str) -> dict | None:
    """The entity whose slug is ``ref``, or None. Server-side, uncapped."""
    return getattr(client, f"resolve_{kind}")(ref)


def _missing(kind: str, ref: str, by: str | None) -> NotFoundError:
    how = {"id": " (as an id)", "slug": " (as a slug)"}.get(by or "", "")
    return NotFoundError(f"no {kind} with id or slug {ref!r}{how}")


def resolve(client, kind: str, ref: str, *, by: str | None = None) -> Ref:
    """Resolve a project/experiment ref to its id, refusing to guess.

    ``by`` forces one interpretation (``"id"`` / ``"slug"``); the default tries
    both and raises :class:`AmbiguousRef` when they disagree.
    """
    if kind not in SLUG_KINDS:  # pragma: no cover -- guards a caller typo
        raise ValueError(f"{kind!r} has no slug form; use resolve_run/an id")

    if by == "id":
        row = _by_id(client, kind, ref)
    elif by == "slug":
        row = _by_slug(client, kind, ref)
    else:
        hit_id = _by_id(client, kind, ref)
        hit_slug = _by_slug(client, kind, ref)
        # Same entity via both spellings (a slug equal to its OWN id) is not a
        # conflict -- either answer is the same answer.
        if hit_id and hit_slug and str(hit_id["id"]) != str(hit_slug["id"]):
            raise AmbiguousRef(kind, ref, hit_id, hit_slug)
        row = hit_id or hit_slug

    if row is None:
        raise _missing(kind, ref, by)
    return Ref(str(row["id"]), describe(kind, row), row)


def resolve_run(client, ref: str) -> Ref:
    """Resolve a run id OR its petname ``short_id``.

    ``GET /v1/runs/{run_ref}`` is the one item route whose path param is an
    untyped string rather than a UUID -- it resolves either form server-side. The
    mutating routes (``DELETE``/``PATCH /v1/runs/{run_id}``) are UUID-typed, which
    is why a petname that reads fine in ``run get`` used to 422 in ``run delete``.
    """
    try:
        row = client.get_run(ref)
    except NotFoundError:
        raise _missing("run", ref, None) from None
    return Ref(str(row["id"]), describe("run", row), row)
