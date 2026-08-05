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

from ..sdk.errors import NotFoundError, UnfilteredListing

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
            f"  id:{self.ref}    (--by-id)    {describe(self.kind, self.by_id)}\n"
            f"  slug:{self.ref}  (--by-slug)  {describe(self.kind, self.by_slug)}\n"
            f"re-run with the one you meant. The id:/slug: prefix works on every command "
            f"that takes a {self.kind}; the flags only exist on the {self.kind} verbs."
        )


class ConflictingSelectors(Exception):
    """``slug:X --by-id``: the ref and the flag name different spellings.

    Refused rather than ranked. This is a disambiguator -- a rule for picking a
    winner is the exact thing it exists to remove, and the losing half is still
    sitting in the command the operator is reading back.
    """

    def __init__(self, typed: str, selector: str, flag: str) -> None:
        super().__init__(
            f"{typed!r} says --by-{selector} and the flags say --by-{flag}. "
            f"Drop one: keep the prefix, or keep the flag."
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


def _by_slug(client, kind: str, ref: str, *, strict: bool = False) -> dict | None:
    """The entity whose slug is ``ref``, or None. Server-side, uncapped.

    ``strict`` refuses to read a MISS from a backend that never applied
    ``?slug=`` -- see :class:`probe.sdk.errors.UnfilteredListing`. Only the
    ambiguity check asks for it: a HIT is trustworthy either way, because the
    SDK verifies the slug it got back rather than taking the first row.

    Kept as an SDK call rather than a direct transport read: the route literals
    live there, and ``tests/test_parity`` checks every call the client makes
    against the declared schema -- which an f-string path defeats.
    """
    resolver = getattr(client, f"resolve_{kind}")
    if not strict:
        return resolver(ref)
    try:
        return resolver(ref, strict=True)
    except TypeError:
        # A stub or an older SDK without the keyword. Losing the guard is the
        # documented degradation; losing the lookup would be a false absence.
        return resolver(ref)


def _missing(kind: str, ref: str, by: str | None, *, typed: str | None = None) -> NotFoundError:
    """``typed`` is the string as the operator wrote it, when a selector was stripped.

    Reporting the stripped remainder would answer a question they did not ask:
    someone who typed ``id:foo`` and reads ``no project with id 'foo'`` has to work
    out that the prefix was consumed rather than part of the name.
    """
    how = {"id": " (as an id)", "slug": " (as a slug)"}.get(by or "", "")
    shown = f"{typed!r} -> {ref!r}" if typed and typed != ref else repr(ref)
    return NotFoundError(f"no {kind} with id or slug {shown}{how}")


def split_selector(ref: str) -> tuple[str, str | None]:
    """``"id:abc"`` -> ``("abc", "id")``. A bare ref keeps its ``None``.

    The flag-free half of the disambiguator, and the reason it exists: a ref is
    accepted by roughly a dozen commands, but ``--by-id``/``--by-slug`` are only
    declared on the project and experiment verbs. Without a prefix, an ambiguous
    ``--project`` on (say) ``experiment create`` would raise an error advising two
    flags that command does not have -- and the project whose ID is the colliding
    string would be unaddressable there at all, since naming it IS the collision.

    Two spellings for one decision is a real cost, paid because neither covers the
    whole surface alone: the flags are discoverable in ``--help`` where the verbs
    are destructive, the prefix works everywhere.

    ``id:``/``slug:`` are therefore RESERVED at the front of a ref. A project
    literally slugged ``id:foo`` is addressed as ``slug:id:foo`` (only the first
    prefix is consumed), and the backend refuses to mint new ones -- the same
    argument as the UUID-shaped slug, since a prefix that can also be a name is
    the same one-string-two-meanings bug this module exists to end.
    """
    for selector in ("id", "slug"):
        prefix = f"{selector}:"
        if ref.startswith(prefix):
            return ref[len(prefix) :], selector
    return ref, None


def resolve(client, kind: str, ref: str, *, by: str | None = None, verify: bool = True) -> Ref:
    """Resolve a project/experiment ref to its id, refusing to guess.

    ``by`` forces one interpretation (``"id"`` / ``"slug"``); the default tries
    both and raises :class:`AmbiguousRef` when they disagree. An ``id:``/``slug:``
    prefix on ``ref`` says the same thing. Agreeing is fine and either may be
    omitted, but a prefix that CONTRADICTS the flag raises: silently letting one
    win means ``project delete slug:X --by-id`` deletes the by-slug project while
    the operator is reading the word "id" in their own command.

    ``verify=False`` skips confirming that an id-shaped ref actually EXISTS, for
    callers that were never a gate (the artifact anchors) and let the server
    answer a bad ref. It costs one request instead of two on the common path: the
    slug lookup alone already answers "is this ambiguous", which is the only
    question those callers need. Leave it True wherever absence should be a local
    error rather than a 404 later -- every verb that names a thing to change.
    """
    if kind not in SLUG_KINDS:  # pragma: no cover -- guards a caller typo
        raise ValueError(f"{kind!r} has no slug form; use resolve_run/an id")

    typed = ref
    ref, selector = split_selector(ref)
    if selector and by and selector != by:
        raise ConflictingSelectors(typed, selector, by)
    by = selector or by

    if by == "id":
        row = _by_id(client, kind, ref)
    elif by == "slug":
        row = _by_slug(client, kind, ref)
    else:
        # Slug first: it is the lookup that decides ambiguity, and when it misses
        # an unverified caller is already done. Ordering it second would pay for
        # the id fetch on every anchor that was never in doubt.
        # strict only when the ref could be an id: that is the only case where a
        # false "not a slug" silently redirects to a different entity.
        hit_slug = _by_slug(client, kind, ref, strict=is_uuid(ref))
        if hit_slug is None and not verify and is_uuid(ref):
            return Ref(ref, f"{kind} {ref}", {"id": ref})
        hit_id = _by_id(client, kind, ref)
        # Same entity via both spellings (a slug equal to its OWN id) is not a
        # conflict -- either answer is the same answer.
        if hit_id and hit_slug and str(hit_id["id"]) != str(hit_slug["id"]):
            raise AmbiguousRef(kind, ref, hit_id, hit_slug)
        row = hit_id or hit_slug

    if row is None:
        raise _missing(kind, ref, by, typed=typed)
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
