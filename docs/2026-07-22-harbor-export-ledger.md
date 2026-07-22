# Harbor durable export ledger

The non-ATIF capture path uses existing Probe records: a rollout span, step-keyed
metrics, file artifacts, and one `kind=harbor_trial` manifest. The local ledger is
an exporter recovery file, not a new backend telemetry model.

## Producer/exporter handoff

Miles writes a `probe-harbor-export/1` bundle on durable storage:

```text
<capture>/<trial-id>/
├── trial/                 # Harbor's materialized host trial tree
├── trial.tar.gz           # producer recovery copy; never deleted by exporter
├── capture-manifest.json  # file paths, sizes, hashes, known missing outputs
└── export-request.json    # target run + step + opaque correlation
```

The producer writes `export-request.json` last. `probe trial export REQUEST`
locks and atomically updates that descriptor, verifies the manifest against the
staged bytes, uploads the raw trial files, publishes the `harbor_trial` manifest,
and uploads the producer manifest as `kind=harbor_capture_manifest` before it
marks the request complete. Failure sets `status=failed` and `last_error` without
deleting anything. `probe trial drain ROOT` retries every unfinished request and
continues past individual failures. The compressed archive remains the local
recovery copy; its constituent regular files are the content-addressed remote
records.

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
