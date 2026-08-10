"""The Probe Research SDK client core.

Two write paths, one core (per the SDK/CLI primitives sketch):
  * granular ``/v1`` calls for interactive / agent-driven capture (Anthrogen);
  * one-shot idempotent ``/ingest`` push for install-once passive capture (Osmosis).

Every method maps onto a real v4 endpoint (Probe Research v0.4.0.0 ingestion fold-in).
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import socket
import sys
import threading
import time
import uuid
import warnings
import weakref
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..models import (
    ArtifactVersionCreate,
    EdgeCreate,
    ExecutionRecordCreate,
    ExperimentVersionMint,
    IngestRunRequest,
    LatestScalarsRequest,
    MetricViewCreate,
    MetricViewPatch,
    MetricViewPreviewRequest,
    RunGroupCreate,
    RunGroupPatch,
    ScopedUploadRequest,
    UploadGcRequest,
    UploadRequest,
    WikiWrite,
)
from . import config as config_module
from . import errors
from .errors import CapabilityUnavailable
from .config import Settings, resolve
from .tags import canonical_tags
from .hashing import fingerprint
from .journal import Journal, run_ref_for_path

from .surface import Surface
from .transport import Page, Transport



def _view_spec(spec: Any) -> dict:
    """Normalize an expression-view spec through ``probe.expr``.

    An ``Expr``, a bare node dict, and a full ``{"expression": ...}`` mapping are
    all accepted and all validated. Imported lazily: ``expr`` pulls the generated
    models, and a client that never touches views should not pay for them.
    """
    from . import expr as expr_module

    return expr_module.spec(spec)


def _touch_run_lease(path: str) -> None:
    """Renew the auto-update lease for whichever run this write targets.

    Reuses ``run_ref_for_path`` -- the journal's own extractor -- rather than a
    second parser, so the run a write is attributed to for lease purposes is by
    construction the same one it is attributed to for barrier purposes.

    Never raises: instrumentation must not be able to break the write it rides on.
    """
    try:
        run_ref = run_ref_for_path(path)
        if not run_ref:
            return
        from probe.cli import run_lock

        run_lock.renew_lease_if_stale(run_ref)
    except Exception:  # noqa: BLE001 -- see docstring
        pass

class Anchor(str, Enum):
    """What an artifact hangs off.

    The database CHECKs that exactly one anchor is set, so this is a closed
    vocabulary, not a hint. Four of these are *artifacts*; workspace and shared are
    *files*, which is a different noun on the wire (see :meth:`Client.upload_file`).
    """

    RUN = "run"
    EXPERIMENT = "experiment"
    PROJECT = "project"
    WORKSPACE = "workspace"
    SHARED = "shared"


#: Anchors whose upload body is a ``ScopedUploadRequest``. That model is declared
#: ``extra="forbid"``, so a run-only field (``kind``, ``meta``, ``span_id``,
#: ``step_index``) sent to one of these is a 422 — silently ignored is NOT what
#: happens, which is why the client rejects it up front with a readable message.
_SCOPED_ANCHORS = frozenset(
    {Anchor.EXPERIMENT, Anchor.PROJECT, Anchor.WORKSPACE, Anchor.SHARED}
)

#: Anchors addressed as "files" rather than "artifacts": their identity is
#: (anchor, name) rather than (anchor, name, content_hash), so re-uploading a name
#: REPLACES it via a confirm-time swap instead of adding a second version. They also
#: have no metadata-only form — a file is its bytes.
_FILE_ANCHORS = frozenset({Anchor.WORKSPACE, Anchor.SHARED})

#: Iteration cap for the step-paged reads (metrics grouped/wide). The loop already
#: stops when the server reports the read exhausted; the cap only exists so a server
#: that keeps answering ``truncated`` with a non-advancing ``next_step`` cannot spin
#: a notebook forever. Hitting it returns honestly: ``truncated`` stays True and
#: ``next_step`` says where to resume.
_MAX_STEPPED_PAGES = 100



def _series_key(column: dict) -> tuple:
    """A wide-read column's series identity: (key, kind, canonical dimensions)."""
    return (
        column.get("key"),
        column.get("kind"),
        json.dumps(column.get("dimensions") or {}, sort_keys=True),
    )


def _merge_wide_page(merged: dict, page: dict) -> None:
    """Append one page of a wide read onto the merged result, in place.

    Columns are per-window: a series with no point inside a page's step range is
    absent from that page's ``columns``, so later pages can be wider (or narrower)
    than the first. Positions therefore cannot be trusted across pages — values are
    realigned by series identity, and a column new to the merge back-fills ``None``
    into the rows already collected, exactly as the server would have emitted had
    the whole range fit in one page."""
    if page.get("columns") == merged["columns"]:
        merged["rows"].extend(page.get("rows") or [])
        return
    positions = {_series_key(column): i for i, column in enumerate(merged["columns"])}
    for column in page.get("columns") or []:
        if _series_key(column) not in positions:
            positions[_series_key(column)] = len(merged["columns"])
            merged["columns"].append(column)
            for row in merged["rows"]:
                row["values"].append(None)
    width = len(merged["columns"])
    page_keys = [_series_key(column) for column in page.get("columns") or []]
    for row in page.get("rows") or []:
        values: list = [None] * width
        for series, value in zip(page_keys, row.get("values") or []):
            values[positions[series]] = value
        merged["rows"].append({**row, "values": values})


def _exactly(rows: list[dict], slug: str, *, strict: bool = False) -> dict | None:
    """The row whose slug actually MATCHES, or None.

    Never `rows[0]`. FastAPI silently drops a query parameter it does not
    declare, so a backend without the `?slug=` filter (an older engine, a
    rolled-back data plane, a self-hosted install) answers an unfiltered first
    page — and taking `rows[0]` there attaches the caller to a real, arbitrary,
    WRONG entity instead of erroring. That failure is worse than the
    get-or-create it replaced: get-or-create at least made an isolated new
    identity, where this appends your metrics to someone else's experiment.

    The membership check in `run()` cannot catch it either, because it reads its
    comparand from the same broken listing and so agrees with the bug. Verifying
    the slug here is what makes an unfiltered response degrade to "not found".
    """
    for row in rows or ():
        if row.get("slug") == slug:
            return row
    if strict and len(rows or ()) > 1:
        # A miss from a listing nobody filtered is not an absence. Only callers
        # deciding id-vs-slug ask for strict; get-or-create still wants "absent".
        raise errors.UnfilteredListing(
            f"this backend did not apply ?slug={slug!r} (returned {len(rows)} rows)"
        )
    return None


