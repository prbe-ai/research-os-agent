"""Weights & Biases -> Probe: read W&B runs (local or hosted) and import them.

Three read paths, in descending fidelity. Every result says WHICH one produced
it (:class:`ReadTier`) because they are not interchangeable: a summary-only
import has one point per metric, and reporting that as curve coverage would let
someone conclude a training run was flat when it was never read.

  HISTORY  the full step-by-step curve, decoded from ``run-<id>.wandb``
  SUMMARY  final scalars only, from ``wandb-summary.json`` (+ any legacy
           ``wandb-history.jsonl``, which IS a real curve when present)
  NONE     the directory is a W&B run and nothing readable came out of it

Why the binary needs decoding at all
------------------------------------
``wandb.Api()`` is an HTTP client for wandb.ai. Offline/never-synced runs are
not there ("Could not find run"), so the ONLY complete local record is
``run-<id>.wandb`` -- a LevelDB-style framed log of ``wandb_internal_pb2.Record``
protobufs. W&B publishes no supported reader for it.

Import vs. vendor (decision, 2026-08-06)
----------------------------------------
We IMPORT ``wandb.sdk.internal.datastore`` rather than vendoring it, even though
W&B is MIT-licensed and vendoring is therefore permitted.

Vendoring the framing loop is ~150 lines and tempting. It is the wrong half of
the problem: the records are ``wandb_internal_pb2`` protobufs, so a vendored
reader still needs the generated protobuf schema -- tens of thousands of
generated lines that describe a wire format W&B revises freely. A vendored copy
would keep *parsing* after an upstream field change and hand back quietly wrong
values, which is strictly worse than not reading at all. Importing binds us to
the installed wandb, so a break surfaces as an exception at a known seam
(:func:`_open_datastore`) that degrades to the SUMMARY tier and says so.

The internal import is confined to :func:`_scan_records`. Nothing else in this
module touches a wandb internal, so re-pointing that one function at a vendored
decoder later is a local change.

Credentials
-----------
``wandb_key`` is resolved env -> ``~/.netrc`` -> probe config context, and stored
with :func:`store_api_key` into the same per-context config file every other
probe credential lives in. ``sdk/redaction.py`` already classes ``wandb_key`` as
sensitive, so it is scrubbed out of any captured payload. This module additionally
never puts the key in a message: :func:`_redacted` scrubs it out of any exception
text raised from the hosted path, because a wandb HTTP error can echo the key back.

Ordering (load-bearing)
-----------------------
A W&B import runs AFTER a file import, and a W&B "project" may map onto an
EXISTING Probe project. So :func:`import_wandb_run` takes the target project as a
required INPUT and never creates or infers one. It also never guesses an
experiment: the run opens PROJECT-DIRECT, which is the shape W&B already has.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..sdk.client import Client
    from ..sdk.run import Run

__all__ = [
    "DEFAULT_PRODUCER",
    "ImportResult",
    "MetricSeries",
    "ReadTier",
    "WandbConnectorError",
    "WandbCredentialsMissing",
    "WandbRun",
    "WandbRunDir",
    "api_key_status",
    "discover_run_dirs",
    "fetch_hosted_run",
    "fetch_hosted_runs",
    "import_local_runs",
    "import_wandb_run",
    "read_local_run",
    "read_local_runs",
    "resolve_api_key",
    "store_api_key",
]

#: Stamped on every derived series this connector writes. These curves were
#: MEASURED by W&B and merely transcribed here, so they are `origin="derived"`
#: with a producer naming the transcriber -- never passed off as live telemetry.
DEFAULT_PRODUCER = "probe.connectors.wandb"

#: The config-context key the API key is stored under. Already listed in
#: ``sdk/redaction.py::_SENSITIVE_KEYS``; keep the two spellings identical.
CONFIG_KEY = "wandb_key"

#: wandb's netrc host.
NETRC_MACHINE = "api.wandb.ai"

#: Run directories W&B writes: ``run-<ts>-<id>`` and ``offline-run-<ts>-<id>``.
_RUN_DIR = re.compile(r"^(?:offline-)?run-(?P<stamp>[^-]+_[^-]+)-(?P<run_id>.+)$")

#: History/summary keys W&B owns. ``_step`` is the x-axis, ``_timestamp`` and
#: ``_runtime`` are clocks, ``_wandb`` is bookkeeping. None of them is a metric
#: someone chose to log, and importing them would put four fake series on every
#: run. Kept on the record as ``WandbRun.wandb_meta``.
_META_PREFIX = "_"


class WandbConnectorError(RuntimeError):
    """Anything this connector refuses to do, phrased for the operator."""


class WandbCredentialsMissing(WandbConnectorError):
    """No W&B API key anywhere. Carries the fix, not just the fact."""


class ReadTier(str, Enum):
    """Which read path produced a :class:`WandbRun`. Never cosmetic.

    ``HISTORY`` is the full curve. ``SUMMARY`` is final values only -- one point
    per metric, at whatever step the run ended on. ``NONE`` means the directory
    was a W&B run and nothing came out of it.
    """

    HISTORY = "history"
    SUMMARY = "summary"
    NONE = "none"

    @property
    def is_full_history(self) -> bool:
        return self is ReadTier.HISTORY


@dataclass(frozen=True)
class WandbRunDir:
    """One on-disk W&B run directory, with the files we know how to read."""

    path: Path
    run_id: str
    offline: bool
    wandb_file: Path | None = None
    summary_file: Path | None = None
    metadata_file: Path | None = None
    history_jsonl: Path | None = None

    @property
    def has_binary(self) -> bool:
        return self.wandb_file is not None


@dataclass(frozen=True)
class MetricSeries:
    """One metric's points, in the order W&B wrote them.

    ``steps``/``values`` are parallel. A SUMMARY-tier series has exactly one
    point; read :attr:`WandbRun.tier` rather than inferring fidelity from length,
    since a one-step run is also one point.
    """

    key: str
    steps: tuple[int, ...]
    values: tuple[float, ...]

    def as_points(self) -> list[tuple[int, float]]:
        return list(zip(self.steps, self.values))

    def __len__(self) -> int:
        return len(self.steps)


@dataclass
class WandbRun:
    """A W&B run, normalized. Produced by every read path, local or hosted."""

    run_id: str
    project: str | None = None
    entity: str | None = None
    display_name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, MetricSeries] = field(default_factory=dict)
    tier: ReadTier = ReadTier.NONE
    #: Never empty on a degraded read. Surface these; do not swallow them.
    warnings: list[str] = field(default_factory=list)
    #: Where the data came from -- a directory path or ``wandb.ai``.
    source: str | None = None
    #: W&B's own ``_step``/``_runtime``/``_timestamp``/``_wandb`` finals.
    wandb_meta: dict[str, Any] = field(default_factory=dict)
    #: Exit code from the binary log, when it recorded one.
    exit_code: int | None = None

    @property
    def qualified_name(self) -> str:
        """``entity/project/run_id`` with the unknown parts dropped."""
        return "/".join(p for p in (self.entity, self.project, self.run_id) if p)

    @property
    def total_points(self) -> int:
        return sum(len(s) for s in self.metrics.values())

    def coverage_note(self) -> str:
        """One line a human can act on. Goes into the derived-metric provenance
        so the tier travels WITH the data instead of only appearing in a log."""
        if self.tier is ReadTier.HISTORY:
            return (
                f"full step history from the W&B transaction log "
                f"({len(self.metrics)} metrics, {self.total_points} points)"
            )
        if self.tier is ReadTier.SUMMARY:
            return (
                f"FINAL VALUES ONLY -- the W&B transaction log was unreadable, so "
                f"{len(self.metrics)} metrics were recovered from the summary; "
                "intermediate steps are absent"
            )
        return "no metric data recovered from this W&B run"


@dataclass
class ImportResult:
    """What :func:`import_wandb_run` did, in terms a caller can assert on."""

    wandb_run_id: str
    probe_run_id: str
    probe_run: "Run"
    tier: ReadTier
    metrics_written: int
    points_written: int
    #: One request per metric key, never one per step. Guarded by a test.
    requests: int
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1. detection
# --------------------------------------------------------------------------


def _classify(path: Path) -> WandbRunDir | None:
    match = _RUN_DIR.match(path.name)
    if not match:
        return None
    run_id = match.group("run_id")
    binary = next((p for p in sorted(path.glob("*.wandb")) if p.is_file()), None)
    files = path / "files"
    summary = _first_existing(path / "wandb-summary.json", files / "wandb-summary.json")
    metadata = _first_existing(path / "wandb-metadata.json", files / "wandb-metadata.json")
    history = _first_existing(path / "wandb-history.jsonl", files / "wandb-history.jsonl")
    return WandbRunDir(
        path=path,
        run_id=run_id,
        offline=path.name.startswith("offline-"),
        wandb_file=binary,
        summary_file=summary,
        metadata_file=metadata,
        history_jsonl=history,
    )


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def discover_run_dirs(root: str | os.PathLike[str]) -> list[WandbRunDir]:
    """Every W&B run directory under ``root``, deduplicated and sorted.

    Walks the whole tree rather than only ``<root>/wandb/``: people move these
    directories around, and a run archived under ``results/2026-08/`` is still a
    run. ``wandb/latest-run`` is a SYMLINK to a sibling run directory -- resolving
    real paths before deduplicating is what stops every run being imported twice.
    """
    base = Path(root)
    if not base.exists():
        raise WandbConnectorError(f"{base} does not exist")
    seen: dict[Path, WandbRunDir] = {}
    candidates: Iterable[Path] = [base, *base.rglob("*")] if base.is_dir() else [base]
    for path in candidates:
        try:
            if not path.is_dir():
                continue
            found = _classify(path)
            if found is None:
                continue
            seen.setdefault(path.resolve(), found)
        except OSError:  # broken symlink, permissions -- not a reason to abort a scan
            continue
    return sorted(seen.values(), key=lambda d: str(d.path))


# --------------------------------------------------------------------------
# 2. local read -- the internal-wandb seam
# --------------------------------------------------------------------------


def _open_datastore(path: Path):
    """The ONE place a wandb internal is imported. Raises WandbConnectorError.

    Every failure mode of the internal API funnels through here -- absent wandb,
    a renamed module, a changed constructor, an invalid header -- so callers have
    a single thing to catch before falling back to the summary tier.
    """
    try:
        from wandb.sdk.internal import datastore  # noqa: PLC0415 - deliberate late import
    except Exception as exc:  # ImportError, or a partially-installed wandb
        raise WandbConnectorError(
            "reading a W&B transaction log needs the `wandb` package installed "
            f"in this environment ({type(exc).__name__}: {exc})"
        ) from exc
    try:
        store = datastore.DataStore()
        store.open_for_scan(str(path))
    except Exception as exc:
        # A truncated/garbage file raises a bare Exception("Invalid header") here.
        raise WandbConnectorError(f"{path.name} is not a readable W&B log: {exc}") from exc
    return store


def _record_class():
    try:
        from wandb.proto import wandb_internal_pb2 as pb  # noqa: PLC0415
    except Exception as exc:
        raise WandbConnectorError(
            f"the wandb protobuf schema is unavailable ({type(exc).__name__}: {exc})"
        ) from exc
    return pb.Record


def _scan_records(path: Path) -> Iterator[Any]:
    """Yield decoded ``Record``s until the log ends OR stops making sense.

    A truncated log -- the normal shape of a run killed mid-write -- fails its
    checksum partway through with an ``AssertionError``. Everything decoded
    BEFORE that point is real data, so the scan stops and keeps it rather than
    discarding the run. The caller learns via the raised-at-the-end sentinel in
    :func:`_read_binary`.
    """
    store = _open_datastore(path)
    record_cls = _record_class()
    while True:
        try:
            raw = store.scan_data()
        except Exception as exc:  # AssertionError on checksum, struct.error, ...
            raise _TruncatedLog(f"{path.name} ends mid-record ({exc})") from exc
        if raw is None:
            return
        record = record_cls()
        try:
            record.ParseFromString(raw)
        except Exception as exc:
            raise _TruncatedLog(f"{path.name} holds an undecodable record ({exc})") from exc
        yield record


class _TruncatedLog(WandbConnectorError):
    """Internal: the log stopped early. Partial data is still returned."""


def _item_key(item: Any) -> str:
    """The metric/config key for a history or config item.

    THE TRAP: W&B populates ``nested_key`` (a repeated path field) and leaves
    ``key`` empty for history items. Reading ``item.key`` returns "" for every
    item, which silently collapses an entire multi-metric run into one series
    named "". ``key`` IS used for top-level config entries, so both are read --
    nested first, because it is the one that carries structure.
    """
    nested = list(getattr(item, "nested_key", ()) or ())
    if nested:
        return ".".join(str(part) for part in nested)
    return str(getattr(item, "key", "") or "")


def _decode_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _numeric(value: Any) -> float | None:
    """``value`` as a metric point, or None.

    Mirrors ``sdk/run.py::_as_metric_value``: strings stay out (a caller who
    logged "0.4" meant the string), bools become 1/0, W&B media dicts and NaN
    payloads fall out on their own.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (str, bytes, bytearray, dict, list)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _is_meta(key: str) -> bool:
    return key.startswith(_META_PREFIX)


