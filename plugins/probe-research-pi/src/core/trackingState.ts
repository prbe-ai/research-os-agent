/**
 * Pi's host bridge to the canonical Python tracking resolver.
 *
 * Folder inheritance and atomic signal publication stay in one place: the
 * probe CLI. Pi asks the hidden `session initialize` command for the durable
 * state and only renders what that command returns.
 */

import { type PathEnv } from "./paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./probeBinary.js";

/**
 * Is a capture daemon live for this session, and if not, why not.
 *
 * `reason` comes from the CLI's own closed vocabulary (`probe session
 * status`'s `capture.reason`) and is rendered verbatim — this side never
 * invents or rewords one, so the footer and the CLI cannot describe the same
 * situation differently.
 */
export interface CaptureReading {
  running: boolean;
  reason: string;
}

/** The switch's four stored states. Mirrors `session_marker.STATES`. */
export const ProbeState = {
  Full: "full",
  Daemon: "daemon",
  ReadOnly: "read-only",
  Off: "off",
} as const;
export type ProbeStateValue = (typeof ProbeState)[keyof typeof ProbeState];

/**
 * Who records this session: the coding agent, or the Probe daemon (the
 * wizard's "Who records"). Mirrors `session_marker.RECORDERS`.
 *
 * The CLI owns it (`<sid>.profile`, refreshed from `defaults.recorders.pi` on
 * every `session initialize`) and reports it as `profile`. In the daemon
 * profile this extension switches itself to the lean set: no MCP bridge, the
 * daemon's own skills (`DAEMON_OWN_SKILLS`), the guard's allowlist. See
 * `profile.ts`.
 */
export const Recorder = {
  Agent: "agent",
  Daemon: "daemon",
} as const;
export type RecorderValue = (typeof Recorder)[keyof typeof Recorder];

export interface TrackingState {
  tracking: boolean;
  signal: "on" | "off";
  /**
   * The three-valued switch. Optional because the CLI grew it after this
   * interface shipped, and a pi running against an older probe must still
   * render: absent means "this CLI cannot say", not "full". `tracking` and
   * `signal` keep their two-valued meaning forever for exactly this reason --
   * `read-only` and `off` both project to `off`, which is true of both.
   */
  state?: ProbeStateValue;
  /**
   * Never absent: a CLI that predates the field is one that cannot put pi on
   * the daemon, so its sessions are the agent's -- see `parseProfile`.
   */
  profile: RecorderValue;
  seeded: boolean;
  source: string;
  /**
   * Absent when the probe CLI on this machine predates the `capture` block
   * (`session initialize` grew it alongside the third state). Absent is not
   * "not capturing" — it is "this CLI cannot say" — which is why it is
   * optional rather than defaulted, and why the footer falls back to the
   * plain two-state text when it is missing.
   */
  capture?: CaptureReading;
  /**
   * The researcher's one line when this session started under the retired
   * per-folder config file (removed 2026-11-04), absent from a CLI that
   * predates it. Given ONCE: the CLI marked it given when it returned it.
   */
  retiredConfigNotice?: string;
  /**
   * What the agent turned down on this computer and is down still, as the status
   * line words it (`recording_lowered[0].status`, e.g. "Probe: new chats off";
   * fixes-1107b PR B). Absent when nothing is, or from a CLI that predates it.
   */
  lowered?: string;
}

export interface TrackingExecOptions {
  env: PathEnv;
  encoding: "utf8";
  timeout: number;
  maxBuffer: number;
  /** No console window for the child on Windows. */
  windowsHide?: boolean;
  /** Where the child starts: a Python one never in the session's folder, whose files it would import. */
  cwd?: string;
}

export interface TrackingChild {
  kill: (signal: "SIGKILL") => unknown;
}

export type TrackingExecFileFn = (
  command: string,
  args: string[],
  options: TrackingExecOptions,
  callback: (error: Error | null, stdout: string, stderr: string) => void,
) => TrackingChild | void;

export interface TrackingStateDeps {
  /** One request to the Probe service (`serviceForward.callService` on its socket): its result, or null. */
  call: (method: string, params: Record<string, unknown>, waitMs: number) => Promise<Record<string, unknown> | null>;
  env: PathEnv;
  log: (message: string) => void;
  timeoutMs?: number;
}

