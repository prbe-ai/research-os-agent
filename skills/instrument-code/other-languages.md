# Record runs and agent calls from a language with no Probe SDK

Read this when the code to instrument is not Python (TypeScript, Rust, Go, ...). Python has the SDK:
use it there. Build a small client in the program's own language from this page, then prove it with
`probe client check`.

## THE CALLS, IN ORDER

1. Sign in (below), then find the project by its slug (lowercase words joined by `-`); create it
   when none has it. A `409` on that create means another process made it first: find it again.
2. Create the run once, with a `creation_key`.
3. Heartbeat every 60 s until the run ends.
4. Send metric points and agent calls as they happen.
5. Finish: `PATCH` the run's `status` (`completed`, `failed` or `canceled`) and `ended_at`.

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

Create the run with `heartbeat: true` and `POST .../heartbeat` every 60 s from the process doing the
work, until it finishes. A run silent for 15 minutes (the server's default) is marked `crashed`.
Never retry a beat, the one exception to the retry rule below: the next beat is its retry. A beat
answered `completed`, `failed`, `crashed` or `canceled` means the run has ended: warn once and stop
beating.

## FAILURES NEVER REACH THE PROGRAM

- `429`, `5xx` and network errors: retry in the background with jittered backoff (a random wait up
  to 1 s, 2 s, 4 s, ... capped at 60 s), never sooner than `Retry-After` when the answer has it.
  Hold at most 1,000 unsent requests; past that drop the oldest and warn once. Never block the
  program's own work on Probe.
- Any other `4xx`: drop that request and warn once per kind (`404` on `/traces`: this server or
  this token takes no traces, keep the metrics). After a `401` stop sending: the token is wrong or expired.
- At exit, flush what is queued for at most 5 s, then let the program end.
- Catch everything. The client never throws into, crashes or slows the program it records; the program's
  exit code is its own.
- Send `step_index` on every metric point, increasing per key: a point at a key and step already
  stored is dropped (first write wins, counted in `deduped`), so a retried batch is never counted
  twice, and a second value logged at the same step is lost. Skip a NaN or infinite value: JSON has
  none, and one makes the whole batch `422`.

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

Run `probe client check` (it arrives with the next CLI release): it starts a local stand-in for the
server, runs your client's own test command against it and prints pass or fail per rule, with the
reason: the run created once under retries, metrics accepted, a call accepted (and split on `413`),
heartbeats, finish, no crash on `401` or `5xx`. Fix every failure before the program ships.

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