class _SeriesBuilder:
    """Accumulates (step, value) per key, preserving write order."""

    def __init__(self) -> None:
        self._points: dict[str, list[tuple[int, float]]] = {}

    def add(self, key: str, step: int, value: float) -> None:
        self._points.setdefault(key, []).append((step, value))

    def build(self) -> dict[str, MetricSeries]:
        out: dict[str, MetricSeries] = {}
        for key, pairs in self._points.items():
            # Last write wins per step: W&B can re-emit a step on resume, and two
            # points at one x would be a duplicate-key write server-side.
            deduped: dict[int, float] = {}
            for step, value in pairs:
                deduped[step] = value
            steps = tuple(sorted(deduped))
            out[key] = MetricSeries(key, steps, tuple(deduped[s] for s in steps))
        return out


def _read_binary(run_dir: WandbRunDir) -> WandbRun:
    """Decode ``run-<id>.wandb``. Raises WandbConnectorError if unusable."""
    assert run_dir.wandb_file is not None
    result = WandbRun(run_id=run_dir.run_id, source=str(run_dir.path), tier=ReadTier.HISTORY)
    builder = _SeriesBuilder()
    meta: dict[str, Any] = {}
    truncation: str | None = None
    saw_history = False
    fallback_step = 0

    records = _scan_records(run_dir.wandb_file)
    while True:
        try:
            record = next(records)
        except StopIteration:
            break
        except _TruncatedLog as exc:
            truncation = str(exc)
            break
        which = record.WhichOneof("record_type")
        if which == "run":
            result.run_id = record.run.run_id or result.run_id
            result.project = record.run.project or None
            result.entity = getattr(record.run, "entity", "") or None
            result.display_name = record.run.display_name or None
            for item in record.run.config.update:
                key = _item_key(item)
                if not key or _is_meta(key):
                    continue
                result.config[key] = _decode_json(item.value_json)
        elif which == "history":
            saw_history = True
            step = getattr(getattr(record.history, "step", None), "num", None)
            items = list(record.history.item)
            if step is None:
                # No step envelope: `_step` among the items is the authority, and
                # a positional counter is the last resort. Never 0-by-default --
                # that would stack an entire run on one x.
                for item in items:
                    if _item_key(item) == "_step":
                        parsed = _numeric(_decode_json(item.value_json))
                        if parsed is not None:
                            step = int(parsed)
                        break
            if step is None:
                step = fallback_step
            fallback_step = int(step) + 1
            for item in items:
                key = _item_key(item)
                if not key:
                    continue
                value = _numeric(_decode_json(item.value_json))
                if value is None:
                    continue
                if _is_meta(key):
                    meta[key] = value
                    continue
                builder.add(key, int(step), value)
        elif which == "summary":
            for item in record.summary.update:
                key = _item_key(item)
                if key and _is_meta(key):
                    meta[key] = _decode_json(item.value_json)
        elif which == "exit":
            result.exit_code = int(record.exit.exit_code)

    result.metrics = builder.build()
    result.wandb_meta = meta
    if truncation:
        result.warnings.append(
            f"{truncation}; kept the {result.total_points} point(s) decoded before that"
        )
    if not saw_history or not result.metrics:
        raise WandbConnectorError(
            f"{run_dir.wandb_file.name} decoded but held no history"
            + (f" ({truncation})" if truncation else "")
        )
    return result


