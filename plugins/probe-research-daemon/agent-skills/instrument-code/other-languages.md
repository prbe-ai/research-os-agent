# Record runs and agent calls from a language with no Probe SDK

Read this when the code to instrument is not Python (TypeScript, Rust, Go, ...). Python has the SDK:
use it there. Build a small client in the program's own language from this page, then prove it with
`probe client check`.

## THE CALLS, IN ORDER

1. Sign in (below), then find the project by its slug (lowercase words joined by `-`); create it,
   with a `kind` (`general` when unsure), when none has it. A `409` on that create means another
   process made it first: find it again.
2. Create the run once, with a `creation_key`.
3. Heartbeat once the run exists, then every 60 s until the run ends.
4. Send metric points and agent calls as they happen. Every metrics `POST` carries
   `X-Probe-Async-Ack: 1` (below).
5. Finish: once every queued write is answered, `PATCH` the run's `status` (`completed`, `failed`
   or `canceled`) and `ended_at`. A write still queued at the finish is lost when the program exits.
   Probe may answer the finish `503` while it stores queued points: wait and send it again (below).

Every field you send and read is in ENDPOINTS below, generated from the server's schema.

## SIGN-IN

- Token: `PROBE_TOKEN`, else the CLI's config file: `PROBE_CONFIG_PATH`, else
  `$XDG_CONFIG_HOME/probe/config.json`, else `~/.config/probe/config.json`. In it,
  `contexts[current_context].token` (`current_context` defaults to `default`); an older flat file
  holds `token` and `base_url` at the top.
- Address: `PROBE_BASE_URL`, else that context's `base_url`, else `https://api.research.prbe.ai`.
  Send the token only over `https://` (plain `http://` only to localhost) and never across a redirect.
- The stored token goes only to its own context's `base_url`: a different `PROBE_BASE_URL` needs
  `PROBE_TOKEN`. Prefer `PROBE_TOKEN` in anything deployed.
- Every request: `Authorization: Bearer <token>`, `Content-Type: application/json` and the
  `Probe-Client` header (below). Keep the token out of every log line and warning.
- No token, or `PROBE_MODE` set to anything but `online` (`disabled`, `offline`): warn once and
  record nothing; the program runs on.

## CREATE THE RUN ONCE

Mint one `creation_key` per run (a random 32-hex string) and keep the exact request body. When a
create's answer is lost (timeout, 5xx), send the SAME body again: the server answers `201` with the run
that key already made (header `Idempotent-Replay: true`), never a second run. A different body under a
used key is `422`.

`PROBE_RUN_ID` set (`probe exec` and remote launches set it): the launcher already opened that run.
Write to it; never create a run or project, and leave the finish to the launcher. Beat with
`?attached=true` (the job itself is reporting). A `PROBE_TOKEN` starting `probe_run_` is that one
run's token (its id is also the `rid` claim of the token's middle, base64url JSON part): it can
write the run, never `/v1/projects`.

## HEARTBEAT

