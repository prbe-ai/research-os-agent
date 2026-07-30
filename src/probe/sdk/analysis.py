"""Reading several runs back for comparison.

This is the job people open ``wandb.Api()`` for: *show me the runs in this group,
aligned on step, so I can see which config won*. Probe already has the backend
primitive for it — ``POST /v1/series/query`` takes a list of run ids and returns
every series in one round trip — but no front door, so the same loop got written
by hand each time: list the runs, fetch each one's series, bucket by step, work
out which column belongs to which run.

Deliberately NOT a second client. W&B splits reads onto a separate ``Api`` object
because it has two transports (a service process for writes, GraphQL for reads);
one REST transport does not need that, and a second object to authenticate and
configure would buy nothing. This is a shaping layer over
:meth:`~probe.sdk.client.Client.query_series`, reached as ``client.compare(...)``.

No pandas dependency. :meth:`Comparison.to_pandas` builds a DataFrame when pandas
is installed, and the plain structures underneath plot directly:

    aligned = client.compare(experiment_id=exp).aligned("loss")
    for label, values in aligned.values.items():
        plt.plot(aligned.steps, values, label=label)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

#: The server caps a series query at 50 run ids (SeriesQueryRequest.run_ids).
#: Comparing more is a real thing to want, so this batches rather than truncating.
_RUN_IDS_PER_QUERY = 50


@dataclass
class Aligned:
    """One metric key across several runs, on a shared step axis.

    ``values`` maps a run label to a list positionally matching :attr:`steps`,
    with ``None`` where that run has no point at that step — runs are compared
    precisely when they did NOT run the same length, so holes are the normal case
    and dropping them would silently truncate every series to the shortest."""

    key: str
    kind: str
    steps: list[int]
    values: dict[str, list[float | None]]

    @property
    def labels(self) -> list[str]:
        return list(self.values)

    def rows(self):
        """``(step, {label: value})`` per step, for writing out or tabulating."""
        for index, step in enumerate(self.steps):
            yield step, {label: col[index] for label, col in self.values.items()}

    def to_pandas(self):
        """A DataFrame indexed by step, one column per run. Requires pandas."""
        pandas = _pandas()
        return pandas.DataFrame(self.values, index=pandas.Index(self.steps, name="step"))


@dataclass
class Comparison:
    """Several runs and their series, fetched together.

    ``runs`` is in the order asked for; ``series`` is the raw
    ``SeriesResult`` list, kept so nothing here is a lossy wrapper over the API."""

    runs: list[dict]
    series: list[dict] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.runs)

    @property
    def keys(self) -> list[str]:
        """Metric keys present, in first-seen order."""
        seen = {}
        for row in self.series:
            seen.setdefault(row["key"], None)
        return list(seen)

    def label(self, run_id: str) -> str:
        """A readable, UNIQUE column name for a run.

        Prefers the server's petname ``short_id``. That is the thing a person
        recognises — but it is optional and nullable on the wire, so the fallback
        is ``name``, which is emphatically not unique: generated run names are
        second-resolution, so a sweep launching runs in the same second produces
        identical names. Two runs sharing a column would silently merge into one
        curve built from both, so a collision gets a short-id suffix."""
        chosen = None
        for row in self.runs:
            if str(row.get("id")) == str(run_id):
                chosen = row.get("short_id") or row.get("name")
                break
        if not chosen:
            return str(run_id)[:8]
        clashes = [
            row
            for row in self.runs
            if (row.get("short_id") or row.get("name")) == chosen
        ]
        if len(clashes) > 1:
            return f"{chosen} ({str(run_id)[:8]})"
        return chosen

    def aligned(self, key: str, *, kind: str = "model") -> Aligned:
        """One metric across every run, on the union of their step axes."""
        matching = [
            row for row in self.series if row["key"] == key and row.get("kind", "model") == kind
        ]
        if not matching:
            available = ", ".join(self.keys) or "none"
            raise KeyError(f"no series {key!r} (kind={kind!r}) in this comparison. Have: {available}")

        by_run: dict[str, dict[int, float]] = {}
        steps: set[int] = set()
        for row in matching:
            label = self.label(row["run_id"])
            points = by_run.setdefault(label, {})
            for point in row.get("points") or []:
                step = point.get("step_index")
                if step is None:
                    # A point with no step is on the wall-clock axis; it has no
                    # position on a step-aligned table, and inventing one would
                    # put unrelated points on the same row.
                    continue
                points[step] = point["value"]
                steps.add(step)

        ordered = sorted(steps)
        return Aligned(
            key=key,
            kind=kind,
            steps=ordered,
            values={
                label: [points.get(step) for step in ordered] for label, points in by_run.items()
            },
        )

    def to_pandas(self, key: str, *, kind: str = "model"):
        return self.aligned(key, kind=kind).to_pandas()


def _pandas():
    try:
        import pandas
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        raise ModuleNotFoundError(
            "to_pandas() needs pandas, which probe does not depend on. "
            "`pip install pandas`, or use .steps / .values directly — they plot as-is."
        ) from exc
    return pandas


def compare(
    client,
    *,
    run_ids: list[str] | None = None,
    keys: list[str] | None = None,
    kind: str = "model",
    step_from: int | None = None,
    step_to: int | None = None,
    max_points: int | None = None,
    **filters: Any,
) -> Comparison:
    """Fetch several runs and their series together. See :meth:`Client.compare`."""
    if run_ids is None:
        # Follow the cursor. GET /v1/runs defaults to limit=50 (max 200) and Page
        # does NOT auto-paginate, so taking .items would quietly compare an
        # experiment's first page and present it as the whole thing — the exact
        # failure the >50 batching below exists to avoid.
        runs = []
        cursor = None
        while True:
            page = client.list_runs(**filters, limit=200, cursor=cursor)
            runs.extend(page.items)
            cursor = page.next_cursor
            if not cursor:
                break
    else:
        runs = [client.get_run(run_id) for run_id in run_ids]
    if not runs:
        return Comparison(runs=[], series=[])

    ids = [str(row["id"]) for row in runs]
    body: dict[str, Any] = {}
    if keys is not None:
        body["series"] = [{"key": key, "kind": kind} for key in keys]
    if step_from is not None:
        body["step_from"] = step_from
    if step_to is not None:
        body["step_to"] = step_to
    if max_points is not None:
        body["max_points"] = max_points

    series: list[dict] = []
    # Batched, not truncated: silently dropping runs 51+ would read as "these are
    # all of them", which is the wrong answer to hand someone comparing configs.
    for start in range(0, len(ids), _RUN_IDS_PER_QUERY):
        chunk = ids[start : start + _RUN_IDS_PER_QUERY]
        result = client.query_series(chunk, **body)
        series.extend(result.get("series") or [])
    if len(ids) > _RUN_IDS_PER_QUERY:
        warnings.warn(
            f"comparing {len(ids)} runs took {-(-len(ids) // _RUN_IDS_PER_QUERY)} series "
            "queries; narrow with keys= or step_from/step_to if this is slow.",
            stacklevel=3,
        )
    return Comparison(runs=runs, series=series)
