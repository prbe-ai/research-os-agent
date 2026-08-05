"""Dashboard URLs for the entities an agent creates and closes.

A run the researcher cannot open is a record they will not read. Everything the
CLI, the SDK and the MCP hand back identifies entities by uuid, which is exactly
the wrong shape for a human: it is unguessable and unclickable. This module is
the one place that turns a uuid into a link, so no caller has to know the
dashboard's route table and no model has to reconstruct it from memory.

Deriving the dashboard origin is the whole difficulty. The API base URL is NOT
reliably the dashboard's sibling: the hosted MCP pods point ``PROBE_BASE_URL`` at
an in-cluster Service (``deploy/mcp/k8s.yaml``), which names no public host at
all. So there is an explicit override that deployment sets, one derivation we can
make with certainty, and ``None`` for everything else -- see
:func:`dashboard_base_url`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlsplit

# Dashboard route segment per entity kind, mirroring dashboard/src/app in the
# research-os repo. Only these three have a page; every other kind (artifact,
# group, document) is rendered INSIDE one of them and has no addressable URL of
# its own, so it deliberately gets no entry and no link.
_ROUTES = {
    "run": "runs",
    "experiment": "experiments",
    "project": "projects",
}

#: Set this when the API host does not name the dashboard -- self-hosted installs
#: and the hosted MCP both need it. Deployment-set, never guessed.
DASHBOARD_URL_ENV = "PROBE_DASHBOARD_URL"


@lru_cache(maxsize=8)
def _derive(api_base_url: str) -> str | None:
    """Dashboard origin implied by an API base URL, or None if it implies none.

    The single shape read here is an ``api.`` label on an https host, which is how
    every hosted deployment is addressed (``api.research.prbe.ai`` ->
    ``research.prbe.ai``). Nothing else is inferred, on purpose: a plain host, a
    bare IP, an in-cluster Service name and a localhost dev API are all shapes
    where the dashboard could be on a different host, a different port, or absent
    entirely, and there is no way to tell which from the API URL alone.

    Cached because the MCP annotates every node of every browse response, and the
    answer is a pure function of a string that does not change within a process.
    """
    parts = urlsplit(api_base_url)
    # http:// is the in-cluster and localhost shape, never a public dashboard.
    if parts.scheme != "https" or not parts.hostname:
        return None
    host = parts.hostname
    if not host.startswith("api."):
        return None
    remainder = host[len("api.") :]
    # `api.` alone, or `api.localhost`, leaves nothing addressable behind it.
    if "." not in remainder:
        return None
    return f"https://{remainder}"


def dashboard_base_url(api_base_url: str | None = None) -> str | None:
    """Public dashboard origin, or ``None`` when it cannot be known.

    ``PROBE_DASHBOARD_URL`` wins outright and is how any deployment whose API host
    does not name its dashboard says so.

    Returning ``None`` is a real answer and callers must respect it: emitting no
    link costs a researcher one navigation, while emitting a WRONG one costs them
    a 404 they have no reason to distrust -- it arrived with the same authority as
    a real one. Never fall back to the public host as a guess; a self-hosted
    install would be handed links into somebody else's tenant.
    """
    explicit = (os.environ.get(DASHBOARD_URL_ENV) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if api_base_url is None:
        # Imported lazily: config resolution reads the context file, and links
        # are also computed in paths that already hold a resolved base URL.
        from .config import resolve

        api_base_url = resolve().base_url
    return _derive(api_base_url.rstrip("/"))


def entity_url(
    kind: str,
    entity_id: str | None,
    *,
    api_base_url: str | None = None,
) -> str | None:
    """Link to one entity's dashboard page, or ``None`` if it has no page.

    ``None`` for three distinct reasons -- unroutable kind, missing id, unknown
    dashboard origin -- and callers treat them identically: say nothing.
    """
    route = _ROUTES.get(str(kind))
    if not route or not entity_id:
        return None
    base = dashboard_base_url(api_base_url)
    if not base:
        return None
    return f"{base}/{route}/{entity_id}"
