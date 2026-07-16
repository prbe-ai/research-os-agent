"""Contract-parity guard: every backend operation is reachable from the client.

This is the test that should have existed. `make regen` regenerates
`_generated/models.py` from `schema/openapi.json`, so a new backend route silently
grows a *model* while the hand-written `sdk/client.py` + `cli/main.py` stay blind to
it. Nothing failed, so nobody noticed the client couldn't call it. This test closes
that loop: it diffs the schema against what the client actually calls.

How reachability is decided
---------------------------
We parse the client's real call sites with `ast` rather than regexing source, because
paths are built by f-string (`f"/v1/artifacts/{aid}/confirm"`) and module constant
(`_START_PATH`). Both are resolved to a path *template* — every interpolation becomes
`{}` — and the schema's paths are normalized the same way, so `/v1/runs/{run_ref}`
matches a call site written as `f"/v1/runs/{run_id}"`. Positional structure is what
matters; parameter *names* are the backend's business.

The two allowlists are deliberately different things
----------------------------------------------------
`NOT_CLIENT_SURFACE` is permanent: routes that are somebody else's job (browser, infra,
dashboard). `PENDING` is a debt ledger: routes we intend to reach but haven't. Keeping
them apart stops "not our job" from quietly absorbing "not done yet".

Both are self-cleaning. Three failure modes are enforced (see the tests below): an
unlisted unreachable route fails; an allowlisted route that becomes reachable fails
(delete the entry); an allowlist entry the schema no longer declares fails (the route
was renamed or dropped). So the lists cannot rot into fiction.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src" / "probe"
_SCHEMA = _ROOT / "schema" / "openapi.json"

# Attribute name -> HTTP method, for calls shaped `<receiver>.<verb>("/path", ...)`.
_VERB_ATTRS = {
    "get": "GET",
    "post": "POST",
    "patch": "PATCH",
    "delete": "DELETE",
    "put": "PUT",
    "get_page": "GET",
}
# Calls shaped `x.<name>("METHOD", "/path", ...)` — Transport.request, Client.write.
_METHOD_FIRST = {"request", "write"}
_HTTP_METHODS = {"GET", "POST", "PATCH", "DELETE", "PUT"}

# Both branches gate on the receiver, so a lookalike (`ROUTES.get("/v1/x")` on a plain
# dict, `audit_log.write("POST", "/v1/x")` on a logger) can't be mistaken for an HTTP
# call and mark a route reachable that nothing calls — which would hide the exact gap
# this file exists to find. Everything in this repo reaches the wire through a
# `*transport` attribute; the exceptions are bare httpx clients named `http`
# (sdk/device.py, before a Transport exists) and `client` (mcp/server.py).
_HTTP_RECEIVER_NAMES = {"http", "client"}


def _is_http_receiver(receiver: str) -> bool:
    """Receiver for a verb call (`x.get("/path")`): an HTTP client, not a dict."""
    return receiver.endswith("transport") or receiver in _HTTP_RECEIVER_NAMES


def _is_dispatch_receiver(receiver: str) -> bool:
    """Receiver for a method-first call (`x.write("POST", "/path")`).

    `write`/`request` are common method names (files, buffers, loggers), so the
    method+path shape alone is not enough — `audit_log.write("POST", "/x")` would
    otherwise register. Only the SDK's own dispatchers carry these: `Client.write`
    (receiver `self`), the composed clients (`self.client` / `self._client`), and
    `Transport.request` (`*transport`). A logger/buffer receiver is rejected.
    """
    return (
        receiver == "self"
        or receiver.endswith("client")  # self.client, self._client, a bare `client`
        or _is_http_receiver(receiver)
    )

_PARAM = re.compile(r"\{[^}]*\}")

Op = tuple[str, str]


def _normalize(path: str) -> str:
    """`/v1/runs/{run_ref}/bundle` -> `/v1/runs/{}/bundle`."""
    return _PARAM.sub("{}", path)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "string"` / `NAME: str = "string"` (e.g. device.py's
    _START_PATH)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = node.value.value
    return out


def _as_path(node: ast.expr, constants: dict[str, str]) -> str | None:
    """Resolve an argument to a path template, or None if it isn't a static path."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):  # f-string
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            elif isinstance(piece, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts)
    return None


