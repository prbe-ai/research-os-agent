/**
 * THE SHIM: what pi loads once, and what lets an open pi session take a new
 * Probe version at its next turn with no `/reload`.
 *
 * pi binds an extension's handlers, commands and tools when it loads the
 * extension (at start and at `/reload`), so a session used to run the Probe
 * version it started on until someone typed `/reload`. Now `index.ts` and this
 * module are the ONLY code that calls pi's registration API (`on`,
 * `registerCommand`, `registerTool`, `setActiveTools`). They register every
 * event, command and tool once, each forwarding BY NAME to the ACTIVE
 * implementation (`src/impl.ts` of some version), and at a turn boundary
 * (`session_start`, `before_agent_start`; never mid tool call or mid stream)
 * switch the active implementation to the version the Probe CLI pinned last
 * (`<state>/pi/current`, see "On disk" below).
 *
 * FROZEN. A pi process keeps the shim it loaded until `/reload` or a restart,
 * and runs later versions' implementations through it. So this file (and
 * `index.ts`) change only together with `SHIM_API`: an implementation declares
 * the `SHIM_API` it needs (`REQUIRES_SHIM_API`), and a shim older than that
 * keeps its implementation and asks the researcher, once, to run `/reload`.
 * This module imports nothing of the implementation's: only node and
 * `./core/paths.js` (read once, at load).
 *
 * THE CONTRACT WITH AN IMPLEMENTATION (`ImplModule`): `prepare(env)` builds a
 * `Candidate` with no side effect (no pi call, no timer, no child process);
 * `activate()` / `deactivate()` start and stop what it runs; `declarations`
 * name its event handlers, commands and tools; `status()` says whether the
 * Probe service records the session. The implementation never registers
 * anything itself: it gets a `ShimApi` (pi's action methods, plus
 * `declarationsChanged()`), and the shim applies the difference.
 *
 * SHARED STATE outlives a version: the shim owns `{schema, session,
 * transports}` and hands each new implementation a COPY of its two records
 * (`copyShared`). The values inside are the same objects, on purpose: a
 * deactivated implementation's in-flight work (a socket lookup, a capture
 * start awaiting its confirmation) still lands where the new one reads it. So
 * an implementation keeps its session state in container records, never as
 * bare values on `session`, and a migration REPLACES a field, never mutates
 * one. MCP keeps only its connections in `transports` -- never a closure --
 * and the new implementation builds its tools over them.
 *
 * A SWITCH IS A TRANSACTION. import the pinned `impl.ts` -> check
 * `REQUIRES_SHIM_API` and the state schema -> `prepare` on a copy -> only then
 * `old.deactivate()` -> apply the declaration difference -> `new.activate()`
 * -> commit. A failure before `deactivate` leaves everything as it was; one
 * after it re-applies the old declarations and calls `old.activate()` on the
 * untouched shared state. Either way the version is recorded in `bad_pins` and
 * never retried by this process. Nothing here ever throws into pi.
 *
 * ON DISK (shared with the Probe CLI's Python, `<state>` = `probeStateDir`):
 *
 *   <state>/pi/pins/<pin>/            a copy of the pi clone's layout:
 *       plugins/probe-research-pi/...   (this package, with src/impl.ts)
 *       plugins/probe-research-tap/...  (and the other sibling plugins)
 *       node_modules/...  package.json
 *       .pinned                         present only once the copy is complete
 *   <state>/pi/current                JSON {"pin": "<pins/ folder name>",
 *                                     "version": "<pi package version>"},
 *                                     replaced atomically by the CLI
 *   <state>/pi/live/<pid>.json        one per pi process, written by THIS
 *                                     module: `LiveRecord` below
 *
 * Read `LiveRecord` for the field-by-field contract the CLI reads.
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { extensionLogFile, probeStateDir, type PathEnv } from "./core/paths.js";

/** The contract version this shim offers an implementation. */
export const SHIM_API = 1;

/** A fresh shared state's schema: no implementation has written it yet. */
export const EMPTY_STATE_SCHEMA = 0;

/** The events the shim registers once and forwards by name. */
export const ShimEvent = {
  SessionStart: "session_start",
  SessionShutdown: "session_shutdown",
  ResourcesDiscover: "resources_discover",
  BeforeAgentStart: "before_agent_start",
  ToolCall: "tool_call",
  AgentSettled: "agent_settled",
  SessionCompact: "session_compact",
  Input: "input",
} as const;
export type ShimEventName = (typeof ShimEvent)[keyof typeof ShimEvent];
export const SHIM_EVENTS: readonly ShimEventName[] = Object.values(ShimEvent);

/** The turn boundaries: the only events a version switch happens at. */
export const SWITCH_EVENTS: ReadonlySet<string> = new Set<string>([ShimEvent.SessionStart, ShimEvent.BeforeAgentStart]);

/** pi's `session_shutdown` reason when the process is quitting. */
const SHUTDOWN_QUIT = "quit";

// ---------------------------------------------------------------------------
// The on-disk contract
// ---------------------------------------------------------------------------