def _read_summary_files(run_dir: WandbRunDir) -> WandbRun:
    """The fallback tier: final scalars, plus a legacy history jsonl if present.

    ``wandb-history.jsonl`` is a REAL curve when it exists (older wandb wrote
    one), so it upgrades the tier back to HISTORY. The summary alone does not.
    """
    result = WandbRun(run_id=run_dir.run_id, source=str(run_dir.path), tier=ReadTier.NONE)

    if run_dir.metadata_file is not None:
        metadata = _load_json_file(run_dir.metadata_file, result)
        if isinstance(metadata, dict):
            result.project = metadata.get("project") or result.project
            result.entity = metadata.get("entity") or result.entity
            result.display_name = metadata.get("name") or result.display_name
            config = metadata.get("config")
            if isinstance(config, dict):
                result.config.update(
                    {k: v for k, v in config.items() if not _is_meta(str(k))}
                )

    if run_dir.history_jsonl is not None:
        builder = _SeriesBuilder()
        meta: dict[str, Any] = {}
        rows = _load_jsonl(run_dir.history_jsonl, result)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            raw_step = _numeric(row.get("_step"))
            step = int(raw_step) if raw_step is not None else index
            for key, value in row.items():
                number = _numeric(value)
                if number is None:
                    continue
                if _is_meta(str(key)):
                    meta[str(key)] = number
                    continue
                builder.add(str(key), step, number)
        series = builder.build()
        if series:
            result.metrics = series
            result.wandb_meta.update(meta)
            result.tier = ReadTier.HISTORY

    if run_dir.summary_file is not None:
        summary = _load_json_file(run_dir.summary_file, result)
        if isinstance(summary, dict):
            step = _numeric(summary.get("_step"))
            final_step = int(step) if step is not None else 0
            finals: dict[str, MetricSeries] = {}
            for key, value in summary.items():
                name = str(key)
                number = _numeric(value)
                if number is None:
                    continue
                if _is_meta(name):
                    result.wandb_meta.setdefault(name, number)
                    continue
                finals[name] = MetricSeries(name, (final_step,), (number,))
            if finals and result.tier is not ReadTier.HISTORY:
                result.metrics = finals
                result.tier = ReadTier.SUMMARY
    return result


