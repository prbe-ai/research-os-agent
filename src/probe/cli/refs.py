"""Turning what a person typed into the id an endpoint will accept.

ONE RULE: **a bare ref is the slug. An id is written ``id:<uuid>``.**

    probe project delete folding            # the slug
    probe project delete id:6fa49e87-...    # the id

Nothing is ever tried two ways, so a ref cannot mean two things -- not on any
command, not for any entity, not depending on what happens to exist. That is the
whole design, and everything below is a consequence of it.

Why the rule, and not a smarter resolver
----------------------------------------
This module used to accept either spelling bare and work out which was meant.
That is the shape Git has, and ``fatal: ambiguous argument`` is the same error
class. It failed here in the worst available way: ``_project_id`` parsed the ref
as a UUID first and, if that parsed, never asked whether a *slug* also matched.
Observed 2026-08-04 with two live projects:

    A: slug ``parity-smoke-18cdeb5``  id ``6fa49e87-...-9f477c0d880e``
    B: slug ``6fa49e87-...-9f477c0d880e``  id ``17acbbb2-...-0d44b23bc558``

``probe project delete 6fa49e87-...``, meaning B by slug, deleted A. Exit 0, a
``deleted`` line echoing the ref, nothing to restore.

The fix after that incident detected the collision and refused. This replaces it:
a collision cannot be *expressed* now, so there is no case to detect, no ranking
rule, and no error to read. Refusing is a good answer to an ambiguous question;
not asking one is better.

Why a prefix and not a ``--uuid`` flag
--------------------------------------
Because a single command line takes MORE THAN ONE ref::

    probe run start --project folding --experiment dockq-sweep

A flag cannot say which of them it applies to. Making it say so means
``--project-uuid`` and ``--experiment-uuid``, per ref, per verb -- the surface
multiplies with every command. A prefix rides on the ref itself, so one spelling
covers every position: positional arguments, ``--project``, ``--experiment``, and
anything added later. It is also the reason ``--by-id`` / ``--by-slug`` are gone
rather than kept as aliases: two spellings for one decision is the wart, and the
one that generalises won.

Which handle is "the name", per kind
------------------------------------
* **project / experiment** -- the slug. User-chosen, so it is what people recall.
* **run** -- the petname ``short_id`` (``tunneling-sambar-254``). Server-minted
  and structurally not a UUID, so ``GET /v1/runs/{run_ref}`` resolves either form
  server-side and no collision is constructible.
* **artifact** -- id only. There is no by-name index and a name is anchor-scoped
  rather than unique, so there is no second spelling to accept.

Absence is absence
------------------
Slug lookups go through the SDK's ``resolve_*``: a server-side ``?slug=`` on a
UNIQUE column, so 0 or 1 row and no pagination. ``strict=True`` additionally
refuses to read a MISS from a backend that never applied the filter -- FastAPI
DROPS an undeclared query param, so an older engine answers an unfiltered page
and "no slug matched" would otherwise be a false absence, the kind of answer
someone acts on by creating a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from ..sdk.errors import NotFoundError, UnfilteredListing

#: Kinds whose bare ref is a slug. Runs name themselves by petname and are
#: resolved server-side; artifacts have ids only.
SLUG_KINDS = ("project", "experiment")

#: Written in front of a ref to mean "this is the id, not the name". Reserved at
#: the front of a slug backend-side, so it can never also be somebody's name.
ID_PREFIX = "id:"

#: Accepted and ignored. The bare form already means the slug, so this only ever
#: says out loud what was true anyway -- useful in generated command lines where
#: being explicit at both ends reads better than being explicit at one.
SLUG_PREFIX = "slug:"

#: The human-readable display NAME, which is not unique. Resolves only when it
#: picks out exactly one row -- see :class:`AmbiguousName`.
NAME_PREFIX = "name:"


@dataclass(frozen=True)
class Ref:
    """A resolved ref: the id an endpoint takes, and what to show a human.

    ``label`` exists so a destructive prompt can name what is about to die rather
    than echoing back the string that was typed.
    """

    id: str
    label: str
    row: dict = field(default_factory=dict)


class AmbiguousName(Exception):
    """A ``name:`` ref matched more than one row.

    Names are free text and carry no uniqueness constraint, so this is a normal
    outcome rather than a corrupt state -- which is exactly why it must not be
    ranked or "best matched". The candidates are listed WITH their slugs, because
    the slug is the thing that would have been unambiguous.
    """

    def __init__(self, kind: str, name: str, rows: list[dict]) -> None:
        lines = "\n".join(f"  {describe(kind, row)}" for row in rows)
        super().__init__(
            f"{len(rows)} {kind}s are named {name!r}:\n{lines}\n"
            f"names are not unique -- name one of them by its slug instead."
        )


class NameFilterUnsupported(Exception):
    """The backend ignored ``?name=``, so what came back is not a name lookup.

    FastAPI DROPS a query parameter a route does not declare, so an engine
    predating the filter answers an unfiltered page. Detected exactly: a real
    ``?name=`` response cannot contain a row whose name is something else.
    """

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(
            f"this backend does not support looking {kind}s up by name, so "
            f"'name:{name}' cannot be resolved. Use the slug, or upgrade the backend."
        )


class NotASlug(NotFoundError):
    """A bare ref did not resolve, and it is id-shaped -- so it is probably an id.

    The one migration cost of the slug-default rule, and it is paid loudly. A
    caller who used to pass a bare UUID gets told the exact edit rather than a
    bare "not found", and NEVER gets a different entity: the old reading does not
    exist, so there is nothing for it to resolve to by mistake.
    """

    def __init__(self, kind: str, ref: str, *, is_an_id: bool) -> None:
        if is_an_id:
            msg = (
                f"{ref!r} is not a {kind} slug -- it is a {kind} ID. A bare ref is "
                f"always the slug, so write it as {ID_PREFIX}{ref}"
            )
        else:
            msg = (
                f"no {kind} with slug {ref!r}. A bare ref is always the slug; "
                f"if you meant an id, write {ID_PREFIX}{ref}"
            )
        super().__init__(msg)


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
    """Whether ``ref`` is id-shaped. Only ever used to make an ERROR more helpful.

    Deliberately permissive (``UUID()`` also takes the dashless, braced and
    ``urn:`` spellings) because being generous here only widens the set of
    failures that get the "write it as id:" hint. Nothing resolves on it.
    """
    try:
        UUID(ref)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def split_selector(ref: str) -> tuple[str, str]:
    """``"id:abc"`` -> ``("abc", "id")``. A bare ref -> ``(ref, "slug")``.

    Only the FIRST prefix is consumed, so a project genuinely slugged ``id:foo``
    is reachable as ``slug:id:foo``. New slugs cannot take that shape -- the
    backend reserves all three prefixes -- but rows minted before that rule must
    stay addressable.
    """
    for prefix, kind in ((ID_PREFIX, "id"), (SLUG_PREFIX, "slug"), (NAME_PREFIX, "name")):
        if ref.startswith(prefix):
            return ref[len(prefix) :], kind
    return ref, "slug"


def from_ambient(value: str | None) -> str | None:
    """Canonicalise a ref the TOOL stored, not one a person typed.

    ``probe project use`` records the project **id**, because an id survives a
    rename and a slug does not. That value then flows back in as a ``--project``
    default -- so under the slug-default rule it would stop resolving, through no
    fault of whoever set it.

    The rule is about what a human types. A machine-written anchor is already
    unambiguous and simply needs saying so, which is what this does: a bare UUID
    coming from the context file or ``PROBE_PROJECT`` is read as an id.

    New contexts store the explicit ``id:`` form (see ``project use``); this keeps
    the ones already on disk, and the env var, working either way.
    """
    if value is None:
        return None
    bare, how = split_selector(value)
    if how != "slug" or not is_uuid(bare):
        return value
    return f"{ID_PREFIX}{bare}"


def resolve(client, kind: str, ref: str, *, verify: bool = True) -> Ref:
    """Resolve a project/experiment ref to its id.

    Bare -> slug. ``id:`` -> id. There is no third case and no guessing.

    ``verify=False`` skips confirming that an ``id:`` ref exists, for callers that
    were never a gate (the artifact anchors) and let the server answer a bad ref.
    It makes an ``id:`` ref cost zero requests, which matters on the path an agent
    filing thousands of artifacts pays for.
    """
    if kind not in SLUG_KINDS:  # pragma: no cover -- guards a caller typo
        raise ValueError(f"{kind!r} has no slug form; use resolve_run/an id")

    bare, how = split_selector(ref)

    if how == "name":
        return _by_name(client, kind, bare)

    if how == "id":
        if not verify:
            return Ref(bare, f"{kind} {bare}", {"id": bare})
        try:
            row = getattr(client, f"get_{kind}")(bare)
        except NotFoundError:
            raise NotFoundError(f"no {kind} with id {bare!r}") from None
        return Ref(str(row["id"]), describe(kind, row), row)

    # strict: a MISS is about to become an error, so it has to be a real miss and
    # not an older backend quietly ignoring ?slug=.
    resolver = getattr(client, f"resolve_{kind}")
    try:
        row = resolver(bare, strict=True)
    except TypeError:
        # A stub, or a client predating the keyword. Losing the unfiltered-listing
        # guard degrades to a possible false absence; refusing to look at all
        # would be worse.
        row = resolver(bare)
    if row is None:
        raise NotASlug(kind, bare, is_an_id=_looks_like_an_id(client, kind, bare))
    return Ref(str(row["id"]), describe(kind, row), row)


def _by_name(client, kind: str, name: str) -> Ref:
    """Resolve ``name:<text>`` -- exactly one match, or refuse.

    The server filter is exact and case-insensitive. Every returned row is checked
    against the name anyway, for the same reason ``_exactly`` checks a slug: a
    backend that never declared ``?name=`` DROPS it and answers an unfiltered
    page, and acting on row one of that is how a ref hits an arbitrary entity.
    Here the drop is detectable exactly -- a genuine name response cannot contain
    a row named something else.
    """
    lister = getattr(client, f"list_{kind}s")
    rows = list(lister(name=name, limit=200).items)
    matched = [row for row in rows if str(row.get("name", "")).lower() == name.lower()]
    if len(matched) != len(rows):
        raise NameFilterUnsupported(kind, name)
    if not matched:
        raise NotFoundError(f"no {kind} named {name!r}")
    if len(matched) > 1:
        raise AmbiguousName(kind, name, matched)
    row = matched[0]
    return Ref(str(row["id"]), describe(kind, row), row)


def _looks_like_an_id(client, kind: str, ref: str) -> bool:
    """Only called on the failure path, to choose which error to print.

    Costs one request, on a command that is already failing, to turn "not found"
    into the exact edit. Never influences what resolves.
    """
    if not is_uuid(ref):
        return False
    try:
        getattr(client, f"get_{kind}")(ref)
    except Exception:  # noqa: BLE001 -- this only picks an error message
        return False
    return True


def resolve_run(client, ref: str) -> Ref:
    """Resolve a run id OR its petname ``short_id``.

    The one kind that stays polymorphic, because it cannot collide: a petname is
    server-minted and is not UUID-shaped, and ``GET /v1/runs/{run_ref}`` is the
    only item route whose path param is an untyped string rather than a UUID --
    the server resolves both forms itself. An ``id:`` prefix is accepted and
    stripped so the spelling is uniform across kinds.

    The mutating routes (``DELETE``/``PATCH /v1/runs/{run_id}``) are UUID-typed,
    which is why a petname that read fine in ``run get`` used to 422 in
    ``run delete``.
    """
    bare, how = split_selector(ref)
    if how == "name":
        return _by_name(client, "run", bare)
    try:
        row = client.get_run(bare)
    except NotFoundError:
        raise NotFoundError(f"no run with id or petname {bare!r}") from None
    return Ref(str(row["id"]), describe("run", row), row)