Create the run with `heartbeat: true`, then `POST .../heartbeat` at once and every 60 s from the
process doing the work, until it finishes. A run silent for 15 minutes (the server's default) is marked `crashed`.
Never retry a beat, the one exception to the retry rule below: the next beat is its retry. A beat
answered `completed`, `failed`, `crashed` or `canceled` means the run has ended: warn once and stop
beating.

## FAILURES NEVER REACH THE PROGRAM

- `429`, `5xx` and network errors: retry in the background with jittered backoff (a random wait up
  to 1 s, 2 s, 4 s, ... capped at 60 s), never sooner than `Retry-After` when the answer has it.
  Hold at most 1,000 unsent requests; past that drop the oldest and warn once. Never block the
  program's own work on Probe.
- Any other `4xx`: drop that request and warn once per kind. After a `401` stop sending: the token
  is wrong or expired.
- At exit, flush what is queued and send the finish within ONE deadline of 600 s, as the Python SDK
  does (`FINISH_TIMEOUT_DEFAULT_SECONDS` in `run/closing/drain.py`): flush until 5 s before it
  (`_CLOSE_RESERVE_SECONDS`), then send the finish even if the flush ran out (dropping what is still
  queued), then let the program end. Wait up to 30 s for each answer (`_DEFAULT_TIMEOUT` in
  `delivery/transport/transport.py`) and retry with waits of at most 4 s while exiting
  (`_FINISH_BACKOFF`). The deadline is a ceiling: a healthy exit ends when the last answer lands.
  Never cut it to a few seconds: on prod a metrics write answered in 5.0-6.9 s and a finish in
  5.5-7.7 s, so a 5 s budget loses the tail of the curve and leaves the run `running`.
- Only a host that is killed sooner (a serverless handler, a container's SIGTERM grace period) uses
  a short budget: the time it is given, less 5 s kept for the finish. Say in the client's README
  that it may lose the last writes and leave the run `running` (the server marks it `crashed` after
  15 minutes without a beat).
- Catch everything. The client never throws into, crashes or slows the program it records; the program's
  exit code is its own.
- Send `step_index` on every metric point, increasing per key: a point at a key and step already
  stored is dropped (first write wins, counted in `deduped`), so a retried batch is never counted
  twice, and a second value logged at the same step is lost. Skip a NaN or infinite value: JSON has
  none, and one makes the whole batch `422`.

## METRICS ARE ACKED EARLY

Probe stores metric points through a queue. Send `X-Probe-Async-Ack: 1` on every metrics `POST`:
Probe then answers `202` as soon as the batch is safe in the queue, and stores it a moment later.
Without the header each write waits for the store: 10-20 s on prod, and `probe client check` fails
`metrics`.

- `202` is success, like `201`: never retry it, the batch is kept. Read nothing from its body.
- A finish sent while queued points are still being stored can answer `503` with
  `detail.code == "data_pending"` (and `detail.pending_points`) and a `Retry-After`. That is waiting,
  not failing: send the SAME finish again, never sooner than `Retry-After` and with waits of at most
  4 s, until it is answered or the 600 s exit deadline ends. Do not stop after a few tries, as for
  other `5xx`.

## AGENT CALLS ARE TRIALS

- One agent call (one trace) is one trial. Send the whole call in one request once its root span has
  ended; most calls fit in one.
- Over a limit (Trace limits below) the answer is `413`: split the call, parents first. The first
  request holds the root (a request whose root is neither in it nor already stored is `422`), and a
  Mastra `model_step` travels with its `model_chunk` children (reasoning sent apart from its step is
  dropped).
- Resending a call is safe: span ids come from (run, trace, span), so it rewrites the same rows.
- `source`: `mastra` for Mastra's exported spans as they are; `generic` for spans you shape
  yourself (the `generic` span table below): one `rollout` per trace with no parent, its `turn`s,
  each turn's `tool_call`s. A turn's `attributes`: `source` (`user` or `agent`), `message`,
  `reasoning`, `prompt_tokens`, `completion_tokens`, `trajectory_index` (its order from 0, the
  user's prompt first); a tool call's: `function_name`, `arguments`, `result`.

## PROBE-CLIENT AND THE PROTOCOL VERSION

Send `Probe-Client: <language>/<protocol version>` on every request, e.g. `typescript/1`. The
protocol is version 1. It only grows by additions: new optional fields and new endpoints, which a
version-1 client ignores. A breaking change gets a new version, served beside the old one.

## MASTRA, THE WORKED EXAMPLE

A Mastra exporter receives every span; this one sends each agent call when its root span ends
(Mastra ends children first). Spans go as Mastra exports them, prompts, tool arguments and results
included: pass `{ customSpanFormatter }` as its config to scrub them first. Register it with
`new Observability({ configs: { default: { serviceName, exporters: [new ProbeExporter(client, runId, mastraVersion)] } } })`.
Proven with `@mastra/core` 1.81.0 and `@mastra/observability` 1.23.0.

- Create the run before building `Mastra`: the exporter needs its `runId`. `mastraVersion` is the
  `version` in `@mastra/core/package.json`.
- At exit: `await mastra.shutdown()` (every span reaches the exporter), then the client's flush, then
  the finish.
- Mastra sends thinking only when the model call turns it on (`providerOptions.anthropic.thinking`,
  or the provider's own reasoning option).

```typescript
import { BaseExporter, type BaseExporterConfig } from "@mastra/observability";
import { SpanType, TracingEventType, type AnyExportedSpan, type TracingEvent } from "@mastra/core/observability";

/** Your client (sign-in, headers, retries): resolves to the HTTP status, never throws. */
export interface ProbeClient {
  send(method: string, path: string, body?: unknown): Promise<number>;
}

const MAX_SPANS = 1000; // spans per request, from "Trace limits" below
const MAX_OPEN_CALLS = 100; // calls whose root has not ended; past it the oldest is dropped

/** Each Mastra agent call becomes one Probe trial, sent when its root span ends. */
export class ProbeExporter extends BaseExporter {
  name = "probe";
  private ended = new Map<string, AnyExportedSpan[]>(); // traceId -> its ended spans

  constructor(private probe: ProbeClient, private runId: string, private mastraVersion: string,
              config?: BaseExporterConfig) { // config.customSpanFormatter scrubs spans before they leave
    super(config);
  }

  protected async _exportTracingEvent(event: TracingEvent): Promise<void> {
    if (event.type !== TracingEventType.SPAN_ENDED) return;
    const span = event.exportedSpan;
    if (!this.ended.has(span.traceId) && this.ended.size >= MAX_OPEN_CALLS) {
      this.ended.delete(this.ended.keys().next().value!); // a call whose root never ended
    }
    const spans = this.ended.get(span.traceId) ?? [];
    spans.push(span);
    this.ended.set(span.traceId, spans);
    if (span.parentSpanId) return; // Mastra ends children first and the root last
    this.ended.delete(span.traceId);
    for (const batch of splitParentFirst(spans, MAX_SPANS)) await this.post(batch);
  }

  private async post(units: AnyExportedSpan[][]): Promise<void> {
    const body = { source: "mastra", source_version: this.mastraVersion, spans: units.flat() };
    const status = await this.probe.send("POST", `/v1/runs/${this.runId}/traces`, body);
    if (status === 413 && units.length > 1) { // too many bytes: halve, the parent half first
      const half = Math.ceil(units.length / 2);
      await this.post(units.slice(0, half));
      await this.post(units.slice(half));
    } else if (status === 413) {
      console.warn(`probe: one step of call ${units[0][0].traceId} is over the request limit; not recorded`);
    }
  }
}

/** Requests of units, parents before children; a model_chunk travels in its model_step's unit. */
function splitParentFirst(spans: AnyExportedSpan[], max: number): AnyExportedSpan[][][] {
  const byId = new Map(spans.map((s) => [s.id, s]));
  const depths = new Map<string, number>();
  const depth = (s: AnyExportedSpan): number => {
    const parent = s.parentSpanId ? byId.get(s.parentSpanId) : undefined;
    if (!depths.has(s.id)) depths.set(s.id, parent ? 1 + depth(parent) : s.parentSpanId ? 1 : 0); // root first
    return depths.get(s.id)!;
  };
  const units = new Map<string, AnyExportedSpan[]>();
  for (const s of [...spans].sort((a, b) => depth(a) - depth(b))) {
    const step = s.type === SpanType.MODEL_CHUNK && s.parentSpanId ? units.get(s.parentSpanId) : undefined;
    if (step) step.push(s);
    else units.set(s.id, [s]);
  }
  const batches: AnyExportedSpan[][][] = [[]];
  let count = 0; // spans in the last batch
  for (const unit of units.values()) {
    if (count + unit.length > max && count > 0) { batches.push([]); count = 0; }
    batches[batches.length - 1].push(unit);
    count += unit.length;
  }
  return batches;
}
```

## CHECK IT

Write a short test program that uses your client as the real program will: create the run, log a
few metric points, send one agent call of 2 or more spans (only when the program records agent
calls), finish, exit 0. Then run
`probe client check -- <command>` (`--json` prints one line; a CLI without it needs `probe update`).
It starts a local stand-in for the server and runs the command three times with `PROBE_BASE_URL`
and a throwaway `PROBE_TOKEN` set (nothing reaches Probe):

1. create: Probe answers one `409` (project create), `503` (run create), `500` (metrics), `503
   data_pending` (the finish) and `413` (the first call of 2 or more spans) along the way, answers
   `202` to a metrics write that carries `X-Probe-Async-Ack: 1`, and holds every metrics and finish
   answer for 7 s (`--write-delay`, 0 turns it off), so a client that exits before those answers
   fails `metrics` or `finish`;
2. join: `PROBE_RUN_ID` and a run token are set;
3. refused: every answer is `401`.

It prints pass or FAIL per rule, with the reason: the project found by its slug (created with a
`kind`), the run created once, metrics, the call split after its `413`, heartbeats, the finish
after every write, retries after `5xx`, stopping after `401`, the `Probe-Client` header, and
joining `PROBE_RUN_ID`. `metrics` also fails a write without `X-Probe-Async-Ack: 1` or one sent
again after its `202`, and `finish` a client that gives up on the `data_pending` `503` or sends
the finish again sooner than its `Retry-After`. Exit 0 means every rule passes.

Which rules apply depends on the client. A client that records only runs and metrics (a training
loop) sends no agent call: its `traces` rule reads `not used` and passes, and it needs no trace
code and no `413` split. A client that sends even one agent call is judged by `traces` in full
(the call of 2 or more spans, the `413`, the split, parents first). Every other rule applies to
both kinds. Fix every failure before the program ships.

<!-- BEGIN GENERATED by tools/codegen/gen_client_protocol.py from schema/openapi.json: `make gen-client-protocol` -->
## ENDPOINTS (GENERATED)

### `GET /v1/projects`: find the project by its slug (a list of 0 or 1)

Query:

| field | type | required | limits |
|---|---|---|---|
| `slug` | string | no | exact slug; returns 0 or 1 row (slug is unique per tenant) |

Answer `200`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `slug` | string | yes |  |
| `name` | string | yes |  |

### `POST /v1/projects`: create it when none has the slug

Body:

| field | type | required | limits |
|---|---|---|---|
| `slug` | string | yes |  |
| `kind` | `training` \| `evaluation` \| `research` \| `general` \| `experiment` | yes |  |
| `description` | string | no | max length 2000 |

Answer `201`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `slug` | string | yes |  |

### `POST /v1/projects/{project_id}/runs`: create the run, once

Body:

| field | type | required | limits |
|---|---|---|---|
| `creation_key` | string | no | min length 16; max length 64; pattern `^[A-Za-z0-9_-]+$` |
| `name` | string | no | min length 1 |
| `description` | string | no |  |
| `config` | object | no |  |
| `tags` | array of string | no |  |
| `heartbeat` | boolean | no | default `false` |
| `source` | string | no | default `"api"` |

Answer `201`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `slug` | string | no |  |
| `name` | string | yes |  |
| `status` | `created` \| `running` \| `completed` \| `failed` \| `crashed` \| `canceled` \| `untracked` | yes |  |

### `POST /v1/runs/{run_ref}/heartbeat`: say the run's process is alive

Query:

| field | type | required | limits |
|---|---|---|---|
| `attached` | boolean | no | default `false` |

Answer `200`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `status` | `created` \| `running` \| `completed` \| `failed` \| `crashed` \| `canceled` \| `untracked` | yes |  |

### `POST /v1/runs/{run_ref}/metrics`: record metric points

Body:

| field | type | required | limits |
|---|---|---|---|
| `points` | array of `MetricPointIn` | yes | max items 50000 |

Each `MetricPointIn`:

| field | type | required | limits |
|---|---|---|---|
| `key` | string | yes |  |
| `value` | number | yes |  |
| `step_index` | integer | no |  |
| `wall_clock` | string (date-time) | no |  |

Answer `201`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `inserted` | integer | yes |  |
| `deduped` | integer | no | default `0` |

### `POST /v1/runs/{run_ref}/traces`: record agent calls, one trial each

Body:

| field | type | required | limits |
|---|---|---|---|
| `source` | `mastra` \| `generic` | yes |  |
| `source_version` | string | no | max length 64 |
| `spans` | array of object | yes |  |

Answer `200`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `upserted` | integer | yes |  |
| `trial_ids` | array of string (uuid) | yes |  |

### `PATCH /v1/runs/{run_ref}`: finish the run

Body:

| field | type | required | limits |
|---|---|---|---|
| `status` | `created` \| `running` \| `completed` \| `failed` \| `crashed` \| `canceled` \| `untracked` | no |  |
| `ended_at` | string (date-time) | no |  |
| `summary_metrics` | object | no |  |

Answer `200`, of which you need:

| field | type | required | limits |
|---|---|---|---|
| `status` | `created` \| `running` \| `completed` \| `failed` \| `crashed` \| `canceled` \| `untracked` | yes |  |

### Trace limits

- Header: `Probe-Client: <language>/1` (protocol version 1).
- At most 1,000 spans and 4 MiB of body per request; past either the answer is `413`.

### A `generic` span

| field | type | required | limits |
|---|---|---|---|
| `trace_id` | string | yes | min length 1; max length 128 |
| `span_id` | string | yes | min length 1; max length 128 |
| `parent_span_id` | string | no | max length 128 |
| `span_type` | `rollout` \| `turn` \| `tool_call` | yes |  |
| `name` | string | no | max length 512 |
| `status` | `running` \| `completed` \| `failed` | no | default `"completed"` |
| `started_at` | string (date-time) | no |  |
| `ended_at` | string (date-time) | no |  |
| `attributes` | object | no |  |

<!-- END GENERATED -->