def _load_json_file(path: Path, result: WandbRun) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        result.warnings.append(f"{path.name} is unreadable ({exc})")
        return None


def _load_jsonl(path: Path, result: WandbRun) -> list[Any]:
    rows: list[Any] = []
    try:
        text = path.read_text()
    except OSError as exc:
        result.warnings.append(f"{path.name} is unreadable ({exc})")
        return rows
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            result.warnings.append(f"{path.name}:{number} is not JSON; skipped")
    return rows


def read_local_run(run_dir: WandbRunDir | str | os.PathLike[str]) -> WandbRun:
    """Read ONE local W&B run at the best tier that works.

    Tries the transaction log; on any failure of the internal-wandb seam falls
    back to the summary files and records why in ``WandbRun.warnings``. Never
    raises for a degraded read -- check :attr:`WandbRun.tier` before treating the
    result as a curve.
    """
    if not isinstance(run_dir, WandbRunDir):
        path = Path(run_dir)
        found = _classify(path)
        if found is None:
            raise WandbConnectorError(
                f"{path} is not a W&B run directory (expected a 'run-<ts>-<id>' or "
                "'offline-run-<ts>-<id>' name)"
            )
        run_dir = found

    binary_error: str | None = None
    if run_dir.has_binary:
        try:
            return _read_binary(run_dir)
        except WandbConnectorError as exc:
            binary_error = str(exc)
    else:
        binary_error = "no run-<id>.wandb transaction log in this directory"

    result = _read_summary_files(run_dir)
    result.warnings.insert(0, f"fell back from the transaction log: {binary_error}")
    if result.tier is ReadTier.NONE:
        result.warnings.append(
            "no wandb-summary.json / wandb-history.jsonl to fall back to either"
        )
    return result