/** Under `probeStateDir`. */
export const PI_STATE_DIRNAME = "pi"; // harness-literal-ok: the state folder probe.cli.pi_pins writes
export const PINS_DIRNAME = "pins";
export const CURRENT_FILENAME = "current";
export const LIVE_DIRNAME = "live";
/** A pin is complete, and may be loaded, only once this file is in it. */
export const PINNED_MARKER = ".pinned";
/** Where this package sits inside a pin (and inside the pi clone). */
export const PIN_PACKAGE_PATH: readonly string[] = ["plugins", "probe-research-pi"];
/** The implementation's entry inside the package: what the shim imports. */
export const IMPL_ENTRY: readonly string[] = ["src", "impl.ts"];

/**
 * `<state>/pi/live/<pid>.json`: what one pi process runs, kept current by its
 * shim (rewritten by an atomic rename whenever a field below changes;
 * `updated` alone never causes a write) and deleted when pi quits
 * (`session_shutdown` with reason "quit") or the process exits.
 *
 * The Probe CLI reads it to say "no restart needed" only when that is true,
 * and to never prune a pin a live process still needs.
 */
export interface LiveRecord {
  /** `SHIM_API` of the shim this process loaded. */
  shim_api: number;
  /** The shared state's schema now (the active implementation's `STATE_SCHEMA`). */
  state_schema: number;
  /** The pin the active implementation runs from; null when it runs from the pi clone itself. */
  pin: string | null;
  /**
   * The other pin this process may still read files from, so never pruned
   * while the record lives: the pin pi loaded this session's skill and
   * resource paths from, when that is not `pin` (pi keeps those paths until
   * `/reload`); otherwise the pin of the implementation the active one
   * replaced. Null when that is the clone, or there is none.
   */
  fallback_pin: string | null;
  /** The pin pi loaded this session's skills and resources from (null: the clone), which only `/reload` changes. */
  resources_pin: string | null;
  /** Every pin this process has run (a replaced version may still own a watcher or an MCP client): never pruned while it lives. */
  pins_used: string[];
  /** The clone this process loaded the package from (`installOf`): `current` serves it only when made from it. */
  install: string;
  /** Pins this process tried and will not try again (import, prepare or activation failed). */
  bad_pins: string[];
  /** The session is recorded by the Probe service (its `.hosted` hand-over happened). */
  recorded: boolean;
  /** The active implementation exposes other skills/resources than pi loaded: run `/reload` to see them. */
  skills_reload_needed: boolean;
  /** `current` names a version that needs a newer shim than this one: only `/reload` (or a restart) applies it. */
  shim_reload_needed: boolean;
  /** The active implementation's package version (its `package.json`). */
  version: string;
  /** When this shim started, epoch seconds. */
  started: number;
  /** When the record was last written, epoch seconds. */
  updated: number;
}

export function piStateDir(env: PathEnv = process.env): string {
  return join(probeStateDir(env), PI_STATE_DIRNAME);
}

export function pinsDir(env: PathEnv = process.env): string {
  return join(piStateDir(env), PINS_DIRNAME);
}

export function currentFile(env: PathEnv = process.env): string {
  return join(piStateDir(env), CURRENT_FILENAME);
}

export function liveDir(env: PathEnv = process.env): string {
  return join(piStateDir(env), LIVE_DIRNAME);
}

export function liveRecordFile(pid: number, env: PathEnv = process.env): string {
  return join(liveDir(env), `${pid}.json`);
}

/** The package directory inside a pin: the implementation's `resourceRoot`. */
export function pinPackageDir(pin: string, env: PathEnv = process.env): string {
  return join(pinsDir(env), pin, ...PIN_PACKAGE_PATH);
}

/** A `pins/` folder name: one path segment, nothing that climbs out. */
const PIN_NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;

export function isPinName(value: unknown): value is string {
  return typeof value === "string" && PIN_NAME.test(value) && !value.includes("..");
}

/** `current`'s content, or null when it names nothing usable. */
export interface CurrentPin {
  pin: string;
  version: string | null;
  /** The pi clone the pin was made from (`probe.cli.pi_pins`): a pin serves only the install it came from. */
  from: string | null;
  /** That clone's git commit when the pin was made (null: none could be read). */
  head: string | null;
}