const CHILD_ENV_KEYS = [
  "PATH",
  "PATHEXT",
  "HOME",
  "USERPROFILE",
  "XDG_STATE_HOME",
  "XDG_CONFIG_HOME",
  "LOCALAPPDATA",
  "APPDATA",
  "SystemRoot",
  "TMPDIR",
  "TMP",
  "TEMP",
  "PROBE_CONFIG_PATH",
  "PROBE_SESSION_TRACKING",
  // Dropping this was a silent opt-out loss: starting pi with
  // PROBE_SESSION_STATE=off left the CLI child resolving the default instead,
  // seeding `full`, and PERSISTING that -- so the override did not merely fail
  // to apply, it was overwritten by a decision nobody made.
  "PROBE_SESSION_STATE",
  // Where pi and its capture live. Dropped, the CLI read pi's capture state
  // from the default folders, so a moved pi home or tap folder showed "not
  // capturing session transcript: not installed" over a running capture.
  "PI_CODING_AGENT_DIR",
  "PROBE_PI_TAP_PLUGIN_DIR",
] as const;

/** What a `probe` child the extension starts may see: the keys above, and `PROBE_AGENT=pi`. */
export function trackingChildEnv(env: PathEnv): PathEnv {
  const child: PathEnv = { PROBE_AGENT: "pi" };
  for (const key of CHILD_ENV_KEYS) {
    if (env[key] !== undefined) child[key] = env[key];
  }
  return child;
}

/** `session.initialize`'s method name (`protocol.Method.SESSION_INITIALIZE`). */
export const SESSION_INITIALIZE = "session.initialize";

/**
 * Seed and read this session's state through the Probe service (`session.initialize`, which runs
 * `probe session initialize` with what of pi's environment it reads, `trackingChildEnv`): the
 * extension spawns no CLI (service-only task 16). Null with no service, or no reading: unknown.
 */
export async function initializeTrackingState(
  sessionId: string,
  cwd: string,
  deps: TrackingStateDeps,
): Promise<TrackingState | null> {
  const timeoutMs = deps.timeoutMs ?? 6_000;
  const env = Object.fromEntries(Object.entries(trackingChildEnv(deps.env)).filter((entry): entry is [string, string] =>
    typeof entry[1] === "string"));
  let answer: Record<string, unknown> | null;
  try {
    answer = await deps.call(SESSION_INITIALIZE, { sid: sessionId, cwd, agent: env.PROBE_AGENT, env }, timeoutMs);
  } catch (err) {
    deps.log(`tracking initialization failed: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
  const state = answer?.state;
  if (typeof state !== "object" || state === null) {
    deps.log(`tracking initialization: no reading from the Probe service${answer?.reason ? ` (${String(answer.reason)})` : ""}`);
    return null;
  }
  const result = { stdout: JSON.stringify(state) };

  try {
    const value = JSON.parse(result.stdout) as Record<string, unknown>;
    if (
      value.session_id !== sessionId ||
      typeof value.tracking !== "boolean" ||
      (value.signal !== "on" && value.signal !== "off") ||
      value.tracking !== (value.signal === "on") ||
      typeof value.seeded !== "boolean" ||
      typeof value.source !== "string"
    ) {
      throw new Error("invalid tracking state payload");
    }
    return {
      tracking: value.tracking,
      signal: value.signal,
      state: parseState(value.state),
      profile: parseProfile(value.profile),
      seeded: value.seeded,
      source: value.source,
      capture: parseCapture(value.capture),
      retiredConfigNotice:
        typeof value.retired_config_notice === "string" && value.retired_config_notice
          ? value.retired_config_notice
          : undefined,
      lowered: parseLowered(value.recording_lowered),
    };
  } catch (err) {
    deps.log(
      `tracking initialization returned invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  }
}

/**
 * The three-valued state, read LENIENTLY for the same reason `capture` is.
 *
 * An unrecognised or missing value is `undefined`, never a guess. The strict
 * fields above already carry a safe two-valued answer, so a reader that cannot
 * understand the third value degrades to "records nothing" rather than to a
 * claim it is not entitled to make.
 *
 * BOTH VOCABULARIES ARE ACCEPTED. The CLI prints the words a person types --
 * `on` / `read` / `off` -- while the config file and the session marker keep the
 * older `full` / `read-only` spellings, because those files are read by every
 * other copy of the client on the machine. This extension ships on its own
 * release train (see the `signal` note in `probe session status`), so it meets
 * both: a new CLI printing `on`, and an older one printing `full`.
 */
function parseState(raw: unknown): ProbeStateValue | undefined {
  if (
    raw === ProbeState.Full ||
    raw === ProbeState.Daemon ||
    raw === ProbeState.ReadOnly ||
    raw === ProbeState.Off
  ) {
    return raw;
  }
  if (raw === "on") return "full";
  if (raw === "read") return "read-only";
  return undefined;
}

/**
 * The recorder, read leniently: only an exact `daemon` is the daemon.
 *
 * Missing or unrecognised is `agent`, which is what every session was before
 * the field existed. The direction is the safe one: the agent profile is
 * today's full extension, so a CLI this side cannot understand costs the lean
 * set, never the agent's tools.
 */
export function parseProfile(raw: unknown): RecorderValue {
  return raw === Recorder.Daemon ? Recorder.Daemon : Recorder.Agent;
}

/**
 * The optional half of the payload, read LENIENTLY on purpose.
 *
 * Every other field above is validated strictly and a failure discards the
 * whole read — those fields are the tracking signal itself, and rendering a
 * guess about that is the one thing this module must never do. `capture` is
 * different: it arrived later than the CLI contract around it, so a probe
 * that does not send it, or sends a shape this version does not recognise,
 * must still leave the tracking state renderable. Unrecognised means absent,
 * never `{running: false}` — "not capturing session transcript" is a claim,
 * and this side is not entitled to make it on the strength of a field it
 * could not read.
 */
function parseCapture(raw: unknown): CaptureReading | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const { running, reason } = raw as { running?: unknown; reason?: unknown };
  if (typeof running !== "boolean" || typeof reason !== "string") return undefined;
  return { running, reason };
}

