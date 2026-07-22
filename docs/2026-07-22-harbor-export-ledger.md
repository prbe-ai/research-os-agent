# Harbor durable export ledger

The non-ATIF capture path uses existing Probe records: a rollout span, step-keyed
metrics, file artifacts, and one `kind=harbor_trial` manifest. The local ledger is
an exporter recovery file, not a new backend telemetry model.

## Producer/exporter handoff

The stack calls the SDK producer API; Probe writes a
`probe-harbor-export/1` bundle on durable storage:

```python
from probe.connectors.harbor import stage_trial_export

export = stage_trial_export(
    harbor_trial_dir,
    capture_root / capture_id,
    run_id=probe_run_id,       # optional while offline
    step_index=rollout_id,
    environment=environment,
    correlation=native_ids,   # Miles/session/sample/trial identifiers
    context=data_mix_context,  # opaque Osmosis/customer labels
)
```

The caller supplies native values, not either JSON contract. This keeps Miles,
Osmosis, and future stacks out of Probe's manifest/versioning details.

```text
<capture>/<trial-id>/
├── trial/                 # Harbor's materialized host trial tree
├── trial.tar.gz           # producer recovery copy; never deleted by exporter
├── capture-manifest.json  # file paths, sizes, hashes, known missing outputs
└── export-request.json    # target run + step + opaque correlation
```

`stage_trial_export()` copies and hashes through `stage_trial()`, creates the
recovery archive and capture manifest, writes `export-request.json` last, then
atomically renames the completed bundle into place. `probe trial export REQUEST`
locks and atomically updates that descriptor, verifies the SDK manifest against
the staged bytes, uploads the raw trial files, publishes the `harbor_trial`
manifest, and uploads the producer manifest as `kind=harbor_capture_manifest`
before it marks the request complete. Failure sets `status=failed` and
`last_error` without deleting anything. `probe trial drain ROOT` retries every
unfinished request and continues past individual failures. The compressed
archive remains the local recovery copy; its constituent regular files are the
content-addressed remote records.

If Miles began while the API was unavailable, the descriptor can legitimately
have no Probe run ID. After the run intent resolves, `probe trial drain ROOT
--run RUN` rejects conflicting identities and persists the resolved ID into
each pending descriptor before export.

Correlation such as Miles run/rollout/sample/group/session IDs and Osmosis data
mix IDs is opaque data under `harbor_trial.meta.source.context`. Only the target
Probe run ID, training step, and external span key have structural meaning.

## Completeness boundary

The ledger distinguishes:

- `collection.state`: whether declared host-trial files exist and match their
  hashes on the durable capture path;
- `capture.state`: whether those file bytes are confirmed in Probe storage;
- `manifest_publication`: whether the ordinary `harbor_trial` record was
  published.

The claim is `capture_scope=declared_file_bytes`,
`scope=host_trial_directory`. It cannot establish that the sandbox filesystem
was complete. Public Harbor stops/deletes the environment before `Trial.run()`
returns, so undeclared sandbox state and files Harbor did not materialize remain
explicitly unknown. A Harbor/environment fork can call `stage_trial()` from a
pre-teardown lifecycle hook and wait for `durable_collection_complete`; a normal
post-run bridge cannot retroactively create that guarantee.