def read_local_runs(root: str | os.PathLike[str]) -> list[WandbRun]:
    """Read every W&B run under ``root``. One unreadable run never stops the rest."""
    out: list[WandbRun] = []
    for run_dir in discover_run_dirs(root):
        try:
            out.append(read_local_run(run_dir))
        except WandbConnectorError as exc:
            out.append(
                WandbRun(
                    run_id=run_dir.run_id,
                    source=str(run_dir.path),
                    tier=ReadTier.NONE,
                    warnings=[str(exc)],
                )
            )
    return out


# --------------------------------------------------------------------------
# 3. hosted pull -- documented public API only
# --------------------------------------------------------------------------


def _redacted(text: str, secret: str | None) -> str:
    """``text`` with ``secret`` removed. Applied to every hosted-path message.

    W&B's HTTP layer echoes the request -- including the key -- into some error
    strings, and an exception string ends up in logs, tracebacks and bug reports.

    Redacting our own message is only half the job: ``raise X from exc`` keeps
    the ORIGINAL exception on ``__cause__``, and a traceback prints the whole
    chain -- so the unredacted wandb message would be printed anyway, right
    under our careful one. Every hosted-path raise therefore uses ``from None``.
    The type name and the redacted text are carried forward in the message, so
    what is lost is the wandb frame list, not the diagnosis.
    """
    if not secret or len(secret) < 8:
        return text
    return text.replace(secret, "<redacted>")