/**
 * The footer, in pi's idiom: only the five states the status line shows (PR32,
 * Richard 2026-11-06), never the old `tracking` word.
 *
 * Capture is never mentioned when nothing records: it may still be running,
 * and read only is the researcher's decision, not a capture report.
 */
/** The footer's word for a recording session; mirrors `session_marker._recording_labels`. */
export const RecordingLabel = {
  Daemon: "on (daemon)",
  DaemonDegraded: "on (daemon degraded)",
  Inline: "on (inline)",
} as const;

/** The footer's word for a read-only session; mirrors `session_marker`'s `read-only` displays. */
export const ReadLabel = {
  Daemon: "read only (daemon)",
  Inline: "read only (inline)",
} as const;

/** The newest lowering's status words (`recording_lowered[].status`), read leniently: anything else is none. */
export function parseLowered(value: unknown): string | undefined {
  if (!Array.isArray(value)) return undefined;
  for (const entry of value) {
    const status = entry && typeof entry === "object" ? (entry as { status?: unknown }).status : undefined;
    if (typeof status === "string" && status && !/[\r\n]/.test(status)) return status;
  }
  return undefined;
}

/** The footer's ` · new chats off` for `lowered` (its words less "Probe: "), else "". */
export function loweredSuffix(lowered?: string): string {
  if (!lowered) return "";
  return ` · ${lowered.startsWith("Probe: ") ? lowered.slice("Probe: ".length) : lowered}`;
}

/**
 * `daemonLive` is undefined outside the `daemon` state, where the agent records:
 * `on (inline)`. Inside it the footer names the switch's position: `on (daemon)`
 * while the daemon holds a live lease, `on (daemon degraded)` while the agent has
 * recording back. With capture down the suffix already explains a daemon that
 * cannot run (it is capture's child), so that line keeps the shorter label.
 * `daemonReads`: a read-only session whose reader is the daemon's.
 */
export function trackingStatusText(
  tracking: boolean,
  capture?: CaptureReading,
  daemonLive?: boolean,
  inline = false,
  daemonReads = false,
): string {
  // An inline session records (the agent does) even while a cached reading from
  // before `/probe inline` still says `read`.
  if (!tracking && !inline) return `○ ${daemonReads ? ReadLabel.Daemon : ReadLabel.Inline}`;
  const captureDown = capture !== undefined && !capture.running;
  const label =
    inline || daemonLive === undefined
      ? RecordingLabel.Inline
      : daemonLive || captureDown
        ? RecordingLabel.Daemon
        : RecordingLabel.DaemonDegraded;
  if (!capture || capture.running) return `● ${label}`;
  return `◐ ${label} · not capturing session transcript: ${capture.reason}`;
}