class Client:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        ingest_token: str | None = None,
        hmac_secret: str | None = None,
        settings: Settings | None = None,
        transport: Transport | None = None,
        fail_open: bool = True,
        journal: "Journal | None" = None,
        spool_dir: str | Path | None = None,
        async_writes: bool = False,
        auto_drain: bool = True,
        drain_interval: float | None = None,
        redact: "Callable[[dict], dict] | bool | None" = None,
        surface: str = Surface.SDK.value,
        client_headers: Mapping[str, str] | None = None,
    ):
        self.settings = settings or resolve(
            base_url=base_url,
            token=token,
            ingest_token=ingest_token,
            hmac_secret=hmac_secret,
        )
        # `surface` tags outbound requests for analytics attribution (cli/sdk/mcp).
        # Ignored when an already-built `transport` is supplied — it carries its own.
        if transport is not None and client_headers:
            raise ValueError(
                "client_headers configure the default Transport; pass them to "
                "a custom Transport directly"
            )
        self.transport = transport or Transport(
            self.settings,
            surface=surface,
            client_headers=client_headers,
        )
        self.fail_open = fail_open
        # One queue (eng review 2026-07-29, T1-C): the journal holds both
        # async-mode writes and the sync fail-open safety net that used to be
        # the spool. `spool_dir` keeps its name as the directory override.
        self.async_writes = async_writes
        if journal is not None and spool_dir is not None:
            raise ValueError("pass journal or spool_dir, not both")
        self.journal = journal or Journal(
            Path(spool_dir).expanduser() if spool_dir else None
        )
        if self.journal.context is None:
            self.journal.context = {
                "name": config_module.current_context_name() or None,
                "base_url": self.settings.base_url,
            }
        # Parity F1/F2 (docs/2026-08-04-outbox-miles-parity.md): async enqueue
        # wakes a delivery loop. Default (F1): kick the detached outbox worker
        # -- default-transport clients only, because the worker resolves its
        # own transport from config and can never replay an injected one.
        # With ``drain_interval`` (or PROBE_EXPORT_INTERVAL_SEC) set (F2): an
        # in-process exporter thread delivers through THIS client instead --
        # the fork-free path, and the one that works with any transport.
        if drain_interval is None and async_writes:
            raw = os.environ.get("PROBE_EXPORT_INTERVAL_SEC")
            if raw:
                try:
                    drain_interval = float(raw)
                except ValueError:
                    warnings.warn(
                        f"ignoring malformed PROBE_EXPORT_INTERVAL_SEC={raw!r}; "
                        "expected seconds as a float",
                        stacklevel=2,
                    )
        self._drain_interval = drain_interval if async_writes else None
        self._exporter = None
        self._exporter_lock = threading.Lock()
        # A worker fork only makes sense for default-transport clients, but a
        # FORCED kick (deferred finish, F3) must work from sync clients too.
        self._default_transport = transport is None
        # Producer accounting (parity F4). Long-lived writers get a
        # per-process identity; the CLI surface shares one per-host id -- a
        # training loop of thousands of `probe --async log` invocations is ONE
        # producer line, not thousands of registry files (sequences stay safe:
        # allocation reads the registry under the append lock).
        # Parity F5: scrub payloads at CAPTURE -- before bytes hit the journal
        # (commonly on shared storage) or the wire. True selects the standard
        # scrubber; a callable brings your own. Default None: untouched.
        if redact is True:
            from .redaction import default_scrub

            self._redact: Callable | None = default_scrub
        else:
            self._redact = redact or None
        self._seal_producer_on_close = False
        if async_writes:
            host = socket.gethostname()
            if surface == Surface.CLI.value:
                producer_id = f"cli:{host}"
            else:
                producer_id = f"{surface}:{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
                self._seal_producer_on_close = True
            # The per-host CLI id is right for a training loop and wrong for
            # concurrent CLI writers: several importers on one box collapse into
            # a single producer line and cannot be told apart afterwards. This
            # lets the caller name them (`PROBE_OUTBOX_PRODUCER_ID=import:shard-3`)
            # without every `probe --async log` minting a registry file.
            # Deliberately id-only: the seal-on-close decision stays with the
            # SURFACE, so naming a shared id does not make the first process to
            # exit mark the line closed under its still-live siblings.
            override = (os.environ.get("PROBE_OUTBOX_PRODUCER_ID") or "").strip()
            if override:
                producer_id = override
            try:
                self.journal.register_producer(producer_id, role=surface)
            except Exception:  # noqa: BLE001 -- accounting must never block writes
                pass
        self._auto_drain = (
            async_writes
            and auto_drain
            and transport is None
            and self._drain_interval is None
        )
        self._drainer_kick_interval = 1.0
        self._drainer_kicked_at = float("-inf")
        if (
            async_writes
            and transport is None
            and (auto_drain or self._drain_interval is not None)
            and not (self.settings.token or self.settings.ingest_token)
        ):
            # F7: queueing an op nothing can ever deliver fails hours later in
            # the drainer log, which is the worst place to learn it.
            raise errors.ValidationError(
                "async_writes needs deliverable credentials: run `probe login` "
                "or set PROBE_TOKEN -- or pass auto_drain=False to queue "
                "offline and deliver later via flush()/`probe outbox drain`"
            )
        self._events = None
        # Stop signals for every live run-heartbeat thread this client minted.
        # Weak so a finished beat (its Run collected, its thread exited) doesn't
        # accumulate here for the client's whole life.
        self._run_heartbeat_stops: weakref.WeakSet[threading.Event] = weakref.WeakSet()

    # -- lifecycle ----------------------------------------------------------
    def _register_run_heartbeat(self, stop: threading.Event) -> None:
        self._run_heartbeat_stops.add(stop)

    def close(self) -> None:
        # The exporter drains over this client's transport; join it first.
        if self._exporter is not None:
            self._exporter.close()
        if self._seal_producer_on_close:
            try:
                self.journal.seal_producer()
            except Exception:  # noqa: BLE001 -- accounting must never block close
                pass
        # Beats ride this client's transport; leaving them running would spin a
        # thread per unfinished run against a closed httpx client every interval.
        for stop in list(self._run_heartbeat_stops):
            stop.set()
        self.transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- fail-open write ----------------------------------------------------
    def write(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        strict: bool | None = None,
        durable: bool = True,
    ):
        """A data write. In ``async_writes`` mode it is journaled without ever
        touching the network (the outbox drainer delivers it); otherwise it is
        attempted and journaled on failure unless ``strict`` (or ``fail_open``
        is off). Returns the parsed response, or None if it was journaled.

        ``durable=False`` is the hardware rail's contract: attempt directly,
        RAISE on failure, never touch the journal — spool space belongs to
        training metrics, and the hw monitor's bounded in-memory buffer is
        that rail's only retry (drop-oldest; backfill repairs the gap)."""
        # Renew a DETACHED run's auto-update lease off its own traffic. This is
        # the single funnel every SDK write passes through, which is what makes
        # the renewal complete rather than sprinkled: a detached run
        # (heartbeat=False) has no process of ours to hold an flock, so without
        # this its 30-minute lease expires under a longer job and an upgrade
        # lands mid-run. No-ops for process-bound runs, which hold an flock and
        # have no lease file, and skips the write until the lease is half spent.
        _touch_run_lease(path)
        if self._redact is not None and body is not None:
            body = self._redact(body)
        if not durable:
            resp = self.transport.request(method, path, json_body=body)
            return resp.json() if resp.content else None
        if self.async_writes:
            self.journal.append_http(method, path, body)
            self._after_enqueue()
            return None
        strict = (not self.fail_open) if strict is None else strict
        try:
            resp = self.transport.request(method, path, json_body=body)
            return resp.json() if resp.content else None
        except errors.RosError:
            if strict:
                raise
            self.journal.append_http(method, path, body)
            return None

    def _after_enqueue(self) -> None:
        """Wake delivery for a just-journaled op (F1/F2). A DEAD exporter is
        not respawned: the only thing that kills one is an auth block, and
        respawning per write would retry rejected credentials forever -- the
        zombie-uploader pitfall. Re-login + `probe outbox retry` resumes."""
        if self._drain_interval is not None:
            exporter = self._exporter
            if exporter is None:
                with self._exporter_lock:
                    exporter = self._exporter
                    if exporter is None:
                        from .exporter import OutboxExporter

                        exporter = OutboxExporter(self, self._drain_interval)
                        self._exporter = exporter
            exporter.wake()
            return
        self._kick_drainer()

    def _kick_drainer(self, *, force: bool = False) -> None:
        """Wake the detached outbox worker (parity F1). Throttled: this runs
        on every async write and a training loop logs hundreds of points a
        second; ``maybe_spawn`` is O(1) but not free. Best-effort by design --
        ``finish()``/`probe run end` is the delivery barrier, this is latency.

        ``force`` (deferred finish, F3): skip the mode gate and the throttle
        -- a queued terminal status must not have its one kick swallowed --
        but never the transport gate; a worker cannot replay an injected one."""
        if not self._default_transport:
            return
        if not (self._auto_drain or force):
            return
        if not force:
            now = time.monotonic()
            if now - self._drainer_kicked_at < self._drainer_kick_interval:
                return
            self._drainer_kicked_at = now
        from . import outbox_worker

        try:
            outbox_worker.maybe_spawn(str(self.journal.dir))
        except Exception:  # noqa: BLE001 -- best-effort; run end is the barrier
            pass

    def _outbox_client_factory(self):
        """client_factory for ``journal.drain`` -- shared by ``flush()`` and
        the in-process exporter (F2).

        Ops pinned to THIS client's endpoint (or unpinned) replay over this
        client -- that keeps fake-transport tests and custom transports
        working. Ops pinned elsewhere resolve their own client from the named
        context, tokens fresh (5A): a context switch between enqueue and flush
        must never deliver to the wrong tenant.
        """

        def factory(context: dict | None):
            base = (context or {}).get("base_url")
            name = (context or {}).get("name")
            mine = (self.journal.context or {}).get("name")
            # BOTH the endpoint and the context name must match before an op
            # replays over this client's credential: tenants can share one API
            # URL, and an op pinned to another context must resolve its own
            # stored token (red team: base_url alone re-opened wrong-principal
            # replay through the flush path).
            if (not base or base == self.settings.base_url) and (
                name is None or name == mine
            ):
                return self
            return None  # journal.drain builds one from the pinned context

        return factory

    def flush(self) -> int:
        """Foreground-drain the journal; returns the delivered count."""
        from .journal import drain

        return drain(self.journal, client_factory=self._outbox_client_factory()).delivered

    # -- identity / auth ----------------------------------------------------
    def ensure_authenticated(self, *, interactive: bool | None = None) -> bool:
        """Make sure a user token exists, minting one via the browser device flow
        when a human can approve it.

        The interactive path runs only when stdin+stderr are TTYs and
        ``PROBE_AUTO_LOGIN`` is not ``0`` (or when ``interactive=True`` forces it).
        On success the token is persisted to the same config file ``probe login``
        writes, so the browser round-trip happens once per machine. Returns True
        when a token is available; False leaves the transport to raise its normal
        ``AuthError`` on first use (the crisp headless/CI behavior)."""
        if self.settings.token:
            return True
        if interactive is None:
            interactive = (
                os.environ.get("PROBE_AUTO_LOGIN", "1") != "0"
                and sys.stdin.isatty()
                and sys.stderr.isatty()
            )
        if not interactive:
            return False
        from .config import save_context
        from .device import DeviceLoginError, device_login

        print(
            f"no Probe token found — opening {self.settings.base_url} for browser approval…",
            file=sys.stderr,
        )

        def _show(prompt) -> None:
            print(f"  visit: {prompt.verification_uri_complete}", file=sys.stderr)
            print(f"  code:  {prompt.user_code}", file=sys.stderr)

        try:
            token = device_login(self.settings.base_url, on_prompt=_show)
        except DeviceLoginError as exc:
            warnings.warn(f"automatic device login failed: {exc}", stacklevel=2)
            return False
        save_context({"base_url": self.settings.base_url, "token": token})
        # Settings is shared with the transport; mutating it authenticates both.
        self.settings.token = token
        print("logged in — token saved for future runs", file=sys.stderr)
        return True

    def me(self) -> dict:
        # /v1/me (not the session-only /auth/me): resolves through the unified
        # door, so a `probe_pat` or OAuth token identifies its own tenant/role.
        return self.transport.get("/v1/me")

    def logout(self) -> None:
        """Revoke the calling token (CLI logout)."""
        self.transport.delete("/v1/tokens/current")

    # -- tokens -------------------------------------------------------------
    def list_tokens(self) -> list[dict]:
        """My live (unrevoked) tokens. Secrets are never returned — only
        ``token_prefix``, which is what a human matches against."""
        return self.transport.get("/v1/tokens")

    def create_token(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        open_browser: bool = True,
        on_prompt=None,
    ) -> dict:
        """Mint a named token through the browser device flow.

        NOT ``POST /v1/tokens``: that route is session-only by design, so it 403s
        for a token-authenticated CLI. The device flow reaches the same minter with
        a human approving in the browser, which is what the invariant "a leaked
        token must not be able to mint more tokens" is protecting.

        Returns ``TokenCreated``; ``["token"]`` is the plaintext secret and this is
        the only time it exists. Callers must show it once and never persist it.
        """
        from .device import device_authorize

        return device_authorize(
            self.settings.base_url,
            scopes=scopes,
            token_name=name,
            open_browser=open_browser,
            on_prompt=on_prompt,
        )

    def revoke_token(self, token_id: str) -> None:
        """Revoke a token by id. Your own: any writer. A teammate's: needs a
        browser session AND owner/admin, so it 403s from the CLI (by design)."""
        self.transport.delete(f"/v1/tokens/{token_id}")

    # -- client installations ----------------------------------------------
    def register_client_capabilities(
        self,
        *,
        auto_update: str,
        mcp: str,
        skills: str,
    ) -> dict:
        """Replace the complete allowlisted snapshot for this token's install.

        The token identifies the installation; no token or installation id is
        accepted in the body. Schema version 1 deliberately carries only three
        coarse states, never paths, commands, environment variables, or secrets.
        """
        return self.transport.put(
            "/v1/client-installations/current/capabilities",
            {
                "schema_version": 1,
                "auto_update": auto_update,
                "mcp": mcp,
                "skills": skills,
            },
        )

    def create_credential_attachment_grant(self, installation_id: str) -> dict:
        """Mint a short-lived installation join grant with the linked API PAT."""
        return self.transport.post(
            f"/v1/client-installations/{installation_id}/credential-attachment-grants"
        )

    def attach_current_credential(self, installation_id: str, *, grant: str) -> dict:
        """Consume an API-minted join grant using this read-only MCP PAT."""
        return self.transport.put(
            f"/v1/client-installations/{installation_id}/credentials/current",
            {"grant": grant},
        )

    def list_client_installations(self) -> dict:
        """Unified view of installs, API/MCP credentials, and capture devices."""
        return self.transport.get("/v1/client-installations")

    # -- workspaces ---------------------------------------------------------
    def list_workspaces(self) -> list[dict]:
        """Every workspace I can see, as a plain list.

        Deliberately NOT paginated: a workspace is one person's folder and there is
        exactly one per team member, so the result is bounded by team size. The server
        offers no cursor — adding one here would invent a contract.

        Server order is caller's-own first, then every other member's, alphabetical.
        Preserved as returned, since "mine first" is the useful default for a picker.
        """
        return self.transport.get("/v1/workspaces")

    def get_workspace(self, workspace_id: str) -> dict:
        return self.transport.get(f"/v1/workspaces/{workspace_id}")

    def rename_workspace(self, workspace_id: str, name: str) -> dict:
        """PATCH /v1/workspaces/{id}. ``name`` is the only user-editable field —
        slug and ownership are server-managed identity."""
        return self.transport.patch(f"/v1/workspaces/{workspace_id}", {"name": name})

    # -- projects -----------------------------------------------------------
    def create_project(
        self,
        slug: str,
        name: str | None = None,
        *,
        workspace_id: str | None = None,
        description: str | None = None,
        summary_markdown: str | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Create a project. Raises ``ConflictError`` if the slug is taken.

        Creation is always explicit: there is no get-or-create. A caller that
        does not know whether the project exists asks :meth:`resolve_project`
        first and decides."""
        body: dict[str, Any] = {"slug": slug, "name": name or slug}
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if description is not None:
            body["description"] = description
        if summary_markdown is not None:
            body["summary_markdown"] = summary_markdown
        if tags is not None:
            body["tags"] = tags
        if metadata is not None:
            body["metadata"] = metadata
        row = self.transport.post("/v1/projects", body)
        if tags is not None:
            self._verify_tags_written(tags, row, "POST /v1/projects")
        return row

    def resolve_project(self, slug: str, *, strict: bool = False) -> dict | None:
        """Look a project up by slug. ``None`` when it does not exist.

        `(customer_id, slug)` is UNIQUE, so ``?slug=`` returns 0 or 1 row and an
        empty result is an unambiguous "absent" rather than "not on this page".
        Nothing is hidden from this read: a deleted project is gone, and its slug
        is free again."""
        rows = self.transport.get("/v1/projects", params={"slug": slug})
        return _exactly(rows, slug, strict=strict)

    def ensure_project(self, slug: str, name: str | None = None, **kw) -> dict:
        """Get-or-create a project by slug. SDK-only; see :meth:`run`.

        :meth:`create_project` stays the explicit one, where a taken slug is an
        error. This is for callers who do not care whether it existed, only that
        it does now — but a slug that LOOKS like a typo of an existing one is
        refused rather than created. See :meth:`_refuse_near_miss`."""
        found = self.resolve_project(slug)
        if found is not None:
            return found
        self._guard_creatable("project", slug)
        try:
            return self.create_project(slug, name, **kw)
        except errors.ConflictError:
            # Lost a create race with a concurrent process. Get-or-create promises
            # the row exists afterwards, not that WE made it, so re-resolve rather
            # than surface a conflict the caller cannot act on. Re-raise if it is
            # still absent — then the 409 meant something else.
            #
            # NOT the swallow #87 removed. That one hid a TYPO behind a
            # successful-looking create; this resolves a race between two
            # processes asking for the same, correct, slug.
            found = self.resolve_project(slug)
            if found is None:
                raise
            return found

    def get_project(self, project_id: str) -> dict:
        return self.transport.get(f"/v1/projects/{project_id}")

    @staticmethod
    def _verify_tags_filter(requested: list[str], items: list, route: str) -> None:
        """A pre-0066 backend IGNORES the ``tags=`` filter and returns the
        unfiltered list — a confident wrong answer presented as filtered (the
        same failure shape as the 0054 ``project_id`` guard in list_runs).
        Every returned row must carry ALL requested tags; both sides compare in
        canonical form so a legacy un-normalized row can never false-positive.
        Refuse rather than mislabel. (An empty page proves nothing and passes.)"""
        want = set(canonical_tags(requested))
        for item in items:
            if want - set(canonical_tags(item.get("tags") or [])):
                raise errors.NotFoundError(
                    f"this research-os backend predates {route}?tags= (0066): it "
                    "ignored the filter and returned unfiltered rows. Upgrade "
                    "the backend to filter by tags."
                )

    @staticmethod
    def _verify_tags_written(sent: list[str], row: dict | None, route: str) -> None:
        """A pre-0066 backend silently DROPS ``tags`` from write bodies (unknown
        Pydantic fields are ignored) and answers 200 with the row unchanged — a
        confident no-op. The response must echo the canonical sent list; refuse
        rather than pretend (the write-side twin of ``_verify_tags_filter``).
        ``row=None`` (a spooled fail-open write) is unverifiable and passes."""
        if row is None:
            return
        if "tags" not in row or canonical_tags(row.get("tags") or []) != canonical_tags(sent):
            raise errors.NotFoundError(
                f"this research-os backend predates tags on {route} (0066): it "
                "ignored the tags write and returned the row unchanged. Upgrade "
                "the backend."
            )

    def list_projects(
        self,
        *,
        workspace_id: str | None = None,
        tags: list[str] | None = None,
        **params,
    ) -> Page:
        """``tags`` filters to projects carrying ALL of them (AND, 0066)."""
        query = dict(params)
        if workspace_id is not None:
            query["workspace_id"] = workspace_id
        # Send the canonical form (the server normalizes anyway): the guard
        # below then compares like against like.
        tags = canonical_tags(tags) if tags else None
        if tags:
            query["tags"] = tags
        page = self.transport.get_page("/v1/projects", params=query or None)
        if tags and page.items:
            self._verify_tags_filter(tags, page.items, "GET /v1/projects")
        return page

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        summary_markdown: str | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """PATCH /v1/projects/{id} for display fields and visible Markdown.

        ``summary_markdown`` is the human/agent-maintained document displayed
        below the live AI project summary. It is replaced wholesale; ``""``
        clears it. AI summary refreshes do not touch it.

        ``tags`` REPLACES the whole list ([] clears); the server normalizes to
        lowercase-kebab (CONTRACT.md "tags").

        Re-filing into another workspace is :meth:`move_project`, not a keyword here.
        Same route, but splitting the verbs keeps a reindex fan-out (see move_project)
        from being something you can trigger by mistyping an update.
        """
        body = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "summary_markdown": summary_markdown,
                "tags": tags,
                "metadata": metadata,
            }.items()
            if value is not None
        }
        if not body:
            raise ValueError("update_project needs at least one field to set")
        row = self.transport.patch(f"/v1/projects/{project_id}", body)
        if summary_markdown is not None:
            expected = summary_markdown if summary_markdown.strip() else ""
            if row.get("summary_markdown") != expected:
                raise errors.RosError(
                    "the research-os backend accepted the project summary write but "
                    "did not store it; upgrade the backend before relying on --summary"
                )
        if tags is not None:
            self._verify_tags_written(tags, row, "PATCH /v1/projects/{id}")
        return row

    def move_project(self, project_id: str, workspace_id: str) -> dict:
        """Re-file a project into another workspace.

        PATCH is the only backend door, but this is a much heavier operation than the
        verb suggests: when the workspace actually changes, the server reindexes every
        live descendant experiment and terminal run in the same transaction, because
        those documents denormalize ``workspace_id``. A no-op move (same workspace)
        skips the fan-out entirely.

        An unknown workspace is a 422, not a 404 — it is a rejected *value*, not a
        missing resource.
        """
        return self.transport.patch(
            f"/v1/projects/{project_id}", {"workspace_id": workspace_id}
        )

    def delete_project(self, project_id: str) -> None:
        """PERMANENTLY delete a project and everything under it.

        Experiments, runs, telemetry and files go with it, and the slug is freed.
        There is no archive and nothing to restore. 409 if a published experiment
        version pins something in the tree."""
        self.transport.delete(f"/v1/projects/{project_id}")

    # -- anchored artifacts / files -----------------------------------------
    # Every route below is written as its own literal call site rather than looked up
    # in a table. That is deliberate: the contract-parity guard resolves paths from the
    # AST, and a path built by `.format()` or a dict lookup is invisible to it — the
    # routes would read as unreachable and the guard would stop guarding them.

    def _presign_anchored(self, anchor: Anchor, anchor_id: str | None, body: dict) -> dict:
        if anchor is Anchor.RUN:
            return self.transport.post(f"/v1/runs/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.post(f"/v1/experiments/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.PROJECT:
            return self.transport.post(f"/v1/projects/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.WORKSPACE:
            return self.transport.post(f"/v1/workspaces/{anchor_id}/files/uploads", body)
        return self.transport.post("/v1/shared/files/uploads", body)

    def list_anchored(self, anchor: Anchor, anchor_id: str | None = None, **params) -> Any:
        """List the artifacts/files under one anchor."""
        query = params or None
        if anchor is Anchor.RUN:
            return self.transport.get(f"/v1/runs/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.get(f"/v1/experiments/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.PROJECT:
            return self.transport.get(f"/v1/projects/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.WORKSPACE:
            return self.transport.get(f"/v1/workspaces/{anchor_id}/files", params=query)
        return self.transport.get("/v1/shared/files", params=query)

    def create_anchored_reference(
        self, anchor: Anchor, anchor_id: str, body: dict
    ) -> dict:
        """Record a metadata-only (reference) artifact — no bytes uploaded.

        Only the three *artifact* anchors have this door. Workspace and shared are
        file anchors: a file is its bytes, so there is no reference-without-bytes form
        of one, and the backend declares no such route.
        """
        if anchor is Anchor.RUN:
            return self.transport.post(f"/v1/runs/{anchor_id}/artifacts", body)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.post(f"/v1/experiments/{anchor_id}/artifacts", body)
        if anchor is Anchor.PROJECT:
            return self.transport.post(f"/v1/projects/{anchor_id}/artifacts", body)
        raise ValueError(
            f"{anchor.value} is a file anchor — a file has no metadata-only form; "
            "upload bytes with upload_file() instead"
        )

    def upload_file(
        self,
        anchor: Anchor,
        anchor_id: str | None,
        name: str,
        path: str,
        *,
        content_type: str | None = None,
        kind: str | None = None,
        meta: dict | None = None,
        notes: str | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
    ) -> dict:
        """Upload a local file to any anchor: fingerprint -> presign -> PUT -> confirm.

        ``kind``/``meta``/``span_id``/``step_index`` are run-only. Passing them with a
        non-run anchor raises here rather than letting the server 422, because
        ``ScopedUploadRequest`` forbids extras and the resulting error does not say
        which field was the problem.

        ``notes`` is the exception and is accepted on EVERY anchor (0095). That is
        the point of it: `ScopedUploadRequest` forbidding extras meant a
        project/experiment upload had no way to describe itself at all, so agents
        concatenated the description onto ``name`` -- which is the file's relative
        posix path, so it broke the extension, the preview and the derived folder.

        Strict by design — no fail-open reference fallback. The fallback exists on
        :meth:`Run.log_artifact` so a training loop is never blocked by a flaky
        upload; an operator running ``probe artifact add`` wants to be told it failed.
        """
        anchor = Anchor(anchor)
        run_only = {
            "kind": kind,
            "meta": meta,
            "span_id": span_id,
            "step_index": step_index,
        }
        if anchor in _SCOPED_ANCHORS:
            offending = sorted(k for k, v in run_only.items() if v is not None)
            if offending:
                raise ValueError(
                    f"{', '.join(offending)} {'is' if len(offending) == 1 else 'are'} "
                    f"only accepted on a run anchor; the {anchor.value} upload contract "
                    "rejects extra fields (422)"
                )
        if anchor is not Anchor.SHARED and not anchor_id:
            raise ValueError(f"a {anchor.value} anchor needs an id")

        digest, size = fingerprint(path)
        return self.upload_fingerprinted(
            anchor,
            anchor_id,
            name,
            path,
            digest=digest,
            size=size,
            content_type=content_type,
            kind=kind,
            meta=meta,
            notes=notes,
            span_id=span_id,
            step_index=step_index,
        )

    def presign_upload(
        self,
        anchor: Anchor | str,
        anchor_id: str | None,
        name: str,
        *,
        digest: str,
        size: int,
        content_type: str | None = None,
        kind: str | None = None,
        meta: dict | None = None,
        notes: str | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
    ) -> dict:
        """Register upload intent: the server creates (or revives) the
        ``pending`` artifact row and returns a presigned PUT. Called by
        :meth:`upload_fingerprinted` on every attempt -- a remembered URL or
        row is never trusted (the reaper may have expired both) -- and by the
        async enqueue's capped best-effort ping (1A)."""
        anchor = Anchor(anchor)
        if anchor in _SCOPED_ANCHORS:
            req = ScopedUploadRequest(
                name=name,
                content_hash=digest,
                size_bytes=size,
                content_type=content_type,
                notes=notes,
            )
        else:
            req = UploadRequest(
                name=name,
                content_hash=digest,
                size_bytes=size,
                content_type=content_type,
                span_id=span_id,
                step_index=step_index,
                kind=kind,
                meta=meta or None,
                notes=notes,
            )
        return self._presign_anchored(
            anchor, anchor_id, req.model_dump(mode="json", exclude_none=True)
        )

    def upload_fingerprinted(
        self,
        anchor: Anchor | str,
        anchor_id: str | None,
        name: str,
        path: str,
        *,
        digest: str,
        size: int,
        content_type: str | None = None,
        kind: str | None = None,
        meta: dict | None = None,
        notes: str | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
    ) -> dict:
        """The presign -> PUT -> confirm core, for callers that already hold the
        fingerprint -- :meth:`upload_file` after hashing, and the outbox journal
        drain replaying a staged blob (whose hash was taken at enqueue or by the
        drainer, 11A). Phase-aware failure handling lives HERE, where the phase
        is known: a 404 on confirm after a ``have`` dedup is success (see below),
        and every attempt re-presigns rather than trusting a remembered URL.
        """
        anchor = Anchor(anchor)
        presign = self.presign_upload(
            anchor,
            anchor_id,
            name,
            digest=digest,
            size=size,
            content_type=content_type,
            kind=kind,
            meta=meta,
            notes=notes,
            span_id=span_id,
            step_index=step_index,
        )
        # `have` means the server already holds these bytes (content-addressed dedup),
        # so there is nothing to PUT. For a file anchor the swap to live also already
        # happened, in its own transaction.
        if not presign.get("have"):
            # Stream the file (an anchored artifact can be model weights); never read
            # it whole into memory. size is the fingerprinted length the presign signed.
            self.transport.put_file(
                presign["upload_url"],
                path,
                content_type=content_type or "application/octet-stream",
                headers=presign.get("upload_headers") or presign.get("headers"),
            )
        # Confirmed unconditionally, including on the `have` path: the server's confirm
        # returns an already-complete row unchanged (uploads_router.py `_confirm_pending_row`
        # is explicitly idempotent), so this costs one call and buys a single uniform
        # return shape — the stored artifact — across every anchor.
        try:
            return self.transport.post(
                f"/v1/artifacts/{presign['artifact_id']}/confirm", None
            )
        except errors.NotFoundError:
            if not presign.get("have"):
                raise
            # `have` means the bytes were already stored and, for a file anchor, already
            # swapped live. A concurrent replace of the same (anchor, name) can then
            # soft-delete this row before the confirm reads it. The upload succeeded;
            # failing here would report a phantom error for work the server did.
            #
            # Return an artifact-shaped row, NOT the presign: the presign carries
            # `upload_url` (a signed, bearer-equivalent write capability) and callers
            # print this — `probe shared add` sends it straight to stdout, where it
            # would land in CI logs. It also has no `id`/`status`, so every caller
            # relying on the documented uniform return shape would KeyError on exactly
            # this race.
            return {
                "id": presign["artifact_id"],
                "name": name,
                "content_hash": digest,
                "size_bytes": size,
                "status": "complete",
                "superseded": True,
            }

    # -- shared folder ------------------------------------------------------
    def share_workspace_file(self, artifact_id: str, *, replace: bool = False) -> dict:
        """Move a workspace file into the team's Shared folder.

        A MOVE, not a copy: ownership transfers and the search index is re-keyed in the
        same transaction, so the file leaves your workspace listing when it lands in
        Shared.

        A name collision in the destination is a 409 by default — the server never
        auto-supersedes someone else's file. ``replace=True`` atomically supersedes
        the prior one, which has to be asked for explicitly.
        """
        return self.transport.request(
            "POST",
            f"/v1/workspace-files/{artifact_id}/share",
            params={"replace": replace} if replace else None,
        ).json()

    def unshare_file(self, artifact_id: str, *, replace: bool = False) -> dict:
        """Move a Shared file back into the caller's personal workspace.

        Same collision rule as :meth:`share_workspace_file`, in the other direction.
        """
        return self.transport.request(
            "POST",
            f"/v1/shared/files/{artifact_id}/unshare",
            params={"replace": replace} if replace else None,
        ).json()

    def download_shared_file(self, artifact_id: str) -> dict:
        """Presigned download URL for a Shared file."""
        return self.transport.get(f"/v1/shared/files/{artifact_id}/download")

    def delete_shared_file(self, artifact_id: str) -> None:
        self.transport.delete(f"/v1/shared/files/{artifact_id}")

    def confirm_shared_file(self, artifact_id: str) -> dict:
        """The Shared folder's own confirm door. Equivalent to the generic
        ``/v1/artifacts/{id}/confirm``; both delegate to the same core."""
        return self.transport.post(f"/v1/shared/files/{artifact_id}/confirm", None)

    # -- experiments --------------------------------------------------------
    def create_experiment(
        self,
        slug: str,
        name: str | None = None,
        *,
        hypothesis: str | None = None,
        project_id: str,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create an experiment. Raises ``ConflictError`` if the slug is taken.

        The hypothesis is REQUIRED and is not synthesised. This used to accept
        ``None`` and compose a marked ``[auto]`` placeholder from ambient context,
        which then became permanent: an existing experiment keeps its own
        hypothesis first-write-wins, so nothing ever replaced the placeholder
        unless a human noticed and ran ``probe experiment set``. Making creation
        explicit means naming what you are testing at the moment you create it."""
        if not hypothesis:
            raise errors.ValidationError(
                f"an experiment needs a hypothesis: what do you expect {slug} to show?"
            )
        if not project_id:
            raise errors.ValidationError(
                "an experiment needs an explicit project_id; create or resolve "
                "the project first"
            )
        body: dict[str, Any] = {
            "slug": slug,
            "name": name or slug,
            "hypothesis": hypothesis,
            "project_id": project_id,
        }
        if description is not None:
            body["description"] = description
        if tags is not None:
            body["tags"] = tags
        return self.transport.post("/v1/experiments", body)

    def resolve_experiment(self, slug: str, *, strict: bool = False) -> dict | None:
        """Look an experiment up by slug. ``None`` when it does not exist.

        Experiment slugs are UNIQUE per TENANT, not per project, so this needs no
        project_id to disambiguate."""
        rows = self.transport.get("/v1/experiments", params={"slug": slug})
        return _exactly(rows, slug, strict=strict)

    def ensure_experiment(
        self,
        slug: str,
        name: str | None = None,
        *,
        hypothesis: str,
        project_id: str,
        **kw,
    ) -> dict:
        """Get-or-create an experiment by slug. SDK-only; see :meth:`run`.

        ``hypothesis`` is REQUIRED and keyword-only: reaching this method means
        creation is on the table, and an experiment is never created without one.
        It is NOT applied to an experiment that already exists — those are
        first-write-wins, so reopening never rewrites the hypothesis. Nothing is
        synthesised; the ``[auto]`` placeholder was permanent unless a human
        noticed it, which is why it is gone.

        A slug that resolves to nothing but looks like a typo of an existing one
        is REFUSED, not created — see :meth:`_refuse_near_miss`. Callers who want
        the strict three-outcome error for an absent slug use
        :meth:`resolve_or_raise` instead."""
        found = self.resolve_experiment(slug)
        if found is not None:
            return found
        self._guard_creatable("experiment", slug)
        try:
            return self.create_experiment(
                slug, name, hypothesis=hypothesis, project_id=project_id, **kw
            )
        except errors.ConflictError:
            # Lost a create race; see the note in ensure_project.
            found = self.resolve_experiment(slug)
            if found is None:
                raise
            return found

    def get_experiment(self, experiment_id: str) -> dict:
        return self.transport.get(f"/v1/experiments/{experiment_id}")

    def update_experiment(
        self,
        experiment_id: str,
        *,
        hypothesis: str | None = None,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        summary: dict | None = None,
    ) -> dict:
        """PATCH /v1/experiments/{id} — amend an experiment's hypothesis, name or
        description after creation. ``tags`` REPLACES the whole list ([] clears);
        the server normalizes to lowercase-kebab (CONTRACT.md "tags")."""
        body = {
            key: value
            for key, value in {
                "hypothesis": hypothesis,
                "name": name,
                "description": description,
                "tags": tags,
                "metadata": metadata,
                "summary": summary,
            }.items()
            if value is not None
        }
        if not body:
            raise ValueError("update_experiment needs at least one field to set")
        row = self.transport.patch(f"/v1/experiments/{experiment_id}", body)
        if tags is not None:
            self._verify_tags_written(tags, row, "PATCH /v1/experiments/{id}")
        return row

    def list_experiments(
        self,
        *,
        project_id: str | None = None,
        tags: list[str] | None = None,
        **params,
    ) -> Page:
        """``tags`` filters to experiments carrying ALL of them (AND, 0066)."""
        query = dict(params)
        if project_id is not None:
            query["project_id"] = project_id
        tags = canonical_tags(tags) if tags else None
        if tags:
            query["tags"] = tags
        page = self.transport.get_page("/v1/experiments", params=query or None)
        if tags and page.items:
            self._verify_tags_filter(tags, page.items, "GET /v1/experiments")
        return page

    def delete_experiment(self, experiment_id: str) -> None:
        """PERMANENTLY delete an experiment and its runs. Frees the slug.

        There is no archive and nothing to restore. 409 if a published experiment
        version outside this experiment pins something under it."""
        self.transport.delete(f"/v1/experiments/{experiment_id}")

    def experiment_edges(self, experiment_id: str) -> list[dict]:
        """Every lineage edge under an experiment (the run-level view is
        :meth:`run_edges`)."""
        return self.transport.get(f"/v1/experiments/{experiment_id}/edges")

    # -- run groups (sweeps / ensembles) ------------------------------------
    @staticmethod
    def _warn_if_notes_dropped(sent: str | None, row: dict, what: str) -> None:
        """Surface the silent drop when the backend predates research-os 0096.

        Neither RunPatch/RunCreate nor the group schemas forbid extra fields, so an
        older backend ACCEPTS `notes`, ignores it, and answers 2xx -- the caveat
        vanishes and the caller is told it succeeded. This is the 0094 hazard, and
        the check is free: create and PATCH both return the row, so nothing extra
        is fetched.

        WARN rather than raise, unlike `set_project_notes`. That call's only effect
        is the notes write, so failing it loses nothing. These calls have already
        created or mutated the entity by the time the response is in hand -- raising
        would leave a run created on the server and an exception in the caller's
        lap, which is worse than a dropped note.
        """
        if sent is None or "notes" in row:
            return
        warnings.warn(
            f"this research-os backend predates `notes` on {what} (0096): it "
            "accepted the field, ignored it, and answered 2xx, so the note was "
            "NOT stored. Upgrade the backend to >= 0.107.0.0.",
            stacklevel=3,
        )

    def create_group(
        self,
        experiment_id: str,
        name: str,
        *,
        kind: str = "group",
        spec: dict | None = None,
        notes: str | None = None,
    ) -> dict:
        """Create a run group under an experiment — coordination metadata for a
        sweep or ensemble; ``spec`` holds e.g. the search space.

        Pass the returned ``id`` to :meth:`create_run` as ``group_id`` to file a run
        under it. 409 if the name is taken within the experiment.

        ``notes`` (server 0096) is free text about the sweep itself — what varies,
        what it was testing, why it was abandoned. Put it HERE rather than in
        ``name``: the name is part of the group's uniqueness key within the
        experiment, so a description appended to it does not merely read badly, it
        changes the row's identity and mints a second group instead of colliding
        with the one it describes."""
        model = RunGroupCreate(name=name, kind=kind, spec=spec or {}, notes=notes)
        row = self.transport.post(
            f"/v1/experiments/{experiment_id}/groups",
            model.model_dump(mode="json", exclude_none=True),
        )
        self._warn_if_notes_dropped(notes, row, "run groups")
        return row

    def list_groups(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/groups")

    def get_group(self, group_id: str) -> dict:
        return self.transport.get(f"/v1/groups/{group_id}")

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        spec: dict | None = None,
        notes: str | None = None,
    ) -> dict:
        """Field-replace PATCH: only the fields you pass change.

        ``notes`` is the field most likely to be written here rather than at
        create — what a sweep was actually testing tends to be known after it has
        run. Omitting it leaves any existing note alone; passing ``""`` clears it."""
        model = RunGroupPatch(name=name, spec=spec, notes=notes)
        body = model.model_dump(mode="json", exclude_none=True)
        if not body:
            raise ValueError("update_group needs at least one of name/spec/notes")
        row = self.transport.patch(f"/v1/groups/{group_id}", body)
        self._warn_if_notes_dropped(notes, row, "run groups")
        return row

    # -- runs (create) ------------------------------------------------------
    @staticmethod
    def _run_create_body(
        name: str,
        *,
        description: str | None,
        notes: str | None,
        source: str,
        external_id: str | None,
        parent_run_id: str | None,
        parent_relation: str | None,
        group_id: str | None,
        config: dict | None,
        tags: list[str] | None,
        metadata: dict | None,
        labeled_point_budget: int | None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "source": source}
        # The run's own slug (server 0.110.0.0), stored in the `short_id` column.
        # Omitted = the server mints a petname, which stays the normal case. A
        # TAKEN one is a 409, never a silent substitution.
        if slug is not None:
            body["slug"] = slug
        if description is not None:
            body["description"] = description
        # Separate from `description` on purpose (server 0096): a description says
        # what the run IS, notes is the caveat a later reader needs ("suspect, the
        # dataloader was stale"). With one field the two compete.
        if notes is not None:
            body["notes"] = notes
        if external_id is not None:
            body["external_id"] = external_id
        if parent_run_id is not None:
            body["parent_run_id"] = parent_run_id
            body["parent_relation"] = parent_relation or "fork"
        if group_id is not None:
            body["group_id"] = group_id
        if config is not None:
            body["config"] = config
        if tags is not None:
            body["tags"] = tags
        if metadata is not None:
            body["metadata"] = metadata
        # Per-run labeled-point budget (server 0061): a run that will log more
        # per-sample points than the server default declares its plan up front.
        if labeled_point_budget is not None:
            body["labeled_point_budget"] = labeled_point_budget
        return body

    def _wrap_run(self, data: dict, *, heartbeat: bool) -> Run:
        run = Run(self, data)
        # A handle minted here is presumed to live and die with this process, so
        # it beats by default and the server's reaper can flip it to 'crashed'
        # when the process dies. Pass heartbeat=False when the run is DETACHED —
        # created here but executed and finished from somewhere else (CLI
        # `run start`, the miles exporter) — because beating briefly and then
        # going silent gets a legitimately-running run reaped.
        #
        # `heartbeat` also decides which tier of the auto-update run lock applies,
        # and it is exactly the right question: a run that lives and dies with
        # this process can hold an flock the kernel releases on death, while a
        # detached one has no process to hold anything and needs a renewable
        # lease. This is the single construction boundary, so every path that
        # opens a run — client.run(), probe.init(), a directly built handle —
        # is covered by it.
        run._hold_run_lock(process_bound=heartbeat)
        if heartbeat:
            run.start_heartbeat()
        return run

    def create_run(
        self,
        experiment_id: str,
        name: str,
        *,
        description: str | None = None,
        notes: str | None = None,
        source: str = "api",
        external_id: str | None = None,
        parent_run_id: str | None = None,
        parent_relation: str | None = None,
        group_id: str | None = None,
        config: dict | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        heartbeat: bool = True,
        labeled_point_budget: int | None = None,
        slug: str | None = None,
    ) -> Run:
        body = self._run_create_body(
            name,
            slug=slug,
            description=description,
            notes=notes,
            source=source,
            external_id=external_id,
            parent_run_id=parent_run_id,
            parent_relation=parent_relation,
            group_id=group_id,
            config=config,
            tags=tags,
            metadata=metadata,
            labeled_point_budget=labeled_point_budget,
        )
        # Literal call site: the tests/test_parity.py AST scan must see the route.
        data = self.transport.post(f"/v1/experiments/{experiment_id}/runs", body)
        self._verify_slug_written(slug, data)
        self._warn_if_notes_dropped(notes, data, "runs")
        return self._wrap_run(data, heartbeat=heartbeat)

    @staticmethod
    def _verify_slug_written(slug: str | None, row: dict) -> None:
        """A backend predating run slugs DROPS the field and names the run itself.

        Pydantic ignores an undeclared body field by default, so the create
        succeeds, returns 201, and hands back a random petname -- and the caller
        exits 0 believing it owns a handle it does not. Reading the stored value
        back is the only thing that tells the two apart.
        """
        if slug is None:
            return
        written = row.get("short_id")
        if written != slug:
            raise CapabilityUnavailable(
                f"this backend ignored the run slug {slug!r} (it stored "
                f"{written!r}); run slugs need research-os >= 0.110.0.0"
            )

    def create_project_run(
        self,
        project_id: str,
        name: str,
        *,
        description: str | None = None,
        notes: str | None = None,
        source: str = "api",
        external_id: str | None = None,
        parent_run_id: str | None = None,
        parent_relation: str | None = None,
        config: dict | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        heartbeat: bool = True,
        labeled_point_budget: int | None = None,
        slug: str | None = None,
    ) -> Run:
        """POST /v1/projects/{id}/runs — open a PROJECT-DIRECT run (W&B shape).

        The experiment level is optional grouping; a run opened here attaches
        straight to the project. No ``group_id``: run groups are
        experiment-anchored, so the backend rejects one on a direct run (422).
        """
        body = self._run_create_body(
            name,
            slug=slug,
            description=description,
            notes=notes,
            source=source,
            external_id=external_id,
            parent_run_id=parent_run_id,
            parent_relation=parent_relation,
            group_id=None,
            config=config,
            tags=tags,
            metadata=metadata,
            labeled_point_budget=labeled_point_budget,
        )
        try:
            # Literal call site: the tests/test_parity.py AST scan must see the route.
            data = self.transport.post(f"/v1/projects/{project_id}/runs", body)
        except errors.NotFoundError as exc:
            # A pre-0054 backend has no such route, and its route-level 404
            # ("Not Found") is indistinguishable from a missing project. The
            # handler's own 404 says "project not found"; anything else means
            # the backend predates the route — say so, and say what to do
            # (the browse/search degraded-backend standard).
            if "project" not in str(exc).lower():
                raise errors.NotFoundError(
                    "this research-os backend predates POST /v1/projects/{id}/runs "
                    "(project-direct runs, 0054). Upgrade the backend, or open "
                    "the run inside an experiment (--experiment / run(experiment=...)).",
                    status=exc.status,
                    detail=exc.detail,
                ) from exc
            raise
        self._verify_slug_written(slug, data)
        self._warn_if_notes_dropped(notes, data, "runs")
        return self._wrap_run(data, heartbeat=heartbeat)

    def run(
        self,
        *,
        experiment: str | None = None,
        hypothesis: str | None = None,
        name: str | None = None,
        project: str | None = None,
        experiment_name: str | None = None,
        on_conflict: str = "auto",
        hw: bool | None = None,
        snapshot: bool | None = None,
        **run_kw,
    ) -> Run:
        """Open a run — full resolution/creation/``on_conflict`` semantics in
        :meth:`_run_impl` (unchanged). ``hw=True`` (or ``PROBE_HW=1``) also
        starts the opt-in hardware collector when this process is the
        node-local leader. Best-effort by contract: a broken collector never
        touches the run (docs/2026-08-05-hw-metrics-design.md). ``snapshot``
        controls the open-time auto-snapshot: ``None`` follows
        ``PROBE_AUTO_SNAPSHOT`` (default on), ``False`` skips, ``True`` forces."""
        handle = self._run_impl(
            experiment=experiment,
            hypothesis=hypothesis,
            name=name,
            project=project,
            experiment_name=experiment_name,
            on_conflict=on_conflict,
            snapshot=snapshot,
            **run_kw,
        )
        from probe.hw import integration as hw_integration

        handle._hw_monitor = hw_integration.maybe_start(self, handle, hw)
        return handle

    def _run_impl(
        self,
        *,
        experiment: str | None = None,
        hypothesis: str | None = None,
        name: str | None = None,
        project: str | None = None,
        experiment_name: str | None = None,
        on_conflict: str = "auto",
        snapshot: bool | None = None,
        **run_kw,
    ) -> Run:
        """Open a run inside an experiment — or straight under a project.

        With ``experiment``, the run opens inside that experiment (and ``project``,
        if also given, is a cross-check). With only ``project`` (W&B shape), the run
        attaches PROJECT-DIRECT — no experiment at all.

        Resolution is strict by default: an unknown slug raises, naming the closest
        existing ones.

        ``hypothesis=`` is the ONE opt-in to creation. Pass it and an absent
        experiment (and its project) is created; omit it and nothing is ever
        created. Creation is gated this way because a hypothesis is the thing you
        can only write when you know what you are testing — so the cost of a new
        experiment is one sentence, and an accident cannot pay it.

        That opt-in is SDK-only on purpose. Here the slug is written once in a file
        and code-reviewed; on the CLI it is hand-typed on every invocation, which is
        where typos come from — so ``probe run start`` cannot create at all. Use
        ``probe experiment create`` / ``probe project create`` there.

        For work with no hypothesis, the honest home is a project-direct run, not an
        experiment named after whatever directory you happened to be in.

        ``on_conflict`` decides what a duplicate ``external_id`` means.
        ``"auto"`` (the default) reads the incumbent's state: dead
        (failed/crashed/canceled) → RESUME it in place — same run, same curve,
        recovery on the record; completed or still alive → the 409 stands,
        because repeating a good run or hijacking a live one must be
        deliberate. ``"resume"`` demands the resume and errors when the
        incumbent is not dead. ``"supersede"`` treats the collision as a
        from-scratch RETRY: a fresh run opens as ``external_id-rN`` with
        ``parent_relation="retry"`` pointing at the incumbent, and a dead
        incumbent is tagged ``superseded`` so nobody trusts its partial
        numbers (a completed one is left unmarked — a repeat is not a
        correction). ``"error"`` keeps the bare 409. The two recovery policies
        split on step continuity: resume continues the curve past the crash
        point (checkpointed relaunch), supersede replays it from step 0.

        ``snapshot`` — None (default) follows ``PROBE_AUTO_SNAPSHOT`` (on
        unless set to "0"); False skips the auto-capture; True forces it
        regardless of the env var. A run opened here and then driven through
        ``run.execute()`` snapshots twice; the second is cheap (same tree →
        same execution record via server dedupe, code-bytes deduped by the
        presign have-check) and overwrites the launch block with the more
        specific child argv — intended. This dedupe is genuine even in a git
        repo: the execution record hashes the code MANIFEST only, never the
        per-snapshot shadow commit (design doc D1), so two snapshots of an
        unchanged tree always land on the same content_hash regardless of how
        many shadow refs were minted along the way."""
        if on_conflict not in ("auto", "error", "supersede", "resume"):
            raise errors.ValidationError(
                f"on_conflict={on_conflict!r} is not a policy. Use \"auto\" "
                "(resume a dead incumbent, else 409), \"resume\" (demand it), "
                "\"supersede\" (retry lineage under a fresh -rN external_id), "
                "or \"error\" (the bare 409)."
            )
        if not experiment and not project:
            raise errors.ValidationError(
                "run() needs an experiment slug — or a project slug for a "
                "project-direct run. It used to fall back to the git repo or "
                "script name, which silently created an experiment named after "
                "whatever directory you happened to be in."
            )
        if experiment_name is not None and hypothesis is None:
            raise errors.ValidationError(
                "experiment_name only titles an experiment run() CREATES, and "
                "creation needs a hypothesis. Pass hypothesis=, or rename an "
                "existing experiment with update_experiment()."
            )
        if hypothesis is not None and not experiment:
            raise errors.ValidationError(
                "hypothesis= creates an experiment, so it needs an experiment "
                "slug. A project-direct run has no experiment to hold one."
            )
        if hypothesis is not None and not project:
            raise errors.ValidationError(
                "creating an experiment needs an explicit project slug. Pass "
                "project= after creating the project first."
            )
        if hypothesis is not None and not hypothesis.strip():
            # `hypothesis=args.hypothesis or ""` is an ordinary way to get here.
            # Creation gates on `is not None` while create_experiment gates on
            # falsiness, so an empty string used to unlock the create path far
            # enough to commit a PROJECT before failing on the experiment.
            raise errors.ValidationError(
                "hypothesis= is empty. Creating an experiment needs one that says "
                "something: what do you expect this to show? Pass a real "
                "hypothesis, or drop the argument to open an existing experiment."
            )
        self.ensure_authenticated()
        name = name or f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        # Refuse an uncreatable experiment slug BEFORE any parent is committed.
        # ensure_project runs first below, so without this a refused experiment
        # leaves a brand-new orphan project behind — the exact stray identity the
        # refusal exists to prevent.
        if experiment and hypothesis is not None and self.resolve_experiment(experiment) is None:
            self._guard_creatable("experiment", experiment)
        project_id = None
        if project:
            # The project follows the experiment: creation is unlocked only by a
            # hypothesis, so a project-direct run (which cannot carry one) always
            # resolves strictly.
            project_id = (
                self.ensure_project(project)["id"]
                if hypothesis is not None
                else self.resolve_or_raise("project", project)["id"]
            )
        if not experiment:
            # Project-direct (0054): the run attaches straight to the project.
            if run_kw.get("group_id") is not None:
                raise errors.ValidationError(
                    "a group needs an experiment: run groups are "
                    "experiment-anchored, so a project-direct run cannot join "
                    "one. Name the experiment or drop the group."
                )
            run_kw.pop("group_id", None)
            handle = self._create_run_with_policy(
                lambda kw, nm: self.create_project_run(project_id, nm, **kw),
                run_kw, name, on_conflict,
                experiment_id=None, project_id=project_id,
            )
            self._maybe_auto_snapshot(handle, snapshot)
            return handle
        if hypothesis is not None:
            exp = self.ensure_experiment(
                experiment,
                experiment_name or experiment,
                hypothesis=hypothesis,
                project_id=project_id,
            )
        else:
            exp = self.resolve_or_raise("experiment", experiment, project_id=project_id)
        # Naming a project for an experiment that already lives somewhere else is a
        # real mistake rather than a no-op: `project` would otherwise silently do
        # nothing, which is its own quiet wrong answer.
        if project_id and exp.get("project_id") not in (None, project_id):
            raise errors.ValidationError(
                f"experiment {experiment!r} is not in project {project!r}. "
                "Drop the project argument, or name the one it actually belongs to."
            )
        handle = self._create_run_with_policy(
            lambda kw, nm: self.create_run(exp["id"], nm, **kw),
            run_kw, name, on_conflict,
            experiment_id=exp["id"], project_id=None,
        )
        self._maybe_auto_snapshot(handle, snapshot)
        return handle

    @staticmethod
    def _maybe_auto_snapshot(handle: Run, snapshot: bool | None) -> None:
        """Auto-snapshot hook for ``run()`` (design D3). ``snapshot=None``
        follows ``PROBE_AUTO_SNAPSHOT`` (default on); best-effort like
        ``execute()``'s hook -- failure warns rather than losing the run.
        Capture is never a gate, opt-in or otherwise (maintainer decision
        2026-08-06)."""
        auto = snapshot if snapshot is not None else (
            os.environ.get("PROBE_AUTO_SNAPSHOT", "1") != "0"
        )
        if auto:
            try:
                handle.snapshot()  # in-process: THIS interpreter is the env
            except Exception as exc:
                warnings.warn(
                    f"auto-snapshot failed; run continues uncaptured: {exc}",
                    stacklevel=2,
                )

    #: Statuses whose numbers cannot be trusted; superseding one marks it.
    _DEAD_RUN_STATUSES = frozenset({"failed", "crashed", "canceled"})

    def _create_run_with_policy(
        self,
        create,
        run_kw: dict,
        name: str,
        on_conflict: str,
        *,
        experiment_id: str | None,
        project_id: str | None,
        max_attempts: int = 5,
    ) -> Run:
        """``create()``, except an external_id 409 is resolved by policy.

        RESUME reopens the incumbent in place — same identity, same curve —
        which is only honest when the relaunch continues from a checkpoint;
        the step guard on the returned handle enforces that. SUPERSEDE opens a
        NEW run as ``external_id-rN`` carrying ``parent_relation="retry"``
        plus ``retry_of``/``retry_attempt`` foreign keys, for the relaunch
        that replays from step 0 (appending that into a half-dead run would
        splice two executions into one curve — the W&B ``resume="allow"``
        failure mode). Either way a dead incumbent's partial record survives:
        resume continues it, supersede tags it ``superseded``."""
        try:
            return create(run_kw, name)
        except errors.ConflictError:
            base = run_kw.get("external_id")
            if on_conflict == "error" or base is None:
                raise
        page = self.list_runs(experiment_id=experiment_id, project_id=project_id)
        old = next((r for r in page.items if r.get("external_id") == base), None)
        if old is None:
            # The 409 is real but the incumbent is not on the first page (or
            # belongs to another source). Acting on it blind would resume or
            # supersede the wrong thing, so the original conflict stands.
            raise errors.ConflictError(
                f"run {base!r} conflicts but its incumbent is not visible "
                "here — resolve the collision by hand",
            )
        status = old.get("status")
        if on_conflict in ("auto", "resume"):
            if status in self._DEAD_RUN_STATUSES:
                return self._resume_run(old, run_kw)
            if status == "completed":
                raise errors.ConflictError(
                    f"run {base!r} already completed — resuming would append "
                    "onto a good record. Repeat it deliberately with "
                    'on_conflict="supersede".'
                )
            raise errors.ConflictError(
                f"run {base!r} is {status} and its writer may still be alive "
                "— refusing to hijack an open run. If it is truly dead, wait "
                "for the reaper to mark it crashed, or supersede explicitly."
            )
        for n in range(2, max_attempts + 2):
            retry_id = f"{base}-r{n}"
            retry_kw = dict(
                run_kw,
                external_id=retry_id,
                parent_run_id=old["id"],
                parent_relation="retry",
            )
            try:
                run = create(retry_kw, retry_id if name == base else name)
                break
            except errors.ConflictError:
                continue
        else:
            raise errors.ConflictError(
                f"no free retry slot for {base!r} after {max_attempts} "
                "attempts — the run has been superseded that many times "
                "already, which is worth a look before retrying again",
            )
        # link() is the sanctioned foreign-keys surface (create_run does not
        # take them); per-key new-wins, so this cannot clobber anything.
        run.link(retry_of=old["id"], retry_attempt=n)
        if old.get("status") in self._DEAD_RUN_STATUSES:
            tags = sorted({*(old.get("tags") or []), "superseded"})
            self.write("PATCH", f"/v1/runs/{old['id']}", {"tags": tags})
        return run

    def _resume_run(self, old: dict, run_kw: dict) -> Run:
        """Reopen a dead incumbent and hand back a live handle on it.

        The session id minted here is the writer fingerprint the reopen
        registers (research-os#364): the engine's write epoch bumps, so a
        zombie predecessor still logging can be told apart from this process.
        The receipt's ``last_step`` arms the handle's resume guard. Snapshot
        again after this returns — each attempt's code+env is its own
        execution record."""
        session_id = str(uuid.uuid4())
        receipt = self.reopen_run(old["id"], session_id=session_id)
        row = receipt.get("run") or receipt
        return self.attach_run(
            row,
            heartbeat=run_kw.get("heartbeat", True),
            session_id=session_id,
            resume_from_step=receipt.get("last_step"),
        )

    def reopen_run(self, run_id: str, *, session_id: str) -> dict:
        """``POST /v1/runs/{id}/reopen`` — flip a dead run back to running.

        Returns the reopen receipt: the updated row plus ``write_epoch`` and
        ``last_step``. The caller just resolved the run, so a 404 here is the
        ROUTE missing, not the run — a backend that predates reopen
        (research-os#364) — and is translated into the actionable message
        rather than passed through as \"run not found\"."""
        try:
            return self.transport.post(
                f"/v1/runs/{run_id}/reopen", {"session_id": session_id}
            )
        except errors.NotFoundError as exc:
            raise errors.NotFoundError(
                "this backend predates run reopen (research-os#364): upgrade "
                'the server, or relaunch with on_conflict="supersede"'
            ) from exc

    def attach_run(
        self,
        run: str | dict,
        *,
        heartbeat: bool = True,
        session_id: str | None = None,
        resume_from_step: int | None = None,
    ) -> Run:
        """A live handle on an EXISTING run row (or id) in this process.

        The counterpart of create, through the same construction boundary
        (`_wrap_run`), so the heartbeat and auto-update-lock story is
        identical. ``resume_from_step`` arms the monotonic-step guard and
        floors the auto-step counters, so a resumed process cannot re-log
        steps the first execution already wrote."""
        data = self.get_run(run) if isinstance(run, str) else run
        handle = self._wrap_run(data, heartbeat=heartbeat)
        if session_id is not None:
            handle.session_id = session_id
        if resume_from_step is not None:
            from probe.hw.integration import SUSPECT_RESUME_FLOOR

            if resume_from_step >= SUSPECT_RESUME_FLOOR:
                # A last_step in hardware's epoch range means the server
                # computed the receipt WITHOUT the #364 hardware exclusion.
                # Arming would refuse every training step — fail open with a
                # warning instead (warn-never-gate).
                warnings.warn(
                    f"resume receipt last_step={resume_from_step} is in the "
                    "hardware rail's epoch range — this server predates the "
                    "hardware exclusion (research-os#364); the resume step "
                    "guard is disabled for this attach",
                    stacklevel=2,
                )
            else:
                handle.arm_resume_guard(resume_from_step)
        return handle

    def resolve_or_raise(self, kind: str, slug: str, *, project_id: str | None = None) -> dict:
        """Resolve a slug or raise the error that says what to do about it.

        Two outcomes: present (return it) or absent (create it, with near misses
        named). There used to be a third — ARCHIVED, where lookup said "missing"
        and create said "already exists" about the same slug — but archiving is
        gone, so a deleted slug is genuinely free. Both `run()` and the CLI go
        through here so the same failure cannot exit 1 from one surface and 2
        from the other."""
        resolve = self.resolve_project if kind == "project" else self.resolve_experiment
        found = resolve(slug)
        if found is not None:
            return found
        listing = (
            self.list_projects(limit=200).items
            if kind == "project"
            else self.list_experiments(project_id=project_id, limit=200).items
        )
        raise self._no_such(kind, slug, listing)

    @staticmethod
    def _near(slug: str, existing: Iterable[dict]) -> list[str]:
        """Existing slugs close enough to ``slug`` to be what the caller meant.

        A mistyped slug is by definition CLOSE to a real one; a genuinely new
        name is not. That asymmetry is what lets one list serve both a hard
        error (when we cannot proceed) and a warning (when we can)."""
        slugs = [row["slug"] for row in existing if row.get("slug")]
        return difflib.get_close_matches(slug, slugs, n=3, cutoff=0.6)

    def _all_slugs(self, kind: str) -> list[dict]:
        """Every row of `kind`, following the cursor.

        The near-miss guard leans on seeing the WHOLE namespace. `limit=200` is
        the schema maximum, and page ordering is unspecified, so stopping at one
        page means the guard silently stops firing past 200 rows — and it is the
        older slugs that drop out of view, which are exactly the ones a typo is
        likely to be a near-miss of. `analysis.compare()` follows the cursor for
        the same reason."""
        rows: list[dict] = []
        cursor = None
        lister = self.list_projects if kind == "project" else self.list_experiments
        while True:
            # Tenant-wide on purpose: experiment slugs are unique per TENANT, not
            # per project (see resolve_experiment). Scoping this to a project
            # would let a typo of an experiment filed elsewhere sail through.
            page = lister(limit=200, cursor=cursor)
            rows.extend(page.items)
            cursor = page.next_cursor
            if not cursor:
                return rows

    def _guard_creatable(self, kind: str, slug: str) -> None:
        """Raise unless `slug` is safe to CREATE (near-miss guard).

        Split out of ensure_* so `run()` can run it BEFORE it commits a parent:
        otherwise a refused experiment leaves a brand-new project behind, which is
        precisely the orphan identity this whole guard exists to prevent."""
        self._refuse_near_miss(kind, slug, self._all_slugs(kind))

    def _refuse_near_miss(self, kind: str, slug: str, existing: Iterable[dict]) -> None:
        """Refuse to CREATE a slug that looks like a typo of an existing one.

        A warning was the obvious first answer and it is the wrong one. The two
        places this fires — a detached ``probe run start`` process and a training
        loop — are both places nobody reads warnings, and this repo has already
        run that experiment: the ``[auto]`` hypothesis shipped WITH a fix
        affordance (``probe experiment set``) and it went unused every time. An
        invisible guard is not a guard.

        Refusing costs a caller who genuinely wants a near-identical name one
        explicit ``create_experiment``. Warning costs everyone else a second
        identity that every later comparison reads as a different thing.

        The 0.6 cutoff keeps short version-y names usable — ``v1`` vs ``v2``
        scores 0.5 and passes — so what this catches is long, near-identical
        slugs, which are far likelier to be typos than intent."""
        # An EXACT match is not a typo, it is the row itself — reachable when the
        # listing sees a slug the resolve did not (a create race, or a stale read
        # replica). Refusing there would turn "someone just made this" into a hard
        # error about a name that is already correct.
        near = [n for n in self._near(slug, existing) if n != slug]
        if not near:
            return
        cli_hint = (
            ' --hypothesis "..." --project PROJECT_SLUG'
            if kind == "experiment"
            else ""
        )
        sdk_hint = (
            ", hypothesis=..., project_id=PROJECT_ID"
            if kind == "experiment"
            else ""
        )
        raise errors.ValidationError(
            f"refusing to create {kind} {slug!r}: it is a near-miss of "
            f"{', '.join(repr(n) for n in near)}. If you meant the existing one, "
            f"use that slug. If this really is new, create it explicitly — "
            f"`client.create_{kind}({slug!r}{sdk_hint})` or "
            f"`probe {kind} create {shlex.quote(slug)}{cli_hint}`."
        )

    @staticmethod
    def _no_such(kind: str, slug: str, existing: Iterable[dict]) -> errors.NotFoundError:
        """A not-found that names near misses, so a typo says what you meant.

        A mistyped slug is by definition CLOSE to a real one; a genuinely new
        name is not. Suggesting neighbours turns the common case from "what do
        you mean it does not exist" into an obvious one-character fix."""
        near = Client._near(slug, existing)
        hint = f" Did you mean: {', '.join(near)}?" if near else ""
        # The suggested command has to actually RUN. `experiment create` requires
        # --hypothesis, so omitting it would print a command that fails on the one
        # field this whole change exists to force. The slug is quoted because it
        # arrives from the caller and this string is presented as copy-pasteable.
        extra = (
            ' --hypothesis "..." --project PROJECT_SLUG'
            if kind == "experiment"
            else ""
        )
        return errors.NotFoundError(
            f"no {kind} with slug {slug!r}.{hint} "
            f"Create it with `probe {kind} create {shlex.quote(slug)}{extra}` "
            "if it is genuinely new."
        )

    def heartbeat_run(self, run_id: str) -> dict:
        """``POST /v1/runs/{id}/heartbeat``: report that this run is still alive.

        Liveness cannot be inferred from `status`: it is a plain column, so a run
        whose process dies without a final PATCH stays 'running' forever and any
        "what is active" count decays into noise. Call this periodically while a
        run executes and the server's reaper marks anything that stops beating
        as 'crashed'.

        Beating is what makes a run REAPABLE -- a run that has never beat is
        never reaped -- so adopting this is safe and gradual, but a run that
        beats ONCE and then stops will eventually be marked crashed. Either beat
        for the run's whole life or not at all.

        SDK handles beat automatically: ``create_run`` starts a background
        thread (see :meth:`Run.start_heartbeat`) unless called with
        ``heartbeat=False``. Call this method directly only for runs managed
        outside the SDK — e.g. a workflow driver renewing the lease on a run it
        opened with ``probe run start``.

        Only a 'running' run is stamped; a late beat racing a normal completion
        is a no-op rather than an error.
        """
        return self.transport.post(f"/v1/runs/{run_id}/heartbeat", None, idempotent=True)

    # -- runs (read) --------------------------------------------------------
    def get_run(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}")

    def update_run(
        self,
        run_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """PATCH /v1/runs/{id} — amend a run's title, description or notes.

        ``notes`` (server 0096) is the door that matters most for that field: a
        run's caveat is nearly always learned after the run finished. It is NOT a
        second description — a description says what the run is, notes says what a
        later reader should distrust about it, and writing the caveat into
        ``description`` means destroying the description to keep it.

        Omitting a field leaves it alone; passing ``""`` for ``notes`` clears it."""
        body = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "notes": notes,
            }.items()
            if value is not None
        }
        if not body:
            raise ValueError("update_run needs at least one of name/description/notes")
        row = self.transport.patch(f"/v1/runs/{run_id}", body)
        self._warn_if_notes_dropped(notes, row, "runs")
        return row

    def run_bundle(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/bundle")

    def run_reproduce(self, run_id: str) -> dict:
        """GET /v1/runs/{id}/reproduce — the server-assembled reproduction record:
        execution record, launch context, restore command, code snapshot,
        inputs-decision (content inlined when small), notes, lockfiles, lineage
        edges, per-span env refs, and a completeness verdict. This is a thin
        passthrough on purpose — the backend is the one place that reads every
        piece together (research-os app/read_models/reproduce.py). A run captured
        before capture-core answers 200 with a degraded body, never a 404."""
        return self.transport.get(f"/v1/runs/{run_id}/reproduce")

    def experiment_reproduce(self, experiment_id: str, *, version: int | None = None) -> dict:
        """GET /v1/experiments/{id}/reproduce — per-run reproduction summaries (a
        map, not N full assemblies; each summary carries a `reproduce_url` for
        drill-down). `version` pins against a minted experiment_versions manifest;
        omitted reads live rows."""
        params = {"version": version} if version is not None else None
        return self.transport.get(f"/v1/experiments/{experiment_id}/reproduce", params=params)

    def run_lineage(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/lineage")

    def run_metrics(
        self,
        run_id: str,
        *,
        key: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Raw metric points for a run. :meth:`run_series` is the summarized view;
        :meth:`query_series` is the multi-run comparison."""
        params = {k: v for k, v in {"key": key, "kind": kind, "limit": limit}.items() if v is not None}
        return self.transport.get(f"/v1/runs/{run_id}/metrics", params=params or None)

    def delete_series(
        self,
        run_id: str,
        *,
        key: str,
        kind: str = "model",
        dimensions: dict | None = None,
    ) -> None:
        """Delete a DERIVED series and its points — the counterpart to pushing one.

        Derived only: a logged series is the run's captured record, cannot be
        recomputed, and has no undo, so the server refuses it with a 409.

        Identity is (kind, key, dimensions), the same triple a write uses.
        `dimensions` pins ONE variant; omitting it addresses the dimension-less
        series, not every variant of the key."""
        params = {"kind": kind, "key": key}
        if dimensions is not None:
            params["dimensions"] = json.dumps(dimensions)
        self.transport.request("DELETE", f"/v1/runs/{run_id}/series", params=params)

    def run_series(self, run_id: str) -> list[dict]:
        """Per-series summary for a run (key/kind/dimensions + first/last/min/max)."""
        return self.transport.get(f"/v1/runs/{run_id}/series")

    # -- coordinate reads (below-run coordinates, research-os 0059-0062) -----
    def get_metrics_grouped(
        self,
        run_id: str,
        key: str,
        *,
        kind: str | None = None,
        agg: str | None = None,
        by: list[str] | None = None,
        where: dict[str, Any] | None = None,
        step_bucket: int | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        max_rows: int | None = None,
    ) -> dict:
        """Server-side reduce/group over one metric's stepped points (0059).

        ``by`` names coordinate axes to split on (sent comma-joined; one cell per
        combination of axis values); ``where`` is a coord filter dict (sent
        JSON-encoded, matched by type-faithful containment). ``agg`` is one of
        mean|sum|min|max|count — OPTIONAL since server 0062: omitted resolves to
        the key's DECLARED reduce fn (see :meth:`Run.log`'s ``agg``), else mean;
        conflicting declarations are a 422. An unknown ``by``/``where`` axis or a
        kind-ambiguous key is a 422, never a silent empty reduction.

        Paging is followed here: a truncated page's ``next_step`` is fed back as
        ``step_from`` until the reduction is exhausted, so one call answers the
        whole range. ``max_rows`` bounds the TOTAL cells returned — when it cuts
        the read short (or ``_MAX_STEPPED_PAGES`` does), the result says so:
        ``truncated`` is True and ``next_step`` is where to resume."""
        merged: dict | None = None
        remaining = max_rows
        for _ in range(_MAX_STEPPED_PAGES):
            params = {
                param: value
                for param, value in {
                    "key": key,
                    "kind": kind,
                    "agg": agg,
                    "by": ",".join(by) if by else None,
                    "where": json.dumps(where) if where is not None else None,
                    "step_bucket": step_bucket,
                    "step_from": step_from,
                    "step_to": step_to,
                    "max_rows": remaining,
                }.items()
                if value is not None
            }
            page = self.transport.get(
                f"/v1/runs/{run_id}/metrics/grouped", params=params
            )
            if merged is None:
                merged = page
            else:
                merged["groups"].extend(page.get("groups") or [])
                merged["truncated"] = page.get("truncated", False)
                merged["next_step"] = page.get("next_step")
            if remaining is not None:
                remaining -= len(page.get("groups") or ())
                if remaining <= 0:
                    break
            if not page.get("truncated") or page.get("next_step") is None:
                break
            step_from = page["next_step"]
        return merged

    def get_metrics_wide(
        self,
        run_id: str,
        *,
        key: list[str] | None = None,
        kind: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        max_rows: int | None = None,
    ) -> dict:
        """Step x metric table for a run — the DataFrame pivot, aligned by step.

        Same paging treatment as :meth:`get_metrics_grouped`: ``next_step`` is
        followed until the table is exhausted (rows realigned by series identity,
        since a page's columns cover only its own step window), ``max_rows``
        bounds the TOTAL step rows, and a short read reports ``truncated`` +
        ``next_step``. ``key`` narrows to those metric keys (repeated query
        param, matching the route's array parameter)."""
        merged: dict | None = None
        remaining = max_rows
        for _ in range(_MAX_STEPPED_PAGES):
            params = {
                param: value
                for param, value in {
                    "key": key or None,
                    "kind": kind,
                    "step_from": step_from,
                    "step_to": step_to,
                    "max_rows": remaining,
                }.items()
                if value is not None
            }
            page = self.transport.get(
                f"/v1/runs/{run_id}/metrics/wide", params=params or None
            )
            if merged is None:
                merged = page
            else:
                _merge_wide_page(merged, page)
                merged["truncated"] = page.get("truncated", False)
                merged["next_step"] = page.get("next_step")
            if remaining is not None:
                remaining -= len(page.get("rows") or ())
                if remaining <= 0:
                    break
            if not page.get("truncated") or page.get("next_step") is None:
                break
            step_from = page["next_step"]
        return merged

    def export_metric_points(
        self,
        run_id: str,
        *,
        key: str | None = None,
        kind: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        after_id: int | None = None,
        limit: int | None = None,
    ):
        """Lossless raw-point export: every point exactly once, labels included,
        no downsampling. A GENERATOR — the ``after_id`` keyset paging is followed
        transparently, so callers just iterate; ``limit`` is the page size of the
        walk, not a total bound. Pass ``after_id`` to resume a previous walk from
        its last point id."""
        while True:
            params = {
                param: value
                for param, value in {
                    "key": key,
                    "kind": kind,
                    "step_from": step_from,
                    "step_to": step_to,
                    "after_id": after_id,
                    "limit": limit,
                }.items()
                if value is not None
            }
            page = self.transport.get(
                f"/v1/runs/{run_id}/metrics/export", params=params or None
            )
            yield from page.get("points") or ()
            next_after_id = page.get("next_after_id")
            # None means the last currently visible page. The monotonic check is
            # the loop's own guard: a cursor that fails to advance would replay
            # the same page forever, and an infinite generator that yields
            # duplicates is worse than stopping at the point already delivered.
            if next_after_id is None or (after_id is not None and next_after_id <= after_id):
                return
            after_id = next_after_id

    def list_run_coordinates(self, run_id: str) -> list[dict]:
        """The run's coordinate catalog (0060): every non-empty coordinate any
        fact has landed on, with which fact tables have it — enumeration for
        split/overlay pickers without scanning points/spans/artifacts. Bounded by
        the series cap's cardinality arithmetic, so no pagination."""
        return self.transport.get(f"/v1/runs/{run_id}/coordinates")

    # -- expression views (read-time computed panels, research-os 0088) ------
    def create_view(self, run_id: str, name: str, spec: Any) -> dict:
        """Save an expression view on a run — a formula over series the run has
        already logged, evaluated at READ time (no points are stored).

        ``spec`` is a :class:`probe.expr.Expr`, a node dict, or a full
        ``{"expression": ...}`` mapping; all three normalize through
        :func:`probe.expr.spec`, which validates before anything reaches the
        wire. Names are unique per run among live views.

        Works on a COMPLETED run: a view reads the catalog, it does not append to
        the run's history. Nothing about finishing a run closes this door."""
        body = MetricViewCreate(name=name, spec=_view_spec(spec))
        return self.transport.post(
            f"/v1/runs/{run_id}/views", body.model_dump(mode="json", exclude_none=True)
        )

    def list_views(self, run_id: str) -> list[dict]:
        """Every live view on a run, with its spec and provenance."""
        return self.transport.get(f"/v1/runs/{run_id}/views")

    def update_view(self, view_id: str, *, name: str | None = None, spec: Any = None) -> dict:
        """Rename a view and/or replace its expression. Omitted fields are left
        alone — this is a PATCH, not a whole-row write."""
        body: dict = {}
        if name is not None:
            body["name"] = name
        if spec is not None:
            body["spec"] = _view_spec(spec)
        if not body:
            raise ValueError("update_view needs a name or a spec to change")
        return self.transport.patch(
            f"/v1/views/{view_id}", MetricViewPatch(**body).model_dump(mode="json", exclude_none=True)
        )

    def delete_view(self, view_id: str) -> None:
        """Soft-delete a view. The series it read are untouched."""
        self.transport.delete(f"/v1/views/{view_id}")

    def view_data(
        self,
        run_id: str,
        view_id: str,
        *,
        step_from: int | None = None,
        step_to: int | None = None,
        max_points: int | None = None,
    ) -> dict:
        """Evaluate a saved view and return its curve.

        Read the envelope, not just ``points``: ``missing_inputs`` names series
        the expression referenced that the run has none of, ``dropped_nonfinite``
        counts steps whose result was NaN/inf (a divide-by-zero), and
        ``truncated`` says the input scan hit its bound before the range ended.
        An empty ``points`` with a populated ``missing_inputs`` is a typo in the
        spec, not a run with no data."""
        params = {
            param: value
            for param, value in {
                "step_from": step_from,
                "step_to": step_to,
                "max_points": max_points,
            }.items()
            if value is not None
        }
        return self.transport.get(
            f"/v1/runs/{run_id}/views/{view_id}/data", params=params or None
        )

    def preview_view(
        self,
        run_id: str,
        spec: Any,
        *,
        step_from: int | None = None,
        step_to: int | None = None,
        max_points: int | None = None,
    ) -> dict:
        """Evaluate a spec WITHOUT saving it — same envelope as :meth:`view_data`.

        The check to run before :meth:`create_view`: a spec that names a series
        the run never logged comes back with ``missing_inputs`` here, instead of
        being saved as a panel that renders empty for everyone."""
        body = MetricViewPreviewRequest(
            spec=_view_spec(spec),
            step_from=step_from,
            step_to=step_to,
            **({"max_points": max_points} if max_points is not None else {}),
        )
        return self.transport.post(
            f"/v1/runs/{run_id}/views/preview",
            body.model_dump(mode="json", exclude_none=True),
        )

    def run_spans(
        self,
        run_id: str,
        *,
        span_type: str | None = None,
        parent_span_id: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Read a run's trajectory spans back (the write path is ``Run.span``)."""
        params = {
            k: v
            for k, v in {
                "span_type": span_type,
                "parent_span_id": parent_span_id,
                "step_from": step_from,
                "step_to": step_to,
                "limit": limit,
            }.items()
            if v is not None
        }
        return self.transport.get(f"/v1/runs/{run_id}/spans", params=params or None)

    def get_span(self, span_id: str) -> dict:
        return self.transport.get(f"/v1/spans/{span_id}")

    # -- lifecycle (permanent delete) ---------------------------------------
    def delete_run(self, run_id: str) -> None:
        """PERMANENTLY delete a run and its telemetry. Frees its natural key.

        Spans, metrics, artifacts and lineage go with it; DB rows only, R2 blobs
        are not touched (deferred, backend-side). There is nothing to restore.
        404 if the run does not exist."""
        self.transport.delete(f"/v1/runs/{run_id}")

    def presign_download(self, artifact_id: str) -> str:
        """Presigned GET URL for an artifact's blob (``POST /v1/artifacts/{id}/download``).

        The one home for this route literal: ``download_artifact*`` and the callers
        that need the raw doc in memory (``trial expand``, ``asset materialize``) all
        route through here, so the parity guard sees one reachable call site."""
        return self.transport.post(f"/v1/artifacts/{artifact_id}/download", None)["download_url"]

    def download_artifact(self, artifact_id: str) -> bytes:
        """Fetch an artifact's bytes into memory. Use :meth:`download_artifact_to`
        for anything large -- this holds the whole blob at once."""
        return self.transport.get_url(self.presign_download(artifact_id))

    def download_artifact_to(self, artifact_id: str, dest: str) -> dict:
        """Stream an artifact's blob to ``dest`` without buffering it in memory.

        Returns ``{artifact_id, dest, size_bytes, sha256}`` -- ``sha256`` is computed
        over the bytes as they land, so the caller can check it against the
        ``content_hash`` from a listing to prove the round trip (metadata match is not
        blob existence). On a mid-stream failure the partial file is removed rather
        than left behind as a truncated blob masquerading as the artifact -- the old
        in-memory path buffered before writing, so it never wrote a partial, and this
        preserves that guarantee."""
        url = self.presign_download(artifact_id)
        ok = False
        try:
            size, digest = self.transport.download_to(url, dest)
            ok = True
        finally:
            if not ok:
                Path(dest).unlink(missing_ok=True)
        return {"artifact_id": artifact_id, "dest": str(dest), "size_bytes": size, "sha256": digest}

    # -- artifact versions (an artifact's content history) ------------------
    def list_artifact_versions(self, artifact_id: str) -> list[dict]:
        """The artifact's version chain, newest first.

        An artifact is a named thing in a container; this is the chain of immutable,
        content-addressed versions behind that name. Immutability lives on the version,
        never the artifact, so renames and moves do not break a pin."""
        return self.transport.get(f"/v1/artifacts/{artifact_id}/versions")

    def create_artifact_version(
        self,
        artifact_id: str,
        *,
        from_artifact_id: str | None = None,
        uri: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        content_type: str | None = None,
        label: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Append the next version. Exactly one source: promote an existing artifact
        (``from_artifact_id``, zero-copy -- pins its hash + uri + size, the R2 object is
        shared, never re-uploaded) or name a pointer directly (``uri``).

        Re-sending content identical to the artifact's current live content is a no-op
        that returns the existing version, so this is safe to retry."""
        if bool(from_artifact_id) == bool(uri):
            raise ValueError(
                "create_artifact_version needs exactly one of from_artifact_id or uri"
            )
        model = ArtifactVersionCreate(
            from_artifact_id=from_artifact_id,
            uri=uri,
            content_hash=content_hash,
            size_bytes=size_bytes,
            content_type=content_type,
            label=label,
            meta=meta,
        )
        return self.transport.post(
            f"/v1/artifacts/{artifact_id}/versions",
            model.model_dump(mode="json", exclude_none=True),
        )

    def presign_version_download(self, artifact_id: str, version: int) -> str:
        """Presigned GET for one version's bytes -- the pin-resolution path.

        The one home for this route literal, mirroring :meth:`presign_download`, so the
        parity guard sees a single reachable call site. A force-deleted version raises
        410 WITH its deletion metadata, so a manifest can tell "deliberately destroyed"
        apart from "never existed"."""
        return self.transport.post(
            f"/v1/artifacts/{artifact_id}/versions/{version}/download", None
        )["download_url"]

    def download_artifact_version(self, artifact_id: str, version: int) -> bytes:
        """Fetch one version's bytes into memory. Use
        :meth:`download_artifact_version_to` for anything large -- this holds the whole
        blob at once."""
        return self.transport.get_url(self.presign_version_download(artifact_id, version))

    def download_artifact_version_to(self, artifact_id: str, version: int, dest: str) -> dict:
        """Stream one version's bytes to ``dest``, hashing as they land. Same partial-file
        guarantee as :meth:`download_artifact_to`: a mid-stream failure removes the file
        rather than leaving a truncated blob that looks like the artifact."""
        url = self.presign_version_download(artifact_id, version)
        ok = False
        try:
            size, digest = self.transport.download_to(url, dest)
            ok = True
        finally:
            if not ok:
                Path(dest).unlink(missing_ok=True)
        return {
            "artifact_id": artifact_id,
            "version": version,
            "dest": str(dest),
            "size_bytes": size,
            "sha256": digest,
        }

    def artifact_pin_impact(self, artifact_id: str) -> dict:
        """Which projects -- and which experiments within them -- pin this artifact's
        versions. What a delete confirmation should show a human before destroying
        something reproducible: not a count, but the work that would break."""
        return self.transport.get(f"/v1/artifacts/{artifact_id}/pin-impact")

    def delete_artifact(self, artifact_id: str) -> None:
        """Delete an artifact row."""
        self.transport.delete(f"/v1/artifacts/{artifact_id}")

    def gc_uploads(self, older_than: str) -> dict:
        """Sweep abandoned (never-confirmed) artifact uploads older than
        ``older_than``. Only ever touches pending rows; confirmed artifacts are
        untouched."""
        model = UploadGcRequest(older_than=older_than)
        return self.transport.post("/v1/artifacts/uploads/gc", model.model_dump(mode="json"))

    def check_run(self, run_id: str, *, verify: bool = False) -> dict:
        """Assess capture completeness from the bounded run bundle.

        Three verdicts, and the distinction is the point:

        * ``incomplete`` -- something is absent or provably unrecoverable.
        * ``unverified`` -- nothing is obviously absent. The default. It does NOT
          mean the run can be rebuilt.
        * ``complete`` -- earned only under ``verify=True``, by resolving the
          recorded code reference against its remote.

        This used to answer ``complete`` for the first case's near-miss: a run
        whose code_snapshot artifact existed and pointed at a commit that lived
        nowhere. Seventeen runs read as captured for a week on the strength of a
        row being present. Counting rows is not the same as proving retrieval, so
        the cheap path no longer claims a word it has not earned.

        ``verify`` costs one depth-1 fetch per DISTINCT (remote, commit) -- it is
        memoized, so auditing a project's runs is a few network calls rather than
        one per run. Never called during a run: it cannot slow training or upload.

        ``advisories`` reports gaps that are surfaced but never flip the verdict:
        judgment calls a human makes on purpose (no ``notes``, no recorded
        ``inputs_decision``) and legacy gaps (a run captured before capture-core
        has no ``launch`` block at all; a launch block that recorded its own
        capture errors). ``missing`` remains the only input to ``state`` -- a
        launch block that EXISTS but is missing a slot (process/runtime/
        determinism) is a genuine capture failure and lands in ``missing``, not
        ``advisories``.
        """
        bundle = self.run_bundle(run_id)
        run = bundle.get("run", bundle)
        artifacts = bundle.get("artifacts", [])
        metadata = run.get("metadata") or {}
        missing: list[str] = []
        # env_ref (execution record) is the launch-capture signal (fold #7). On the
        # ingest path it is run.env_ref; on the interactive path it is metadata.env_ref.
        if not (run.get("env_ref") or metadata.get("env_ref")):
            missing.append("execution_record")
        if not any(item.get("kind") == "code_snapshot" for item in artifacts):
            missing.append("code_snapshot_artifact")
        # A reference recorded because a managed upload FAILED (fold #16 fail-open) is a
        # real capture gap: its bytes never reached R2. An INTENTIONAL path reference (a
        # shared-volume checkpoint the agent resolves locally) is NOT -- it names bytes
        # that exist, just off-platform. Distinguish by meta.upload, not uri presence:
        # both now carry a file:// uri, so the old `not uri` test would both miss the
        # failure and false-flag every intentional reference.
        failed_uploads = [
            item.get("id") or item.get("name")
            for item in artifacts
            if item.get("is_reference") and (item.get("meta") or {}).get("upload") == "failed"
        ]
        if failed_uploads:
            missing.append("portable_artifact_bytes")

        # Free: the manifest summary already rode in on the artifact's meta, so
        # this costs a dict lookup. A file classified as needing upload whose
        # bytes nobody stored is unrecoverable exactly like a dead reference --
        # and this is the failure mode per-file capture INTRODUCED, so leaving it
        # unchecked would repeat the original mistake in a new place.
        snapshot_meta: dict = next(
            (
                (item.get("meta") or {})
                for item in artifacts
                if item.get("kind") == "code_snapshot"
            ),
            {},
        )
        pending = snapshot_meta.get("n_pending_upload")
        if isinstance(pending, int) and pending > 0:
            missing.append("pending_code_bytes")

        advisories: list[str] = []
        launch = metadata.get("launch") or {}
        if not launch:
            # Pre-capture-core run: honest advisory, not a verdict flip --
            # otherwise every historical run reads incomplete and the exit-2
            # gate becomes noise during migration.
            advisories.append("launch_context")
        else:
            for slot in ("process", "runtime", "determinism"):
                if not launch.get(slot):
                    missing.append(f"launch_{slot}")
            if launch.get("errors"):
                advisories.append("launch_errors")
        n_lockfiles = snapshot_meta.get("n_lockfiles")
        if isinstance(n_lockfiles, int) and n_lockfiles == 0:
            advisories.append("no_lockfiles")
        if not any(a.get("kind") == "inputs_decision" for a in artifacts):
            advisories.append("inputs_decision")
        if not run.get("notes") and not any(a.get("kind") == "note" for a in artifacts):
            advisories.append("notes")

        verified = None
        if verify and not missing:
            from . import snapshot as _snapshot

            base_commit = snapshot_meta.get("base_commit")
            remote = snapshot_meta.get("remote")
            if base_commit and remote:
                verified = _snapshot.commit_on_remote(str(remote), str(base_commit))
                if not verified:
                    missing.append("unresolvable_code_reference")
            else:
                # Pre-0.26.3 runs recorded no manifest, so there is nothing to
                # resolve against. Absence of evidence; say so rather than
                # inventing a pass or a failure.
                verified = False

        if missing:
            state = "incomplete"
        elif verify and verified:
            state = "complete"
        else:
            state = "unverified"
        return {
            "run_id": run_id,
            "state": state,
            "missing": missing,
            "local_only_artifacts": failed_uploads,
            "verified_code_reference": verified,
            "advisories": advisories,
        }

    # -- lineage edges (fold #2) -------------------------------------------
    def add_edge(
        self,
        *,
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        meta: dict | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """POST /v1/edges. Closed vocab for types (run/artifact/asset_version) and
        relation (consumes/produces/evaluates_on/...); the generated EdgeCreate enforces it."""
        model = EdgeCreate(
            source_type=source_type,
            source_id=source_id,
            relation=relation,
            target_type=target_type,
            target_id=target_id,
            meta=meta or {},
        )
        return self.write(
            "POST", "/v1/edges", model.model_dump(mode="json", exclude_none=True), strict=strict
        )

    def run_edges(self, run_id: str) -> list[dict]:
        return self.transport.get(f"/v1/runs/{run_id}/edges")

    # -- execution records (fold #7) ---------------------------------------
    def execution_record(
        self,
        *,
        code: dict | None = None,
        deps: dict | None = None,
        hardware: dict | None = None,
        settings: dict | None = None,
        paths: dict | None = None,
    ) -> dict:
        """POST /v1/execution-records (content-addressed, idempotent). Returns
        {content_hash, ...}."""
        model = ExecutionRecordCreate(
            code=code or {},
            deps=deps or {},
            hardware=hardware or {},
            settings=settings or {},
            paths=paths or {},
        )
        return self.transport.post(
            "/v1/execution-records", model.model_dump(mode="json"), idempotent=True
        )

    def get_execution_record(self, content_hash: str) -> dict:
        return self.transport.get(f"/v1/execution-records/{content_hash}")

    # -- experiment versions (fold #6) -------------------------------------
    def experiment_version(
        self,
        experiment_id: str,
        *,
        label: str | None = None,
        as_of: str | None = None,
        exclude_run_ids: list[str] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """POST /v1/experiments/{id}/versions - mint an immutable launch-time manifest
        (a snapshot of the experiment's runs). This replaces the removed run-level
        `promote`; Probe Research rejected promotion tiers."""
        model = ExperimentVersionMint(
            label=label, as_of=as_of, exclude_run_ids=exclude_run_ids or []
        )
        return self.write(
            "POST",
            f"/v1/experiments/{experiment_id}/versions",
            model.model_dump(mode="json", exclude_none=True),
            strict=strict,
        )

    def list_experiment_versions(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/versions")

    def get_experiment_version(self, experiment_id: str, version: int | str) -> dict:
        return self.transport.get(f"/v1/experiments/{experiment_id}/versions/{version}")

    def list_runs(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        direct: bool = False,
        tags: list[str] | None = None,
        **params,
    ) -> Page:
        """``project_id`` returns ALL the project's runs — project-direct AND
        experiment-attached (0054); ``direct=True`` narrows to experiment-less
        runs only."""
        query = dict(params)
        if experiment_id is not None:
            query["experiment_id"] = experiment_id
        if project_id is not None:
            query["project_id"] = project_id
        if direct:
            query["direct"] = "true"
        tags = canonical_tags(tags) if tags else None
        if tags:
            query["tags"] = tags
        page = self.transport.get_page("/v1/runs", params=query or None)
        if tags and page.items:
            # Same refuse-rather-than-mislabel contract as the 0054 guard below.
            self._verify_tags_filter(tags, page.items, "GET /v1/runs")
        # A pre-0054 backend IGNORES unknown query params and returns the
        # unscoped list — a confident wrong answer presented as project-scoped.
        # Its rows also predate the project_id field, which is how we can tell:
        # refuse rather than mislabel. (An empty page proves nothing and passes.)
        if (
            (project_id is not None or direct)
            and page.items
            and "project_id" not in page.items[0]
        ):
            raise errors.NotFoundError(
                "this research-os backend predates GET /v1/runs?project_id= "
                "(0054): it ignored the filter and returned unscoped runs. "
                "Upgrade the backend before relying on project-scoped listings."
            )
        return page

    def list_run_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        name: str | None = None,
        scope: str | None = None,
    ) -> list[dict]:
        """List a run's artifacts, optionally server-filtered by kind and/or an
        inclusive step window — e.g. sandbox states around a collapse:
        ``list_run_artifacts(run_id, kind="sandbox_state", step_from=599, step_to=601)``.

        ``scope`` controls inheritance (default ``own`` = this run only): ``all`` also
        returns the run's experiment- and project-level artifacts, ``inherited`` only
        those parent levels. On a non-``own`` scope each row carries ``source_level`` and
        rows are ordered nearest-wins, so a ``name`` lookup resolves to the closest level."""
        params = {
            key: value
            for key, value in {
                "kind": kind,
                "step_from": step_from,
                "step_to": step_to,
                "name": name,
                "scope": scope,
            }.items()
            if value is not None
        }
        return self.transport.get(f"/v1/runs/{run_id}/artifacts", params=params or None)

    def move_artifact(
        self, artifact_id: str, *, level: str, target_id: str | None = None
    ) -> dict:
        """Move an artifact vertically along its own run->experiment->project chain.

        Promote up (``level`` above the artifact's current anchor) derives the target
        from its chain; demote down needs a ``target_id`` that sits inside the current
        subtree. A file (workspace/shared) or a lateral target is a 422; an identical
        artifact already at the destination is a 409. The artifact keeps its id."""
        body: dict = {"level": level}
        if target_id is not None:
            body["target_id"] = target_id
        return self.transport.post(f"/v1/artifacts/{artifact_id}/move", body)

    def list_experiment_artifacts(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/artifacts")

    def list_project_artifacts(self, project_id: str) -> list[dict]:
        """Project-wide shared data (fold #22) — the sibling of the experiment read.

        Written as its own literal call site rather than routed through
        :meth:`list_anchored`, for the same reason every anchored route above is:
        the contract-parity guard resolves paths from the AST."""
        return self.transport.get(f"/v1/projects/{project_id}/artifacts")

    # -- the project's notes file ------------------------------------------
    # One markdown document per project that agents read and write. Deliberately NOT
    # a schema: an earlier attempt gave notes a kind vocabulary
    # (intent/decision/observation/...), supersession and an authority field, and
    # nothing server-side validated, aggregated or grouped by any of it -- eight
    # kinds bought one list filter. Prose is what people actually write.
    #
    # It is a COLUMN on the project (research-os 0094), not an artifact. The artifact
    # version was the first implementation and it was wrong twice over: artifact
    # identity is anchor+name+content_hash, so every edit appended a new row and a
    # project's artifact list filled with copies of one file; and reading a paragraph
    # cost three round trips (list -> presign -> R2 GET). The column rides along on
    # the project row, so an orienting caller gets the notes with NO extra request.

    def get_project_notes(self, project_id: str) -> str | None:
        """The project's notes, or None when nobody has written any.

        Note that `GET /v1/projects/{id}` already returns this, so a caller that
        holds the project row should read `row["notes"]` rather than call here --
        the point of the column is that the text costs no second request."""
        return self.get_project(project_id).get("notes")

    def set_project_notes(self, project_id: str, text: str) -> str:
        """Replace the project's notes and return what the server actually stored.

        The read-back is not belt-and-braces. `ProjectPatch` does not forbid extra
        fields, so a backend PREDATING 0094 accepts `notes`, ignores it, and answers
        200 -- the write vanishes and the caller is told it succeeded. Returning the
        stored value makes that detectable instead of silent."""
        queued = self.write("PATCH", f"/v1/projects/{project_id}", {"notes": text})
        if queued is None:
            # Journaled (async mode) or fail-open-spooled: nothing reached the server,
            # so there is nothing to read back. Verifying here would report a failure
            # for a write that is simply still in the outbox.
            return text
        stored = self.get_project(project_id).get("notes")
        if stored != text:
            raise errors.RosError(
                "the server did not store the notes: this backend predates the "
                "projects.notes column (research-os 0094) and silently ignored the "
                "field. Upgrade the backend."
            )
        return stored

    # -- the team wiki (research-os 0098) ----------------------------------
    # ONE markdown document per TENANT, version-checked. The team-level sibling of
    # the project's notes above, and deliberately a different mechanism rather than
    # "notes with a wider anchor":
    #
    #   * it has TWO writers -- a nightly generator sweep and coding agents through
    #     `probe wiki write` -- so a write can legitimately lose, and every write
    #     carries the version it was based on. The notes column is last-one-wins,
    #     which is tolerable for one project and one agent and not for a document
    #     the whole lab reads;
    #   * losing is INFORMATIVE: the 409 carries the current body, so the loser can
    #     merge immediately instead of re-fetching and racing again;
    #   * it has real history (`wiki_versions`) and a revert, which a column cannot
    #     have.
    #
    # There is no id in any of these paths and that is the contract: the tenant on
    # the credential identifies the document completely.
    #
    # OLD-BACKEND BEHAVIOUR is the opposite shape from `set_project_notes` above, and
    # that is why these do not need its read-back. `notes` was a new FIELD on an
    # existing route, and `ProjectPatch` does not forbid extras -- so a pre-0094
    # server took the write, dropped it, and answered 200, which is why that call has
    # to verify. These are new ROUTES: a server that predates them 404s, loudly and
    # at the transport. The only thing left to do is say what the 404 MEANS, because
    # "Not Found" on a document the caller was told always exists reads as data loss.

    @staticmethod
    def _wiki_absent() -> errors.NotFoundError:
        """The route-level 404, translated into the upgrade message.

        No wiki route can 404 for a data reason: `GET /v1/wiki` answers an empty
        document at version 0 for a team that has never had one, and none of the
        paths carry an id to be wrong about. (`POST /v1/wiki/revert` is the one
        exception -- a version that names no row -- and it is handled at its own
        call site rather than here, so a real "no such version" is never rewritten
        into "upgrade your server".)"""
        return errors.NotFoundError(
            "this Probe Research backend predates the team wiki (research-os "
            "0098): upgrade the server. Until then the project's notes "
            "(`probe notes`) are the per-project equivalent."
        )

    def get_wiki(self) -> dict:
        """``GET /v1/wiki`` — the team's current document.

        Always answers: a team that has never generated one gets
        ``{"body": "", "version": 0, "updated_at": None}``. Version 0 is not a
        placeholder -- it is the version a FIRST write must send, which is what
        makes seeding the document the same call as amending it."""
        try:
            return self.transport.get("/v1/wiki")
        except errors.NotFoundError as exc:
            raise self._wiki_absent() from exc

    def set_wiki(self, body: str, version: int, summary: str | None = None) -> dict:
        """``PUT /v1/wiki`` — replace the document, if it is still at ``version``.

        ``version`` is REQUIRED here while the wire schema makes it optional, and
        that asymmetry is deliberate on both sides: the route answers 428
        Precondition Required for a write that carries none, and a client that let
        you omit it would only ever turn that into a round trip. Read
        :meth:`get_wiki` and pass its ``version`` back.

        Raises :class:`errors.ConflictError` when the document has moved. Its
        ``detail`` carries ``{message, expected_version, current_version,
        current_body}`` -- the body is there so a loser can merge without a second
        request, and losing is ORDINARY here rather than exceptional: the other
        writer is usually the nightly sweep, which no caller can see coming.

        NOT routed through :meth:`write`, unlike every other data write in this
        client, and this is the one call where that would be wrong. In
        ``async_writes`` mode `write` journals the request and returns None; the
        outbox would then deliver a version-checked write minutes later, against a
        version that has almost certainly moved, and hand the 409 to a drainer with
        nobody to merge it. A precondition that is checked after the fact is not a
        precondition. So this one is synchronous, and it RAISES rather than
        fail-open spooling.

        Built through the generated ``WikiWrite`` so the 20,000-character document
        cap and the 200-character summary cap fail client-side instead of as a
        server 422."""
        model = WikiWrite(body=body, version=version, summary=summary)
        try:
            return self.transport.put(
                "/v1/wiki", model.model_dump(mode="json", exclude_none=True)
            )
        except errors.NotFoundError as exc:
            raise self._wiki_absent() from exc

    def wiki_versions(
        self, *, limit: int | None = None, before_version: int | None = None
    ) -> dict:
        """``GET /v1/wiki/versions`` — the history, newest first, WITHOUT bodies.

        Returns ``{versions: [...], next_before_version: int|None}``. Each row is
        ``{version, author, summary, created_at, size_chars}``; ``author`` is
        'agent:wiki' for the nightly sweep and the writing credential otherwise,
        which is the first thing a reader wants when a revision surprises them.

        Paged by ``before_version``, not an offset: history is append-only and
        grows at the HEAD, so a revision landing between two requests shifts every
        offset by one and silently re-serves a row the caller already saw. Pass
        ``next_before_version`` back to continue; None means this was the last
        page."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if before_version is not None:
            params["before_version"] = before_version
        try:
            return self.transport.get("/v1/wiki/versions", params=params or None)
        except errors.NotFoundError as exc:
            raise self._wiki_absent() from exc

    def revert_wiki(self, version: int) -> dict:
        """``POST /v1/wiki/revert`` — copy a prior revision FORWARD as a new one.

        History is never rewritten: reverting to version 3 does not delete 4 and 5,
        it appends 3's body as version 6. So a revert is itself revertible, and the
        history list never lies about what the document said when.

        The 404 here is AMBIGUOUS in a way the other three are not -- it is either
        "no such version" or "this backend has no wiki" -- so it is resolved rather
        than assumed. One extra read, only on the error path, and only to avoid
        telling someone to upgrade a server that is already current."""
        try:
            return self.transport.post("/v1/wiki/revert", {"version": version})
        except errors.NotFoundError as exc:
            try:
                self.transport.get("/v1/wiki")
            except errors.NotFoundError:
                raise self._wiki_absent() from exc
            raise
    def append_project_notes(self, project_id: str, text: str) -> str:
        """Extend the project's notes WITHOUT reading them first.

        The read-then-write this replaces was lossy by construction: two writers
        both read the same document, both append their paragraph, and the second
        PATCH overwrites the first. No error, no warning -- you find out by
        noticing prose is missing. Concurrent backfill units made that the
        normal case rather than a rare one.

        The server derives the new value from the row's own column inside one
        UPDATE, so a blocked writer re-reads the winner's committed value and
        appends to that. The separator is the server's business too: whether one
        is needed depends on what the other writer just left behind, which is
        not observable from here without re-introducing the race.

        Returns the stored document, and the read-back is load-bearing for the
        same reason :meth:`set_project_notes`'s is: ``ProjectPatch`` does not
        forbid extra fields, so a backend predating this route accepts
        ``notes_append``, ignores it, and answers 200. Silently appending
        nothing is exactly the failure this method exists to end, so it is
        raised rather than returned.
        """
        queued = self.write("PATCH", f"/v1/projects/{project_id}", {"notes_append": text})
        if queued is None:
            # Journaled or spooled: nothing reached the server yet, so there is
            # nothing to read back and no claim to verify.
            return text
        stored = self.get_project(project_id).get("notes") or ""
        if text.strip() and text.strip() not in stored:
            raise errors.RosError(
                "the server did not append the notes: this backend predates "
                "`notes_append` (research-os 0.117.0.0) and silently ignored the "
                "field. Upgrade the backend, or use `notes write` without --append "
                "if you are the only writer."
            )
        return stored

    def query_series(self, run_ids: list[str], **kw) -> dict:
        return self.transport.post(
            "/v1/series/query", {"run_ids": run_ids, **kw}, idempotent=True
        )

    def latest_scalars(
        self,
        run_ids: list[str],
        *,
        keys: list[str] | None = None,
        kind: str | None = None,
    ) -> dict:
        """``POST /v1/series/latest``: cross-run scalar summary (last/min/max per
        series) for run tables — reads the derived series catalog, never raw
        points. POST-for-read, so it retries like a GET. Every run must be live
        and in-tenant; a soft-deleted or unknown one is a 404 before any read.
        Built through the generated ``LatestScalarsRequest``, so the caps (50
        runs, 200 keys) fail client-side instead of as a server 422."""
        model = LatestScalarsRequest(run_ids=run_ids, keys=keys, kind=kind)
        return self.transport.post(
            "/v1/series/latest",
            model.model_dump(mode="json", exclude_none=True),
            idempotent=True,
        )

    def compare(self, **kw):
        """Fetch several runs and their metric series together, aligned on step.

        The read people actually want when they open a comparison: which of these
        configs won. Name the runs with ``run_ids=[...]`` or select them with the
        same filters :meth:`list_runs` takes (``experiment_id=``, ``group_id=``)::

            comparison = client.compare(experiment_id=exp_id, keys=["dockq"])
            aligned = comparison.aligned("dockq")
            for label, values in aligned.values.items():
                plot(aligned.steps, values, label=label)   # or .to_pandas()

        Columns are labelled by the server's petname ``short_id``. Runs of
        different lengths keep ``None`` holes rather than being truncated to the
        shortest — differing length is usually the thing being compared.

        A shaping layer over :meth:`query_series`, not a second client: see
        :mod:`probe.sdk.analysis`."""
        from .analysis import compare as _compare

        return _compare(self, **kw)

    def search(
        self,
        query: str,
        *,
        corpus: list[str] | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        top_k: int | None = None,
        exact_limit: int | None = None,
        exact_cursor: str | None = None,
        semantic_cursor: str | None = None,
    ) -> dict:
        """``POST /v1/search`` (workspaces+kb fold-in): one-index exact+semantic search.

        POST-for-read, so it retries like any GET. Returns the sectioned
        per-channel response ``{query, state, exact:{results,cursor,error},
        semantic:{results,cursor,error}}``; a backend that predates the
        endpoint 404s (callers such as the MCP source fall back)."""
        body: dict[str, Any] = {"query": query}
        optional = {
            "corpus": corpus,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "top_k": top_k,
            "exact_limit": exact_limit,
            "exact_cursor": exact_cursor,
            "semantic_cursor": semantic_cursor,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self.transport.post("/v1/search", body, idempotent=True)

    def browse(
        self,
        *,
        scope: str | None = None,
        depth: int | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        """``GET /v1/browse``: the structured "what exists" tree.

        ``tags`` filters the RUNS level, like ``status`` (repeatable; a run
        must carry ALL — 0066). A pre-0066 backend ignores it; browse trees
        carry no per-run tags to verify against, so no guard here — the
        deterministic check is ``list_runs(tags=…)``.

        Where ``search`` ranks by relevance and needs a query, this enumerates
        structure and needs nothing -- the cold-start read. Returns
        ``{projects|experiments|runs, cursor, depth, limit, truncated}`` with
        exactly one level populated at the top, decided by ``scope``.

        A backend that predates the endpoint 404s; callers fall back honestly
        rather than presenting an empty tree as "nothing exists".
        """
        params: dict[str, Any] = {}
        optional = {
            "scope": scope,
            "depth": depth,
            "status": status,
            "tags": canonical_tags(tags) if tags else None,
            "limit": limit,
            "cursor": cursor,
        }
        params.update({k: v for k, v in optional.items() if v is not None})
        return self.transport.get("/v1/browse", params=params or None)

    # -- passive / batch push ----------------------------------------------
    def ingest(
        self,
        *,
        experiment_slug: str,
        project_slug: str,
        run: dict,
        batch_id: str | None = None,
        execution_record: dict | None = None,
        spans: list[dict] | None = None,
        metrics: list[dict] | None = None,
        artifacts: list[dict] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """One idempotent push (bearer ingest token + optional HMAC). Keyed on
        ``(customer_id, run.source, run.external_id)`` with ``batch_id`` dedup.

        Built through the generated ``IngestRunRequest`` (the backend now declares
        this body in its OpenAPI schema), so a malformed run/span/metric/artifact
        fails client-side instead of as a server 422.

        The ingest path is where the fold-in fields actually pin server-side:
        ``run['foreign_keys']`` (per-key new-wins merge), ``execution_record``
        (pins ``run.env_ref``), and per-metric ``dimensions``."""
        model = IngestRunRequest(
            experiment_slug=experiment_slug,
            run=run,
            project_slug=project_slug,
            batch_id=batch_id,
            execution_record=execution_record,
            spans=spans or [],
            metrics=metrics or [],
            artifacts=artifacts or [],
        )
        body = model.model_dump(mode="json", exclude_none=True)
        return self.write("POST", "/ingest/v1/runs", body, strict=strict)

    # -- composed SDK surfaces --------------------------------------------
    @property
    def events(self):
        """Read the backend append-only lifecycle+structure events log (read-only)."""
        if self._events is None:
            from .events import EventsReadClient

            self._events = EventsReadClient(self)
        return self._events



# Late import to avoid a cycle at module load (Run needs Client, Client returns Run).
from .run import Run  # noqa: E402