def _arg_node(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """The positional-or-keyword argument expression (`t.get(path=...)` counts)."""
    if len(node.args) > index:
        return node.args[index]
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _arg(node: ast.Call, index: int, keyword: str, constants: dict[str, str]) -> str | None:
    """That argument, resolved to a string."""
    found = _arg_node(node, index, keyword)
    return _as_path(found, constants) if found is not None else None


def _is_opaque_path(node: ast.expr | None) -> bool:
    """True when the path is BUILT by an expression we cannot read — `.format()`,
    `"/".join(...)`, `"/v1/" + x`, `"%s" % x`.

    A bare name or subscript is NOT opaque: that is a generic dispatcher forwarding a
    caller-supplied path (`Client.write`, the spool replay), whose real routes are
    recorded at its own call sites. Flagging those would be noise.
    """
    return isinstance(node, (ast.Call, ast.BinOp))


def _receiver(node: ast.Call) -> str:
    """The call's receiver as source text: `self.transport.get(...)` -> `self.transport`."""
    return ast.unparse(node.func.value) if isinstance(node.func, ast.Attribute) else ""


def _scan(src: Path | None = None, root: Path | None = None) -> tuple[dict[Op, list[str]], list[str]]:
    """Walk the client for HTTP call sites.

    Returns `(operations -> call sites, opaque transport calls)`.

    Only args resolving to a literal starting with "/" are recorded, which is what
    keeps `dict.get("some_key")` and `put_url(absolute_r2_url)` out — neither looks
    like an API path.

    KNOWN LIMIT: only literals, f-strings, and module-level string constants resolve.
    A path built by `.format()`, `+`, `%`, or `"/".join(...)` is invisible here. That
    is why the second return value exists: a `*transport` call whose path is BUILT by
    such an expression is reported, so a new unreadable style fails loudly instead of
    silently marking a route unreachable — or, worse, letting a PENDING entry sit
    there forever after the route was actually wired.
    """
    src, root = src or _SRC, root or _ROOT
    found: dict[Op, list[str]] = {}
    opaque: list[str] = []

    for py in sorted(src.rglob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            where = f"{py.relative_to(root)}:{node.lineno}"
            attr, receiver = node.func.attr, _receiver(node)
            if attr in _METHOD_FIRST and _is_dispatch_receiver(receiver):
                # `write`/`request` need BOTH an HTTP-method arg0 AND a dispatcher
                # receiver: the method string alone would let `audit_log.write("POST",
                # "/x")` through, and the receiver alone would catch `file.write("POST")`
                # (no path). Together they pin it to a real SDK dispatch.
                path_node = _arg_node(node, 1, "path")
                method = _arg(node, 0, "method", constants)
                path = _arg(node, 1, "path", constants)
                if not method or method.upper() not in _HTTP_METHODS:
                    continue
            elif attr in _VERB_ATTRS and _is_http_receiver(receiver):
                path_node = _arg_node(node, 0, "path")
                method, path = _VERB_ATTRS[attr], _arg(node, 0, "path", constants)
            else:
                continue

            if path and path.startswith("/"):
                found.setdefault((method.upper(), _normalize(path)), []).append(where)
            elif _is_dispatch_receiver(receiver) and _is_opaque_path(path_node):
                opaque.append(f"{where}  ({receiver}.{attr}(...))")
    return found, opaque


def client_call_sites() -> dict[Op, list[str]]:
    """Every HTTP operation the client makes, mapped to its `file:line` call sites."""
    return _scan()[0]


def schema_operations() -> dict[Op, str]:
    """Every operation the backend declares, mapped to its operationId."""
    spec = json.loads(_SCHEMA.read_text())
    ops: dict[Op, str] = {}
    for path, item in spec["paths"].items():
        for verb, operation in item.items():
            if verb.upper() not in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
                continue  # `parameters`, `servers`, ... are not operations
            ops[(verb.upper(), _normalize(path))] = operation.get("operationId", "?")
    return ops


# --------------------------------------------------------------------------
# Allowlist 1: PERMANENT. Not the SDK/CLI's job. Every entry states why.
# --------------------------------------------------------------------------
NOT_CLIENT_SURFACE: dict[Op, str] = {
    ("GET", "/healthz"): "infra liveness probe; not a client call",
    ("GET", "/livez"): "infra liveness probe; not a client call",
    # MCP OAuth: for third-party MCP clients doing browser-based auth against the
    # hosted MCP server. `probe` is the non-agent surface and authenticates with a PAT.
    ("GET", "/.well-known/oauth-authorization-server"): "MCP OAuth discovery; browser/agent surface",
    ("GET", "/oauth/authorize"): "MCP OAuth; browser surface",
    ("POST", "/oauth/consent"): "MCP OAuth; browser surface",
    ("GET", "/oauth/jwks"): "MCP OAuth; key discovery for token verifiers",
    ("POST", "/oauth/register"): "MCP OAuth dynamic client registration; agent surface",
    ("POST", "/oauth/token"): "MCP OAuth; browser/agent surface",
    # Browser OIDC legs: redirects, not JSON.
    ("GET", "/auth/login"): "browser OIDC redirect leg",
    ("GET", "/auth/callback"): "browser OIDC redirect leg",
    ("POST", "/auth/logout"): "clears the session cookie; the CLI revokes via DELETE /v1/tokens/current",
    # Session-only by construction. A PAT is bound to one team at the row level and
    # /v1/me deliberately reports one tenant, so these cannot answer a token caller.
    ("GET", "/auth/me"): "session-only; /v1/me is the machine-credential door and IS wired",
    ("POST", "/auth/switch-team"): (
        "session-only. A PAT is team-bound at the row level, so this 403s for the CLI. "
        "Teams are not a switchable axis; workspaces are (2026-07-15 decision)."
    ),
    ("POST", "/auth/teams"): "session-only team admin; dashboard surface",
    ("GET", "/auth/teams/members"): "session-only (require_team_role -> require_session); dashboard surface",
    ("PATCH", "/auth/teams/members/{}"): "session-only team admin; dashboard surface",
    ("DELETE", "/auth/teams/members/{}"): "session-only team admin; dashboard surface",
    ("GET", "/auth/teams/invites"): "session-only team admin; dashboard surface",
    ("POST", "/auth/teams/invites"): "session-only team admin; dashboard surface",
    ("DELETE", "/auth/teams/invites/{}"): "session-only team admin; dashboard surface",
    ("POST", "/auth/teams/invites/{}/resend"): "session-only team admin; dashboard surface",
    ("GET", "/auth/invites/pending"): "session-only; dashboard surface",
    ("POST", "/auth/invites/{}/accept"): "session-only; dashboard surface",
    # The device-flow approval leg is the human's half of the handshake: the CLI
    # starts (/auth/device/code) and exchanges (/auth/device/token) — both wired —
    # while a signed-in human approves in the dashboard's /authorize page.
    ("GET", "/auth/device/requests/{}"): "session-only; the dashboard /authorize page is the human surface",
    ("POST", "/auth/device/requests/{}"): "session-only; the dashboard /authorize page approves/denies",
    # Deliberate security invariant, not an oversight: "a leaked token must not be
    # able to mint more tokens" (app/auth/token_router.py). It rejects PATs outright,
    # so `probe token create` drives the device flow instead — a human approves in
    # the browser and the same backend mints exactly one PAT.
    ("POST", "/v1/tokens"): "session-only mint by design; `probe token create` uses the device flow",
    # Inbound webhook: GitHub POSTs here (HMAC-verified), the `probe` client never
    # calls it. Structurally not a client surface, like the liveness probes.
    ("POST", "/webhooks/github"): "inbound GitHub webhook; server receives, client never calls",
}

# --------------------------------------------------------------------------
# Allowlist 2: TEMPORARY debt. Routes we intend to reach but have not yet.
# --------------------------------------------------------------------------
# All of these are entangled with the in-flight workspaces + KB fold-in, which makes
# `ProjectOut.workspace_id` required and adds a 4th artifact anchor (workspace). They
# are deliberately deferred so the `probe project` group and the artifact anchor
# generalization are each designed ONCE against the final model, rather than built now
# and reworked. Tracked in tasks/probe-workspace-context-handoff.md.
_WORKSPACES = "deferred to the workspaces pass — see tasks/probe-workspace-context-handoff.md"
# The workspaces + KB fold-in (backend PR #42/#43) also shipped the workspace surface
# itself, federated search, and the GitHub knowledge-connector. These are all part of
# that same program and belong to the workspaces client pass — NOT permanently
# not-our-job. Whether the GitHub-integration management (ADMIN-scoped, browser OAuth
# install flow) is CLI surface or dashboard-only is a decision for THAT pass to make,
# so it sits in PENDING rather than being pre-declared permanent here.
_CONNECTORS = "workspaces + KB connector pass — see tasks/probe-workspace-context-handoff.md"

PENDING: dict[Op, str] = {
    ("PATCH", "/v1/projects/{}"): _WORKSPACES,
    ("POST", "/v1/projects/{}/archive"): _WORKSPACES,
    ("POST", "/v1/projects/{}/restore"): _WORKSPACES,
    ("POST", "/v1/projects/{}/artifacts"): _WORKSPACES,
    ("GET", "/v1/projects/{}/artifacts"): _WORKSPACES,
    ("POST", "/v1/projects/{}/artifacts/uploads"): _WORKSPACES,
    ("POST", "/v1/experiments/{}/artifacts"): _WORKSPACES,
    ("POST", "/v1/experiments/{}/artifacts/uploads"): _WORKSPACES,
    # Workspace surface (the switchable axis) + federated search.
    ("GET", "/v1/workspaces"): _WORKSPACES,
    ("GET", "/v1/workspaces/{}"): _WORKSPACES,
    ("GET", "/v1/workspaces/{}/files"): _WORKSPACES,
    ("POST", "/v1/workspaces/{}/files/uploads"): _WORKSPACES,
    ("POST", "/v1/search"): _WORKSPACES,
    # GitHub knowledge-connector management (READ status + ADMIN install/uninstall).
    ("GET", "/v1/integrations/github"): _CONNECTORS,
    ("POST", "/v1/integrations/github/installations"): _CONNECTORS,
    ("DELETE", "/v1/integrations/github/installations/{}"): _CONNECTORS,
}

_ALLOWED: dict[Op, str] = {**NOT_CLIENT_SURFACE, **PENDING}


def _fmt(ops) -> str:
    return "\n".join(f"  {method:6} {path}" for method, path in sorted(ops, key=lambda o: (o[1], o[0])))


def test_no_allowlist_entry_is_in_both_lists():
    """A route is either not-our-job or not-done-yet. Never both."""
    overlap = set(NOT_CLIENT_SURFACE) & set(PENDING)
    assert not overlap, f"listed as both permanent and pending:\n{_fmt(overlap)}"


def test_every_backend_operation_is_reachable_from_the_client():
    """The parity check itself: no unlisted backend route may be unreachable."""
    ops = schema_operations()
    reachable = set(client_call_sites())
    unreachable = set(ops) - reachable - set(_ALLOWED)
    assert not unreachable, (
        f"{len(unreachable)} backend operation(s) are unreachable from the client.\n\n"
        f"{_fmt(unreachable)}\n\n"
        "Wire each one up (sdk/client.py + cli/main.py), or add it to NOT_CLIENT_SURFACE "
        "(with a reason) / PENDING (with a tracking pointer) in this file."
    )


def test_allowlisted_operations_are_still_unreachable():
    """Anti-rot: implementing a route means deleting its allowlist entry.

    Without this, PENDING silently becomes a list of things we already did, and
    NOT_CLIENT_SURFACE stops describing the boundary it claims to describe.
    """
    reachable = set(client_call_sites())
    stale = {op: why for op, why in _ALLOWED.items() if op in reachable}
    assert not stale, (
        "these operations are allowlisted but ARE reachable — delete their entries:\n"
        + "\n".join(f"  {m:6} {p}\n      listed as: {why}" for (m, p), why in sorted(stale.items()))
    )


def test_allowlisted_operations_still_exist_in_the_schema():
    """Anti-rot: an entry for a route the backend dropped is a lie. Delete it."""
    ops = schema_operations()
    ghosts = set(_ALLOWED) - set(ops)
    assert not ghosts, (
        "allowlisted operations that the backend no longer declares — delete these entries "
        f"(or run `make regen` if the schema is stale):\n{_fmt(ghosts)}"
    )


def test_client_only_calls_operations_the_backend_declares():
    """The reverse drift: a client call to a route that does not exist.

    Catches typos and rot — a path that quietly 404s forever is worse than a
    compile error, because fail-open callers swallow it.
    """
    ops = schema_operations()
    sites = client_call_sites()
    phantom = {op: where for op, where in sites.items() if op not in ops}
    assert not phantom, (
        "the client calls operations the backend does not declare:\n"
        + "\n".join(
            f"  {m:6} {p}\n      called from: {', '.join(where)}"
            for (m, p), where in sorted(phantom.items())
        )
        + "\n\nRun `make regen` if the schema is stale; otherwise this call is dead code."
    )


# -- guards on the extractor itself -----------------------------------------
# The parity check is only as good as its path extraction. A silent regression here
# (e.g. f-strings stop resolving) would mark everything unreachable — or worse, mark
# the whole suite green by finding nothing to check.


def test_extractor_resolves_fstring_and_constant_paths():
    sites = client_call_sites()
    # f-string interpolation -> `{}` (sdk/run.py builds this from presign['artifact_id'])
    assert ("POST", "/v1/artifacts/{}/confirm") in sites
    # module-level constant (sdk/device.py's _START_PATH)
    assert ("POST", "/auth/device/code") in sites
    # plain literal
    assert ("GET", "/v1/me") in sites


def test_no_transport_call_builds_its_path_opaquely():
    """No API call may be invisible to the parity check.

    A `*transport` call whose path is assembled by `.format()`/`+`/`%`/`join` is a
    hole: the route it hits looks unreachable (noisy but safe), or — worse — a PENDING
    entry it satisfies never clears. Use a literal or f-string, or teach `_as_path`.
    """
    _, opaque = _scan()
    assert not opaque, "transport calls with an unreadable path:\n  " + "\n  ".join(opaque)


def _scan_source(tmp_path, source: str):
    """Run the extractor over a synthetic module instead of the real client."""
    (tmp_path / "fake_module.py").write_text(source)
    return _scan(src=tmp_path, root=tmp_path)


def test_extractor_reads_constants_fstrings_and_keyword_paths(tmp_path):
    found, opaque = _scan_source(tmp_path, '''
_CONST = "/auth/device/code"
_ANNOTATED: str = "/v1/annotated"

def calls(self, thing_id):
    self.transport.get("/v1/literal")
    self.transport.post(_CONST, {})
    self.transport.get(_ANNOTATED)
    self.transport.patch(f"/v1/things/{thing_id}/parts", {})
    self.transport.get(path="/v1/keyword")
    self.transport.request("DELETE", f"/v1/things/{thing_id}")
    self._client.write("POST", "/v1/written", {})
''')
    assert not opaque
    assert set(found) == {
        ("GET", "/v1/literal"),
        ("POST", "/auth/device/code"),
        ("GET", "/v1/annotated"),
        ("PATCH", "/v1/things/{}/parts"),
        ("GET", "/v1/keyword"),
        ("DELETE", "/v1/things/{}"),
        ("POST", "/v1/written"),
    }


def test_extractor_ignores_lookalikes_that_are_not_api_calls(tmp_path):
    """A dict keyed by paths, and a presigned absolute URL, are not operations."""
    found, opaque = _scan_source(tmp_path, '''
ROUTES = {"/v1/not-a-call": 1}

def calls(self, body, url, data):
    ROUTES.get("/v1/not-a-call")          # a plain dict, not a transport
    body.get("slug")                       # not a path at all
    self.transport.put_url(url, data)      # absolute R2 URL, no API path
    self.transport.get_url(url)
''')
    assert found == {}
    assert not opaque  # put_url/get_url are not path-taking verbs


def test_extractor_reports_a_transport_path_it_cannot_read(tmp_path):
    """The guard that keeps the known limits honest: an unreadable path is loud."""
    found, opaque = _scan_source(tmp_path, '''
def calls(self, thing_id):
    self.transport.get("/v1/things/{}/parts".format(thing_id))
    self.transport.post("/v1/" + thing_id, {})
''')
    assert found == {}
    assert len(opaque) == 2
    assert all("self.transport" in u for u in opaque)


def test_extractor_does_not_flag_generic_dispatchers(tmp_path):
    """`Client.write` and the spool replay forward a caller-supplied path. Their real
    routes are recorded at their own call sites, so they must not be reported."""
    found, opaque = _scan_source(tmp_path, '''
def write(self, method, path, body=None):
    return self.transport.request(method, path, json_body=body)

def flush(self, transport, entry):
    return transport.request(entry["method"], entry["path"])
''')
    assert found == {}
    assert not opaque


def test_method_first_requires_a_dispatcher_receiver(tmp_path):
    """`write`/`request` are ubiquitous method names — a method-string + path on a
    NON-dispatcher receiver (a logger, a buffer) must NOT register as a reachable
    route. Otherwise an unrelated `.write("POST", "/v1/x")` could silently satisfy —
    and thereby retire — a PENDING entry, with no test going red.
    """
    found, _ = _scan_source(tmp_path, '''
def calls(self, audit_log, buf):
    audit_log.write("POST", "/v1/runs/gc")   # a logger, not an HTTP client
    buf.write("GET", "/v1/tokens")            # a buffer
    self.write("POST", "/v1/real")            # Client.write — a real dispatch
    self._client.write("PATCH", "/v1/also-real")  # run helper — a real dispatch
''')
    assert set(found) == {("POST", "/v1/real"), ("PATCH", "/v1/also-real")}
    assert ("POST", "/v1/runs/gc") not in found
    assert ("GET", "/v1/tokens") not in found


def test_verb_call_on_a_bare_http_client_is_seen(tmp_path):
    """`client.get("/v1/me")` (mcp/server.py's httpx client) must register, so a route
    wired only through such a receiver still clears its PENDING entry."""
    found, _ = _scan_source(tmp_path, '''
def calls(client, some_dict):
    client.get("/v1/me")            # a real httpx client
    some_dict.get("not-a-path")     # a dict lookup, ignored (no leading /)
''')
    assert ("GET", "/v1/me") in found


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/v1/runs/{run_ref}/bundle", "/v1/runs/{}/bundle"),
        ("/v1/experiments/{experiment_id}/versions/{version}", "/v1/experiments/{}/versions/{}"),
        ("/v1/projects", "/v1/projects"),
    ],
)
def test_normalize_collapses_parameter_names(raw, expected):
    """Parameter naming is the backend's business; only position matters."""
    assert _normalize(raw) == expected