/** The folder a path names, links resolved when it exists. */
function canonical(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

/**
 * Was `current` made from the install this shim was loaded from? The package
 * sits at `<clone>/plugins/probe-research-pi` (`installRoot`) and the pin
 * names its clone (`from`): a pi whose package entry now names another
 * install (a local checkout) never runs the mirror's pins.
 */
export function pinServesInstall(current: CurrentPin, installRoot: string): boolean {
  return current.from !== null && canonical(current.from) === installOf(installRoot);
}

/** The clone a package folder (`<clone>/plugins/probe-research-pi`) belongs to. */
export function installOf(installRoot: string): string {
  return canonical(dirname(dirname(installRoot)));
}

/** A git checkout's commit (`HEAD`, through its ref or `packed-refs`), or null. */
export function gitHead(clone: string): string | null {
  const git = join(clone, ".git");
  try {
    const head = readFileSync(join(git, "HEAD"), "utf-8").trim();
    if (!head.startsWith("ref: ")) return head || null;
    const ref = head.slice(5).trim();
    try {
      return readFileSync(join(git, ref), "utf-8").trim() || null;
    } catch {
      for (const line of readFileSync(join(git, "packed-refs"), "utf-8").split("\n")) {
        const [sha, name] = line.split(" ");
        if (name?.trim() === ref) return sha ?? null;
      }
    }
  } catch {
    return null;
  }
  return null;
}

/**
 * Is `current` the version the clone holds now? A clone moved by hand (`pi
 * update` with Probe's own update off) is newer than the pin: a session then
 * runs the clone's code, whose skills pi listed, until the CLI pins it.
 */
export function pinIsClone(current: CurrentPin, installRoot: string): boolean {
  return current.version === readVersion(installRoot) && current.head === gitHead(installOf(installRoot));
}

export function parseCurrent(text: string): CurrentPin | null {
  try {
    const data: unknown = JSON.parse(text);
    if (typeof data !== "object" || data === null) return null;
    const { pin, version, from, head } = data as Record<string, unknown>;
    if (!isPinName(pin)) return null;
    return {
      pin,
      version: typeof version === "string" ? version : null,
      from: typeof from === "string" ? from : null,
      head: typeof head === "string" ? head : null,
    };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// The implementation contract
// ---------------------------------------------------------------------------

export type EventHandler = (event: any, ctx: any) => unknown;

export interface CommandDeclaration {
  description?: string;
  handler: (args: string, ctx: any) => Promise<void> | void;
}

/**
 * A pi tool, as `pi.registerTool` takes it. Its function-valued fields
 * (`execute`, ...) are called through the shim, on whichever implementation is
 * active when pi calls them; the rest is its descriptor, and a change there
 * re-registers the tool.
 */
export interface ToolDeclaration {
  name: string;
  label: string;
  description: string;
  parameters: object;
  execute: (...args: any[]) => Promise<unknown>;
}

export interface Declarations {
  /** Handlers by event name. The shim registers every `SHIM_EVENTS` name regardless, and any other declared name late. */
  readonly events: Readonly<Record<string, EventHandler>>;
  /** Slash commands by name. */
  readonly commands: Readonly<Record<string, CommandDeclaration>>;
  /** The tools now; read again whenever the implementation calls `declarationsChanged()`. */
  tools(): readonly ToolDeclaration[];
}

/** The state that outlives an implementation version. See the module docstring. */
export interface SharedState {
  schema: number;
  session: Record<string, unknown>;
  transports: Record<string, unknown>;
}

export function emptySharedState(): SharedState {
  return { schema: EMPTY_STATE_SCHEMA, session: {}, transports: {} };
}

/** A new `{schema, session, transports}` whose two records are new objects holding the same values. */
export function copyShared(shared: SharedState): SharedState {
  return { schema: shared.schema, session: { ...shared.session }, transports: { ...shared.transports } };
}

/** What an implementation may ask of pi, on whichever pi runtime is current. */
export interface ShimApi {
  sendMessage: ExtensionAPI["sendMessage"];
  getActiveTools(): string[];
  setActiveTools(toolNames: string[]): void;
  /** The implementation's tools changed (`Declarations.tools()`): the shim applies them now. */
  declarationsChanged(): void;
}

export interface ImplEnv {
  api: ShimApi;
  /** A copy of the shim's shared state: `prepare` migrates it and hands it back as `Candidate.shared`. */
  shared: SharedState;
  /** Where pi loaded `index.ts` from (the pi clone): package identity, for opt-outs and adapter detection. */
  installRoot: string;
  /** Where this implementation's own files are: the pin's package folder, or `installRoot`. */
  resourceRoot: string;
}

export interface ImplStatus {
  /** The session is recorded by the Probe service. */
  recorded: boolean;
}

export interface Candidate {
  /** The shared state this implementation runs on; the shim commits it once activation succeeds. */
  readonly shared: SharedState;
  readonly declarations: Declarations;
  activate(): void | Promise<void>;
  deactivate(): void | Promise<void>;
  status(): ImplStatus;
}

export interface ImplModule {
  REQUIRES_SHIM_API: number;
  STATE_SCHEMA: number;
  MIGRATES_FROM: readonly number[];
  RESOURCES_FINGERPRINT: string;
  prepare(env: ImplEnv): Candidate | Promise<Candidate>;
}

/** The `code` of the error `prepare` throws for a state schema it cannot migrate. */
export const UNSUPPORTED_STATE_SCHEMA = "PROBE_PI_UNSUPPORTED_STATE_SCHEMA";

export class UnsupportedStateSchemaError extends Error {
  readonly code = UNSUPPORTED_STATE_SCHEMA;
  constructor(
    readonly found: number,
    readonly supported: readonly number[],
  ) {
    super(`shared state schema ${found} is not one this implementation runs on or migrates from (${supported.join(", ")})`);
    this.name = "UnsupportedStateSchemaError";
  }
}

/** By `code`, not `instanceof`: each version's module has its own class. */
export function isUnsupportedStateSchema(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: unknown }).code === UNSUPPORTED_STATE_SCHEMA;
}

/** Can an implementation declaring `schema` / `migratesFrom` run on state of `found`? */
export function supportsSchema(found: number, schema: number, migratesFrom: readonly number[]): boolean {
  return found === EMPTY_STATE_SCHEMA || found === schema || migratesFrom.includes(found);
}

/** The module's exports, checked: an `ImplModule`, or why it is not one. */
export function asImplModule(value: unknown): ImplModule {
  const mod = value as Partial<ImplModule> | null;
  if (!mod || typeof mod !== "object") throw new Error("the implementation module exported nothing");
  if (typeof mod.prepare !== "function") throw new Error("the implementation exports no prepare()");
  if (!Number.isInteger(mod.REQUIRES_SHIM_API)) throw new Error("the implementation exports no REQUIRES_SHIM_API");
  if (!Number.isInteger(mod.STATE_SCHEMA)) throw new Error("the implementation exports no STATE_SCHEMA");
  if (!Array.isArray(mod.MIGRATES_FROM)) throw new Error("the implementation exports no MIGRATES_FROM");
  if (typeof mod.RESOURCES_FINGERPRINT !== "string") throw new Error("the implementation exports no RESOURCES_FINGERPRINT");
  return mod as ImplModule;
}

function checkCandidate(candidate: Candidate): Candidate {
  const ok =
    candidate &&
    typeof candidate.activate === "function" &&
    typeof candidate.deactivate === "function" &&
    typeof candidate.status === "function" &&
    candidate.shared &&
    candidate.declarations &&
    typeof candidate.declarations.tools === "function";
  if (!ok) throw new Error("prepare() returned something that is not a candidate");
  return candidate;
}

// ---------------------------------------------------------------------------
// The host
// ---------------------------------------------------------------------------

export interface ShimOptions {
  /** The directory pi loaded `index.ts` from: the pi clone's package folder. */
  installRoot: string;
  /** The implementation beside the shim (`installRoot/src/impl.ts`), loaded with it: used whenever no pin can be. */
  cloneImpl: unknown;
  /** Where `probeStateDir` is read from. Defaults to `process.env`. */
  env?: PathEnv;
  /** Loads a pinned `impl.ts`. Defaults to a dynamic `import()` (jiti's, under pi). */
  importImpl?: (path: string) => Promise<unknown>;
  pid?: number;
  /** Epoch milliseconds. */
  now?: () => number;
  /** The extension log. Defaults to `extensionLogFile`. */
  log?: (line: string) => void;
  /** Start from this shared state (a test's stand-in for one pi process); a fresh one otherwise. */
  shared?: SharedState;
}

interface Active {
  pin: string | null;
  candidate: Candidate;
  resourceRoot: string;
  version: string;
  fingerprint: string;
}

/** What one pi runtime (`ExtensionAPI` instance) has been given. */
interface Bound {
  pi: ExtensionAPI;
  events: Set<string>;
  /** Command name -> its description as registered. */
  commands: Map<string, string>;
  /** Tool name -> its descriptor as registered. */
  tools: Map<string, string>;
  /** Tools the shim took out of pi's active set because no implementation declares them now. */
  hidden: Set<string>;
}

const UNKNOWN_VERSION = "0.0.0";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function readVersion(packageDir: string): string {
  try {
    const version = (JSON.parse(readFileSync(join(packageDir, "package.json"), "utf-8")) as { version?: unknown }).version;
    return typeof version === "string" && version ? version : UNKNOWN_VERSION;
  } catch {
    return UNKNOWN_VERSION;
  }
}

/** A tool's descriptor: everything but its functions, plus which functions it has. */
function descriptorKey(tool: ToolDeclaration): string {
  const plain: Record<string, unknown> = {};
  const functions: string[] = [];
  for (const [key, value] of Object.entries(tool as object)) {
    if (typeof value === "function") functions.push(key);
    else plain[key] = value;
  }
  try {
    return JSON.stringify({ plain, functions: functions.sort() });
  } catch {
    return `${tool.name}:${Math.random()}`; // not serialisable: always re-register
  }
}

/**
 * The files `process.on("exit")` removes. One set per process, on
 * `globalThis`: pi evaluates this module again at every `/reload`, and one exit
 * listener must serve every evaluation.
 */
const LIVE_FILES_KEY = Symbol.for("probe-research-pi.shim.live-files");

function liveFilesAtExit(): Set<string> {
  const holder = globalThis as unknown as Record<symbol, Set<string> | undefined>;
  let files = holder[LIVE_FILES_KEY];
  if (!files) {
    const created = new Set<string>();
    files = created;
    holder[LIVE_FILES_KEY] = created;
    process.on("exit", () => {
      for (const file of created) {
        try {
          rmSync(file, { force: true });
        } catch {
          // exiting anyway
        }
      }
    });
  }
  return files;
}

export class ShimHost {
  readonly installRoot: string;
  private readonly env: PathEnv;
  private readonly importImpl: (path: string) => Promise<unknown>;
  private readonly pid: number;
  private readonly now: () => number;
  private readonly logSink: (line: string) => void;
  private readonly cloneImpl: unknown;

  private shared: SharedState;
  private active: Active | null = null;
  private bound: Bound | null = null;
  private booted = false;
  /** The pin of the implementation the active one replaced (null: the clone, or none). */
  private previousPin: string | null = null;
  /** The pin pi loaded this runtime's skill/resource paths from (null: the clone). */
  private resourcesPin: string | null = null;
  /** Every pin this process has run or read resources from: none is pruned while it lives. */
  private readonly pinsUsed = new Set<string>();
  /** The fingerprint of the skills/resources pi loaded for this runtime. */
  private loadedFingerprint: string | null = null;
  private readonly badPins = new Set<string>();
  /** Pins that need a newer shim: kept apart from `badPins`, they are not broken. */
  private readonly tooNewPins = new Set<string>();
  private skillsReloadNeeded = false;
  private shimReloadNeeded = false;
  private reloadNoticePending = false;
  private reloadNoticeShown = false;
  private switching = false;
  /** The candidate a switch is activating: its `declarationsChanged` counts before it is committed. */
  private activating: Candidate | null = null;
  private currentStamp: { mtimeMs: number; size: number; ino: number } | null = null;
  private wanted: CurrentPin | null = null;
  private readonly started: number;
  private lastRecord: string | null = null;
  private stopped = false;

  constructor(options: ShimOptions) {
    this.installRoot = options.installRoot;
    this.env = options.env ?? process.env;
    this.importImpl = options.importImpl ?? ((path) => import(pathToFileURL(path).href));
    this.pid = options.pid ?? process.pid;
    this.now = options.now ?? (() => Date.now());
    this.logSink = options.log ?? ((line) => this.appendLog(line));
    this.cloneImpl = options.cloneImpl;
    this.shared = options.shared ?? emptySharedState();
    this.started = this.now() / 1000;
  }

  /** The shared state as committed now. */
  get sharedState(): SharedState {
    return this.shared;
  }

  /** The pin the active implementation runs from (null: the clone; undefined: none runs). */
  get activePin(): string | null | undefined {
    return this.active ? this.active.pin : undefined;
  }

  /**
   * Bind a pi runtime: at load, and again whenever pi builds a new runtime for
   * a session switch (`new`, `resume`, `fork`), which calls the extension's
   * factory again with a fresh `ExtensionAPI`. The first call also starts an
   * implementation. Never throws.
   */
  async attach(pi: ExtensionAPI): Promise<void> {
    try {
      // The events first, before anything awaits: they forward to whichever
      // implementation is active when pi emits, none until one starts.
      this.bound = { pi, events: new Set(), commands: new Map(), tools: new Map(), hidden: new Set() };
      for (const name of SHIM_EVENTS) this.bindEvent(name);
      if (!this.booted) {
        this.booted = true;
        await this.bootstrap();
      }
      if (this.active) this.apply(this.active.candidate.declarations);
      this.writeLive();
    } catch (err) {
      this.log(`attach failed: ${message(err)}`);
    }
  }

  // -- bootstrap -------------------------------------------------------------

  /** Start on the pin `current` names when it is complete and loads; on the clone otherwise. */
  private async bootstrap(): Promise<void> {
    this.refreshWanted();
    const want = this.wanted;
    if (want && this.pinComplete(want.pin)) {
      try {
        const mod = asImplModule(await this.importImpl(this.implPath(want.pin)));
        if (mod.REQUIRES_SHIM_API > SHIM_API) {
          this.markTooNew(want.pin, mod.REQUIRES_SHIM_API);
        } else {
          await this.start(mod, want.pin);
        }
      } catch (err) {
        this.markBad(want.pin, `could not start on it: ${message(err)}`);
      }
    }
    if (!this.active) {
      try {
        await this.start(asImplModule(this.cloneImpl), null);
      } catch (err) {
        this.log(`the implementation beside the shim did not start (${message(err)}): Probe does nothing in this pi process`);
      }
    }
    if (this.active) {
      this.resourcesPin = this.active.pin;
      this.loadedFingerprint = this.active.fingerprint;
    }
  }

  /** Prepare and activate an implementation with nothing to replace. */
  private async start(mod: ImplModule, pin: string | null): Promise<void> {
    if (!supportsSchema(this.shared.schema, mod.STATE_SCHEMA, mod.MIGRATES_FROM)) {
      throw new UnsupportedStateSchemaError(this.shared.schema, [mod.STATE_SCHEMA, ...mod.MIGRATES_FROM]);
    }
    const resourceRoot = pin === null ? this.installRoot : this.pinPackageDir(pin);
    const owner: { candidate: Candidate | null } = { candidate: null };
    const candidate = checkCandidate(
      await mod.prepare({ api: this.apiFor(owner), shared: copyShared(this.shared), installRoot: this.installRoot, resourceRoot }),
    );
    owner.candidate = candidate;
    try {
      await candidate.activate();
    } catch (err) {
      // Stop whatever it started before throwing; the caller falls back.
      try {
        await candidate.deactivate();
      } catch {
        // already failing
      }
      throw err;
    }
    this.active = { pin, candidate, resourceRoot, version: readVersion(resourceRoot), fingerprint: mod.RESOURCES_FINGERPRINT };
    if (pin !== null) this.pinsUsed.add(pin);
    this.shared = candidate.shared;
    this.log(`running ${pin === null ? "the implementation beside the shim" : `pin ${pin}`} (version ${this.active.version})`);
  }

  // -- forwarding ------------------------------------------------------------

  private bindEvent(name: string): void {
    const bound = this.bound;
    if (!bound || bound.events.has(name)) return;
    bound.events.add(name);
    bound.pi.on(name as never, ((event: unknown, ctx: unknown) => this.forward(name, event, ctx)) as never);
  }

  /** One pi event: maybe switch (at a turn boundary), then the active implementation's handler. */
  private async forward(name: string, event: unknown, ctx: any): Promise<unknown> {
    try {
      this.refreshWanted();
      if (SWITCH_EVENTS.has(name)) await this.maybeSwitch();
      this.showReloadNotice(ctx);
    } catch (err) {
      this.log(`${name}: ${message(err)}`);
    }
    const handler = this.active?.candidate.declarations.events[name];
    try {
      // The implementation's own errors reach pi as they always did.
      return handler ? await handler(event, ctx) : undefined;
    } finally {
      try {
        this.afterEvent(name, event);
      } catch (err) {
        this.log(`${name}: ${message(err)}`);
      }
    }
  }

  private afterEvent(name: string, event: unknown): void {
    if (name === ShimEvent.SessionStart && this.active) {
      // pi discovers skills and resources again right after every
      // `session_start` (startup, reload, new, resume, fork), from whatever
      // implementation is active now.
      this.resourcesPin = this.active.pin;
      this.loadedFingerprint = this.active.fingerprint;
      this.skillsReloadNeeded = false;
    }
    if (name === ShimEvent.SessionShutdown && (event as { reason?: unknown } | null)?.reason === SHUTDOWN_QUIT) {
      this.removeLive();
      return;
    }
    if (name === ShimEvent.SessionStart || name === ShimEvent.BeforeAgentStart || name === ShimEvent.AgentSettled) {
      this.writeLive();
    }
  }

  private runCommand(name: string, args: string, ctx: any): Promise<void> | void {
    const command = this.active?.candidate.declarations.commands[name];
    if (!command) {
      this.log(`/${name}: the Probe version running now has no such command`);
      try {
        if (ctx?.hasUI) ctx.ui.notify(`/${name} is not part of the Probe version running now.`, "warning");
      } catch {
        // a notice is best effort
      }
      return;
    }
    return command.handler(args, ctx);
  }

  private runTool(name: string, field: string, args: unknown[]): unknown {
    const tool = this.active?.candidate.declarations.tools().find((candidate) => candidate.name === name);
    const fn = tool ? (tool as unknown as Record<string, unknown>)[field] : undefined;
    if (typeof fn !== "function") {
      throw new Error(`Probe tool "${name}" is not offered by the Probe version running now.`);
    }
    return (fn as (...a: unknown[]) => unknown).apply(tool, args);
  }

  // -- declarations ----------------------------------------------------------

  /** Make pi's registrations match `declarations`: new and changed ones registered, removed tools hidden. */
  private apply(declarations: Declarations): void {
    const bound = this.bound;
    if (!bound) return;
    const { pi } = bound;
    for (const name of Object.keys(declarations.events)) this.bindEvent(name);
    for (const [name, command] of Object.entries(declarations.commands)) {
      const key = command.description ?? "";
      if (bound.commands.get(name) === key) continue;
      pi.registerCommand(name, { description: command.description, handler: (args: string, ctx: any) => this.runCommand(name, args, ctx) } as never);
      bound.commands.set(name, key);
    }
    const declared = new Set<string>();
    const reactivate: string[] = [];
    for (const tool of declarations.tools()) {
      declared.add(tool.name);
      const key = descriptorKey(tool);
      if (bound.tools.get(tool.name) !== key) {
        const definition: Record<string, unknown> = {};
        for (const [field, value] of Object.entries(tool as object)) {
          if (typeof value !== "function") definition[field] = value;
          // `execute` is async in pi's contract: a tool gone from this version rejects, never throws.
          else if (field === "execute") definition[field] = async (...args: unknown[]) => this.runTool(tool.name, field, args);
          else definition[field] = (...args: unknown[]) => this.runTool(tool.name, field, args);
        }
        pi.registerTool(definition as never);
        bound.tools.set(tool.name, key);
      }
      if (bound.hidden.delete(tool.name)) reactivate.push(tool.name);
    }
    const removed = [...bound.tools.keys()].filter((name) => !declared.has(name) && !bound.hidden.has(name));
    if (removed.length === 0 && reactivate.length === 0) return;
    for (const name of removed) bound.hidden.add(name);
    const activeNow = pi.getActiveTools();
    const next = activeNow.filter((name) => !removed.includes(name));
    for (const name of reactivate) if (!next.includes(name)) next.push(name);
    pi.setActiveTools(next);
    if (removed.length) this.log(`hid ${removed.length} tool(s) the running version no longer offers: ${removed.join(", ")}`);
  }

  /** The `ShimApi` one implementation gets: pi's action methods, and its own `declarationsChanged`. */
  private apiFor(owner: { candidate: Candidate | null }): ShimApi {
    const pi = (): ExtensionAPI => {
      if (!this.bound) throw new Error("no pi runtime is bound yet");
      return this.bound.pi;
    };
    return {
      sendMessage: ((...args: Parameters<ExtensionAPI["sendMessage"]>) => pi().sendMessage(...args)) as ExtensionAPI["sendMessage"],
      getActiveTools: () => pi().getActiveTools(),
      setActiveTools: (toolNames) => pi().setActiveTools(toolNames),
      declarationsChanged: () => {
        // Only the active implementation's, or the one being activated: a
        // replaced implementation's late change must not hide the new tools.
        const candidate = owner.candidate;
        if (!candidate || (this.active?.candidate !== candidate && this.activating !== candidate)) return;
        try {
          this.apply(candidate.declarations);
        } catch (err) {
          this.log(`applying the implementation's tools failed: ${message(err)}`);
          if (this.activating === candidate) throw err;
        }
      },
    };
  }

  // -- switching -------------------------------------------------------------

  /** `current`, re-read only when the file changed (one stat per event). */
  private refreshWanted(): void {
    const path = currentFile(this.env);
    let stamp: { mtimeMs: number; size: number; ino: number };
    try {
      const stat = statSync(path);
      stamp = { mtimeMs: stat.mtimeMs, size: stat.size, ino: stat.ino };
    } catch {
      this.currentStamp = null;
      this.wanted = null;
      return;
    }
    const cached = this.currentStamp;
    if (cached && cached.mtimeMs === stamp.mtimeMs && cached.size === stamp.size && cached.ino === stamp.ino) return;
    this.currentStamp = stamp;
    try {
      this.wanted = parseCurrent(readFileSync(path, "utf-8"));
    } catch {
      this.wanted = null;
    }
    if (this.wanted && !pinServesInstall(this.wanted, this.installRoot)) {
      this.log(`current names ${this.wanted.pin}, made from ${this.wanted.from ?? "an unknown install"}, not this one: it is not run here`);
      this.wanted = null;
    } else if (this.wanted && !pinIsClone(this.wanted, this.installRoot)) {
      this.log(`current names ${this.wanted.pin}, not the version the clone holds now: it is not run here until it is pinned`);
      this.wanted = null;
    }
  }

  private async maybeSwitch(): Promise<void> {
    const want = this.wanted;
    if (!want || this.switching) return;
    if (want.pin === this.active?.pin || this.badPins.has(want.pin) || this.tooNewPins.has(want.pin)) return;
    if (!this.pinComplete(want.pin)) return;
    this.switching = true;
    try {
      if (this.active) await this.switchTo(want.pin);
      else await this.startLate(want.pin);
    } finally {
      this.switching = false;
    }
  }

  /** Nothing runs (even the clone's implementation failed): start on the pin, with nothing to roll back to. */
  private async startLate(pin: string): Promise<void> {
    try {
      const mod = asImplModule(await this.importImpl(this.implPath(pin)));
      if (mod.REQUIRES_SHIM_API > SHIM_API) {
        this.markTooNew(pin, mod.REQUIRES_SHIM_API);
        return;
      }
      await this.start(mod, pin);
      if (this.active) this.apply(this.active.candidate.declarations);
      this.skillsReloadNeeded = true; // pi discovered no resources from it
      this.writeLive();
    } catch (err) {
      this.markBad(pin, `could not start on it: ${message(err)}`);
    }
  }

  /** The transaction (module docstring). Never throws. */
  private async switchTo(pin: string): Promise<void> {
    const old = this.active!;
    let mod: ImplModule;
    try {
      mod = asImplModule(await this.importImpl(this.implPath(pin)));
    } catch (err) {
      this.markBad(pin, `import failed: ${message(err)}`);
      return;
    }
    if (mod.REQUIRES_SHIM_API > SHIM_API) {
      this.markTooNew(pin, mod.REQUIRES_SHIM_API);
      return;
    }
    if (!supportsSchema(this.shared.schema, mod.STATE_SCHEMA, mod.MIGRATES_FROM)) {
      this.markBad(pin, `its state schema ${mod.STATE_SCHEMA} cannot take over state schema ${this.shared.schema}`);
      return;
    }
    const resourceRoot = this.pinPackageDir(pin);
    const owner: { candidate: Candidate | null } = { candidate: null };
    let candidate: Candidate;
    try {
      candidate = checkCandidate(
        await mod.prepare({ api: this.apiFor(owner), shared: copyShared(this.shared), installRoot: this.installRoot, resourceRoot }),
      );
      owner.candidate = candidate;
    } catch (err) {
      this.markBad(pin, isUnsupportedStateSchema(err) ? `state schema: ${message(err)}` : `prepare failed: ${message(err)}`);
      return;
    }

    // From here the old implementation stops; any failure rolls back to it.
    let activeBefore: string[] | null = null;
    try {
      activeBefore = this.bound ? this.bound.pi.getActiveTools() : null;
    } catch {
      activeBefore = null;
    }
    try {
      await old.candidate.deactivate();
      this.activating = candidate;
      this.apply(candidate.declarations);
      await candidate.activate();
    } catch (err) {
      this.activating = null;
      await this.rollback(old, candidate, activeBefore);
      this.markBad(pin, `activation failed, kept the running version: ${message(err)}`);
      return;
    }
    this.activating = null;
    this.previousPin = old.pin;
    this.active = { pin, candidate, resourceRoot, version: readVersion(resourceRoot), fingerprint: mod.RESOURCES_FINGERPRINT };
    if (pin !== null) this.pinsUsed.add(pin);
    this.shared = candidate.shared;
    if (this.active.fingerprint !== this.loadedFingerprint) this.skillsReloadNeeded = true;
    this.log(`switched ${old.pin === null ? "from the implementation beside the shim" : `from pin ${old.pin}`} to pin ${pin} (version ${this.active.version})`);
    this.writeLive();
  }

  private async rollback(old: Active, failed: Candidate, activeBefore: string[] | null): Promise<void> {
    try {
      await failed.deactivate();
    } catch (err) {
      this.log(`rollback: the failed version's deactivate threw: ${message(err)}`);
    }
    try {
      this.apply(old.candidate.declarations);
      if (activeBefore && this.bound) this.bound.pi.setActiveTools(activeBefore);
    } catch (err) {
      this.log(`rollback: re-applying the running version's tools failed: ${message(err)}`);
    }
    try {
      await old.candidate.activate();
    } catch (err) {
      this.log(`rollback: the running version's activate threw: ${message(err)}`);
    }
  }

  private markBad(pin: string, why: string): void {
    this.badPins.add(pin);
    this.log(`pin ${pin}: ${why}; not tried again in this process`);
    this.writeLive();
  }

  private markTooNew(pin: string, needs: number): void {
    this.tooNewPins.add(pin);
    this.shimReloadNeeded = true;
    this.reloadNoticePending = true;
    this.log(`pin ${pin} needs shim API ${needs}, this pi loaded ${SHIM_API}: keeping the running version until /reload`);
    this.writeLive();
  }

  /** Once per shim: the one update a person has to apply. */
  private showReloadNotice(ctx: any): void {
    if (!this.reloadNoticePending || this.reloadNoticeShown || !ctx) return;
    this.reloadNoticePending = false;
    this.reloadNoticeShown = true;
    const notice = "Probe updated; run /reload in pi to apply";
    try {
      process.stderr.write(`probe-research-pi: ${notice}\n`);
    } catch {
      // stderr is best effort
    }
    this.log(notice);
    try {
      if (ctx.hasUI) ctx.ui.notify(notice, "info");
    } catch {
      // a notice is best effort
    }
  }

  // -- pins ------------------------------------------------------------------

  private pinPackageDir(pin: string): string {
    return pinPackageDir(pin, this.env);
  }

  private implPath(pin: string): string {
    return join(this.pinPackageDir(pin), ...IMPL_ENTRY);
  }

  private pinComplete(pin: string): boolean {
    return existsSync(join(pinsDir(this.env), pin, PINNED_MARKER)) && existsSync(this.implPath(pin));
  }

  // -- the live record -------------------------------------------------------

  private record(): Omit<LiveRecord, "updated"> {
    let recorded = false;
    try {
      recorded = this.active ? this.active.candidate.status().recorded === true : false;
    } catch (err) {
      this.log(`status failed: ${message(err)}`);
    }
    const pin = this.active ? this.active.pin : null;
    const fallback = this.resourcesPin !== null && this.resourcesPin !== pin ? this.resourcesPin : this.previousPin;
    return {
      shim_api: SHIM_API,
      state_schema: this.shared.schema,
      pin,
      fallback_pin: fallback === pin ? null : fallback,
      resources_pin: this.resourcesPin,
      pins_used: [...this.pinsUsed].sort(),
      install: installOf(this.installRoot),
      bad_pins: [...this.badPins].sort(),
      recorded,
      skills_reload_needed: this.skillsReloadNeeded,
      shim_reload_needed: this.shimReloadNeeded,
      version: this.active?.version ?? UNKNOWN_VERSION,
      started: Math.round(this.started * 1000) / 1000,
    };
  }

  /** Write the record when a field changed. Never throws. */
  private writeLive(): void {
    if (this.stopped || !this.booted) return;
    try {
      const record = this.record();
      const key = JSON.stringify(record);
      if (key === this.lastRecord) return;
      const path = liveRecordFile(this.pid, this.env);
      mkdirSync(dirname(path), { recursive: true });
      const full: LiveRecord = { ...record, updated: Math.round(this.now()) / 1000 };
      const temp = `${path}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`;
      writeFileSync(temp, `${JSON.stringify(full)}\n`, { mode: 0o600 });
      renameSync(temp, path);
      this.lastRecord = key;
      liveFilesAtExit().add(path);
    } catch (err) {
      this.log(`live record not written: ${message(err)}`);
    }
  }

  private removeLive(): void {
    this.stopped = true;
    try {
      const path = liveRecordFile(this.pid, this.env);
      rmSync(path, { force: true });
      liveFilesAtExit().delete(path);
    } catch (err) {
      this.log(`live record not removed: ${message(err)}`);
    }
  }

  // -- logging ---------------------------------------------------------------

  private log(line: string): void {
    try {
      this.logSink(`shim: ${line}`);
    } catch {
      // a log line is best effort
    }
  }

  private appendLog(line: string): void {
    const file = extensionLogFile(this.env);
    mkdirSync(dirname(file), { recursive: true });
    appendFileSync(file, `[${new Date().toISOString()}] ${line}\n`);
  }
}

/** One host per evaluation of this module: pi evaluates it again only at `/reload`. */
let processHost: ShimHost | undefined;

/** `index.ts`'s whole job. Never throws into pi. */
export async function attachShim(pi: ExtensionAPI, installRoot: string, cloneImpl: unknown): Promise<void> {
  try {
    processHost ??= new ShimHost({ installRoot, cloneImpl });
    await processHost.attach(pi);
  } catch (err) {
    try {
      process.stderr.write(`probe-research-pi: the shim failed to start: ${message(err)}\n`);
    } catch {
      // nothing left to tell
    }
  }
}