def _netrc_key() -> str | None:
    """The wandb password from ``~/.netrc``. Never raises."""
    try:
        import netrc  # noqa: PLC0415

        auth = netrc.netrc().authenticators(NETRC_MACHINE)
    except Exception:
        return None
    if not auth:
        return None
    password = auth[2]
    return password or None


def resolve_api_key(explicit: str | None = None, *, context: str | None = None) -> str | None:
    """The W&B API key, or None. Precedence: explicit -> env -> netrc -> config.

    Env and netrc come first so an existing ``wandb login`` on the machine keeps
    working with no probe-side setup at all.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("WANDB_API_KEY")
    if from_env:
        return from_env
    from_netrc = _netrc_key()
    if from_netrc:
        return from_netrc
    from ..sdk.config import load_context  # noqa: PLC0415

    stored = load_context(context).get(CONFIG_KEY)
    return stored if isinstance(stored, str) and stored else None


def require_api_key(explicit: str | None = None, *, context: str | None = None) -> str:
    """:func:`resolve_api_key`, or an error that says how to fix it."""
    key = resolve_api_key(explicit, context=context)
    if key:
        return key
    raise WandbCredentialsMissing(
        "no Weights & Biases API key is configured. Get one from "
        "https://wandb.ai/authorize, then do ONE of:\n"
        "  export WANDB_API_KEY=<key>            (this shell only)\n"
        "  wandb login                           (writes ~/.netrc)\n"
        "  probe.connectors.wandb.store_api_key(<key>)   (probe config, per context)"
    )


def store_api_key(key: str, *, context: str | None = None) -> Path:
    """Persist the key into the active probe config context.

    Same file, same lock, same 0600 as every other probe credential, under the
    key ``wandb_key`` -- which ``sdk/redaction.py`` already classes as sensitive,
    so it is scrubbed out of captured payloads. ``probe logout`` clears it with
    the rest of the context (``clear_context`` wipes rather than subtracts).
    """
    if not isinstance(key, str) or not key.strip():
        raise WandbConnectorError("refusing to store an empty W&B API key")
    from ..sdk.config import save_context  # noqa: PLC0415

    return save_context({CONFIG_KEY: key.strip()}, name=context)


def api_key_status(*, context: str | None = None) -> dict[str, Any]:
    """Where a key would come from, WITHOUT returning the key itself.

    The shape a `status`/`doctor` surface should print. It reports presence and
    origin only; there is deliberately no accessor that prints the secret.
    """
    from ..sdk.config import load_context  # noqa: PLC0415

    stored = load_context(context).get(CONFIG_KEY)
    sources = {
        "env": bool(os.environ.get("WANDB_API_KEY")),
        "netrc": bool(_netrc_key()),
        "config": bool(isinstance(stored, str) and stored),
    }
    origin = next((name for name, present in sources.items() if present), None)
    return {"configured": origin is not None, "source": origin, "sources": sources}


def _wandb_api(api_key: str):
    """A ``wandb.Api()`` bound to ``api_key`` without touching global state.

    The key goes in via the constructor's documented ``api_key`` override rather
    than ``os.environ``, so it never outlives this call in the process env.
    """
    try:
        import wandb  # noqa: PLC0415
    except Exception as exc:
        raise WandbConnectorError(
            f"pulling hosted W&B runs needs the `wandb` package installed "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    try:
        return wandb.Api(api_key=api_key)
    except Exception as exc:
        raise WandbConnectorError(
            _redacted(f"could not open the W&B API ({type(exc).__name__}: {exc})", api_key)
        ) from None  # see _redacted: the cause would print the key unredacted


def _from_hosted(handle: Any, api_key: str) -> WandbRun:
    """Normalize one ``wandb.apis.public.Run`` into a :class:`WandbRun`."""
    result = WandbRun(
        run_id=str(getattr(handle, "id", "") or ""),
        project=getattr(handle, "project", None) or None,
        entity=getattr(handle, "entity", None) or None,
        display_name=getattr(handle, "name", None) or None,
        source="wandb.ai",
        tier=ReadTier.HISTORY,
    )
    raw_config = getattr(handle, "config", None)
    if isinstance(raw_config, dict):
        result.config = {k: v for k, v in raw_config.items() if not _is_meta(str(k))}

    builder = _SeriesBuilder()
    meta: dict[str, Any] = {}
    try:
        rows = handle.scan_history()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            raw_step = _numeric(row.get("_step"))
            step = int(raw_step) if raw_step is not None else index
            for key, value in row.items():
                number = _numeric(value)
                if number is None:
                    continue
                if _is_meta(str(key)):
                    meta[str(key)] = number
                    continue
                builder.add(str(key), step, number)
    except Exception as exc:
        raise WandbConnectorError(
            _redacted(
                f"scan_history() failed for {result.qualified_name} "
                f"({type(exc).__name__}: {exc})",
                api_key,
            )
        ) from None  # see _redacted

    result.metrics = builder.build()
    result.wandb_meta = meta
    if not result.metrics:
        result.tier = ReadTier.NONE
        result.warnings.append("scan_history() returned no numeric points")
    return result


def fetch_hosted_runs(
    entity: str,
    project: str,
    *,
    api_key: str | None = None,
    run_ids: Iterable[str] | None = None,
    context: str | None = None,
) -> list[WandbRun]:
    """Pull runs from wandb.ai via the documented public API.

    ``api.runs(f"{entity}/{project}")`` then ``run.scan_history()`` -- the
    supported path, unlike the local reader. ``run_ids`` filters client-side.
    """
    key = require_api_key(api_key, context=context)
    api = _wandb_api(key)
    wanted = {str(r) for r in run_ids} if run_ids is not None else None
    try:
        handles = list(api.runs(f"{entity}/{project}"))
    except Exception as exc:
        raise WandbConnectorError(
            _redacted(
                f"could not list runs in {entity}/{project} ({type(exc).__name__}: {exc})",
                key,
            )
        ) from None  # see _redacted
    out: list[WandbRun] = []
    for handle in handles:
        if wanted is not None and str(getattr(handle, "id", "")) not in wanted:
            continue
        out.append(_from_hosted(handle, key))
    return out


def fetch_hosted_run(
    entity: str,
    project: str,
    run_id: str,
    *,
    api_key: str | None = None,
    context: str | None = None,
) -> WandbRun:
    """One hosted run by id."""
    key = require_api_key(api_key, context=context)
    api = _wandb_api(key)
    try:
        handle = api.run(f"{entity}/{project}/{run_id}")
    except Exception as exc:
        raise WandbConnectorError(
            _redacted(
                f"could not read {entity}/{project}/{run_id} "
                f"({type(exc).__name__}: {exc})",
                key,
            )
        ) from None  # see _redacted
    return _from_hosted(handle, key)


# --------------------------------------------------------------------------
# 4. map onto Probe
# --------------------------------------------------------------------------


def external_id_for(run: WandbRun) -> str:
    """A stable, collision-free external id, so a re-import is recognized as one."""
    return f"wandb:{run.qualified_name}"


def foreign_keys_for(run: WandbRun) -> dict[str, str]:
    """The ``probe link`` payload -- ``wandb_run_id`` plus what else is known."""
    keys = {"wandb_run_id": run.run_id}
    if run.project:
        keys["wandb_project"] = run.project
    if run.entity:
        keys["wandb_entity"] = run.entity
    return keys


def _scrubbed_config(run: WandbRun) -> dict[str, Any]:
    """The W&B config with secrets stripped.

    A W&B config is whatever the researcher passed to ``wandb.init`` -- it
    routinely carries an ``hf_token`` or an ``api_key``. ``Client(redact=True)``
    is opt-in, so scrubbing here means the connector cannot import a secret into
    Probe even when the caller did not ask for redaction.
    """
    from ..sdk.redaction import default_scrub  # noqa: PLC0415

    return {key: default_scrub(value, key=str(key)) for key, value in run.config.items()}


def import_wandb_run(
    client: "Client",
    run: WandbRun,
    *,
    project: str,
    name: str | None = None,
    producer: str = DEFAULT_PRODUCER,
    kind: str = "model",
    tags: list[str] | None = None,
    metrics: Iterable[str] | None = None,
    finish: bool = True,
    heartbeat: bool = False,
    on_conflict: str = "auto",
    dry_run: bool = False,
) -> ImportResult:
    """Import one W&B run into an EXISTING Probe ``project``.

    ``project`` is required and is never inferred. A W&B import runs after a file
    import and may land in a project that already exists for other reasons, so
    the caller -- who knows the mapping -- supplies it. The run opens
    PROJECT-DIRECT (``client.run(project=...)``), which is the shape W&B already
    has: no experiment is invented to hold it.

    Metrics are written with :meth:`Run.log_derived_series` -- ONE request per
    metric key, whole curve in the body. They land as ``origin="derived"`` with
    ``producer``, because they were measured by W&B and transcribed here, not
    observed live. The provenance note carries :meth:`WandbRun.coverage_note`, so
    a SUMMARY-tier import is self-describing wherever it is read.

    ``dry_run`` validates and returns the plan without opening a Probe run.
    """
    if not project:
        raise WandbConnectorError(
            "import_wandb_run() needs an explicit target Probe project. W&B "
            "projects do not map 1:1 onto Probe projects and the import runs "
            "after a file import, so the caller owns that decision."
        )
    if run.tier is ReadTier.NONE or not run.metrics:
        raise WandbConnectorError(
            f"W&B run {run.qualified_name} has no importable metrics "
            f"({'; '.join(run.warnings) or 'nothing was decoded'})"
        )

    selected = dict(run.metrics)
    if metrics is not None:
        wanted = list(metrics)
        missing = [key for key in wanted if key not in selected]
        if missing:
            raise WandbConnectorError(
                f"W&B run {run.qualified_name} has no metric(s) {', '.join(missing)}; "
                f"it has {', '.join(sorted(selected)) or 'none'}"
            )
        selected = {key: selected[key] for key in wanted}

    if dry_run:
        return ImportResult(
            wandb_run_id=run.run_id,
            probe_run_id="",
            probe_run=None,  # type: ignore[arg-type]
            tier=run.tier,
            metrics_written=len(selected),
            points_written=sum(len(s) for s in selected.values()),
            requests=len(selected),
            warnings=list(run.warnings),
        )

    probe_run = client.run(
        project=project,
        name=name or run.display_name or run.run_id,
        external_id=external_id_for(run),
        config=_scrubbed_config(run),
        tags=list(tags) if tags else None,
        metadata={
            "imported_from": "wandb",
            "wandb_tier": run.tier.value,
            "wandb_source": run.source,
            "wandb_coverage": run.coverage_note(),
        },
        heartbeat=heartbeat,
        on_conflict=on_conflict,
    )

    note = f"imported from W&B run {run.qualified_name}: {run.coverage_note()}"
    written = 0
    points = 0
    for key, series in selected.items():
        # ONE request per metric key -- the whole curve rides in the body. A
        # per-step call would be 3000 round trips for a 3000-step run.
        probe_run.log_derived_series(
            key,
            series.as_points(),
            producer=producer,
            note=note,
            kind=kind,
            code_ref=DEFAULT_PRODUCER,
        )
        written += 1
        points += len(series)

    probe_run.link(**foreign_keys_for(run))
    if finish:
        status = "completed" if (run.exit_code in (0, None)) else "failed"
        probe_run.finish(status=status)

    return ImportResult(
        wandb_run_id=run.run_id,
        probe_run_id=probe_run.id,
        probe_run=probe_run,
        tier=run.tier,
        metrics_written=written,
        points_written=points,
        requests=written,
        warnings=list(run.warnings),
    )


def import_local_runs(
    client: "Client",
    root: str | os.PathLike[str],
    *,
    project: str,
    **kwargs: Any,
) -> list[ImportResult]:
    """Import every readable W&B run under ``root`` into ``project``.

    Unreadable runs are skipped, not fatal -- a directory tree with one corrupt
    log should still import the other nine runs. Check the returned
    ``ImportResult.tier`` and ``.warnings`` before reporting coverage.
    """
    results: list[ImportResult] = []
    for run in read_local_runs(root):
        if run.tier is ReadTier.NONE or not run.metrics:
            continue
        results.append(import_wandb_run(client, run, project=project, **kwargs))
    return results
