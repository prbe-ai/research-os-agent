/**
 * A session the Probe service records (plan A2), as pi reads it.
 *
 * Such a session has no `tap watch` of its own, so no `.pid` file: `tap record`
 * leaves a `.hosted` marker naming the service's pid instead (`paths.ts::
 * hostedFile`). Every reader that looked only for the `.pid` file put "not
 * capturing" on every session the service recorded (audit #8), and pi's
 * post-spawn wait waited its full 2 s and logged "spawn failed?" (audit #9).
 *
 * The marker is NOT taken at face value: /tmp is shared, and nothing rewrites
 * the marker of a service that stopped without resuming the session, so a
 * reused pid would read as recording for as long as it lives. The rule is
 * `statusline_refresh.py::_capture_reading` / `_is_the_service` (#3350), and
 * `tap/record_client.py::hosted_pid` asks the same question:
 *
 *  - the marker is this user's regular file, opened without following a link
 *    and without blocking (another user's FIFO there must not hang pi);
 *  - it names a pid above 1;
 *  - that pid is this user's Probe service: on Linux `/proc/<pid>/cmdline`
 *    names `probe.service`; everywhere else (macOS, Windows) it is the pid the
 *    running service wrote first into its lock file (`server._name_holder`),
 *    and alive. `/proc` is read on Linux only: a `C:\\proc` someone planted on
 *    Windows must not decide.
 *
 * The lock lives in the service's folder, keyed by this machine
 * (`probe.service.paths.machine_key`), which is re-stated here: `tap service
 * socket` would answer it, but that is a Python start, and the readers here
 * run on pi's session-start path and in `/probe-status`. The machine id costs
 * one `ioreg` (macOS) or `reg query` (Windows) start, run from where the OS
 * keeps it (a PATH search on Windows looks in the current folder first), and
 * only for a session with a marker on a machine that is not Linux. Its key is
 * kept once read. A key from the HOSTNAME is never kept: it is the fallback
 * when the id cannot be read, and a Mac's hostname changes with its network,
 * so it is computed afresh each time and tried after the id's key (the service
 * may have fallen back to it too), and the id is asked again a minute later.
 *
 * On Windows the service, like `capture_state._hosted_pid`, also compares the
 * process's start time; Node cannot read it without a subprocess, so here a
 * pid the lock names and that is alive is the service, as on macOS. (The
 * service locks a byte far past the pid it writes, so the pid stays readable.)
 *
 * Read-only, and never throws: an unreadable answer is "not recorded".
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, constants as fsConstants, fstatSync, lstatSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { hostname } from "node:os";
import { join, win32 } from "node:path";

import { hostedFile, OsPlatform, serviceRootDir, SERVICE_LOCK_NAME, type PathEnv } from "./paths.js";

/** `paths.MACHINE_ID_FILES`, in the order the service reads them (Linux). */
export const MACHINE_ID_FILES = ["/etc/machine-id", "/var/lib/dbus/machine-id"] as const;
/**
 * `paths.machine_key`: the first MACHINE_KEY_LEN hex digits of the sha256 of
 * "probe-service", then ONE NUL byte (MACHINE_KEY_SEPARATOR), then the machine
 * id in UTF-8. Hashed as three parts, so no escape in this source can be
 * misread for the byte.
 */
export const MACHINE_KEY_PREFIX = "probe-service";
export const MACHINE_KEY_SEPARATOR = 0x00;
export const MACHINE_KEY_LEN = 16;
/** `paths._IOREG_TIMEOUT_S`: how long the Mac's `ioreg` (or Windows's `reg`) may take. */
const IOREG_TIMEOUT_MS = 5_000;
/** Where macOS keeps `ioreg`: run from there, never found on PATH. */
export const IOREG_PATH = "/usr/sbin/ioreg";
/** How long a failed read of the OS's machine id is not asked again. */
export const MACHINE_ID_RETRY_MS = 60_000;
/** The macOS hardware UUID's key in `ioreg -rd1 -c IOPlatformExpertDevice`. */
const IOREG_UUID_KEY = '"IOPlatformUUID"';
/** `paths._windows_guid`: HKLM\\SOFTWARE\\Microsoft\\Cryptography's MachineGuid, from the 64-bit view. */
const WINDOWS_GUID_KEY = "HKLM\\SOFTWARE\\Microsoft\\Cryptography";
const WINDOWS_GUID_VALUE = "MachineGuid";

/** Where Linux lists processes (`statusline_refresh._PROC_DIR`). */
export const PROC_DIR = "/proc";
/** What the service's command line carries (`client.py`: `python -I -m probe.service`). */
export const SERVICE_COMMAND_MARK = "probe.service";
/** Read at most this much of a watcher file (`_own_pid`: `os.read(fd, 32)`). */
const WATCHER_FILE_MAX_BYTES = 32;
/** Read at most this much of the lock file (`_is_the_service`: `read(64)`). */
const LOCK_FILE_MAX_CHARS = 64;

export interface MachineIdDeps {
  platform?: NodeJS.Platform;
  /** A file's text, or throws. Defaults to `fs.readFileSync`. */
  readFile?: (path: string) => string;
  /** What `ioreg -rd1 -c IOPlatformExpertDevice` prints, or null. */
  ioreg?: () => string | null;
  /** What `reg query <key> /v MachineGuid /reg:64` prints, or null. */
  regQuery?: () => string | null;
  hostname?: () => string;
}

function runIoreg(): string | null {
  try {
    return execFileSync(IOREG_PATH, ["-rd1", "-c", "IOPlatformExpertDevice"], {
      encoding: "utf-8",
      timeout: IOREG_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch {
    return null;
  }
}

/**
 * Windows's `reg.exe`, as `%SystemRoot%\\System32\\reg.exe`: a bare `reg` is
 * searched for in the current folder before PATH, so a planted `reg.exe` there
 * would run.
 */
export function regExePath(env: PathEnv = process.env): string {
  const root = env.SystemRoot ?? env.SYSTEMROOT;
  return win32.join(root && root.trim() ? root : "C:\\Windows", "System32", "reg.exe");
}

function runRegQuery(): string | null {
  try {
    return execFileSync(regExePath(), ["query", WINDOWS_GUID_KEY, "/v", WINDOWS_GUID_VALUE, "/reg:64"], {
      encoding: "utf-8",
      timeout: IOREG_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    });
  } catch {
    return null;
  }
}

/** The MachineGuid line of `reg query`: `    MachineGuid    REG_SZ    <guid>`. */
function windowsGuid(output: string | null): string | null {
  for (const line of (output ?? "").split(/\r?\n/)) {
    const fields = line.trim().split(/\s+/);
    if (fields.length >= 3 && fields[0] === WINDOWS_GUID_VALUE && fields[1].startsWith("REG_")) {
      return fields.slice(2).join(" ").trim() || null;
    }
  }
  return null;
}

/** The Mac's hardware UUID as `paths._darwin_uuid` reports it (upper case). */
function darwinUuid(output: string | null): string | null {
  for (const line of (output ?? "").split("\n")) {
    if (line.includes(IOREG_UUID_KEY) && line.includes("=")) {
      const value = line.slice(line.indexOf("=") + 1).trim().replace(/^"+|"+$/g, "").toUpperCase();
      return value || null;
    }
  }
  return null;
}

/** The OS's own machine id (`paths.machine_id` before its hostname fallback), or null. */
function osMachineId(deps: MachineIdDeps): string | null {
  const platform = deps.platform ?? process.platform;
  if (platform === OsPlatform.Windows) return windowsGuid((deps.regQuery ?? runRegQuery)());
  if (platform === OsPlatform.Mac) return darwinUuid((deps.ioreg ?? runIoreg)());
  const read = deps.readFile ?? ((path: string) => readFileSync(path, "utf-8"));
  for (const name of MACHINE_ID_FILES) {
    try {
      const value = read(name).trim();
      if (value) return value;
    } catch {
      // the next one
    }
  }
  return null;
}

/** `paths.machine_id`'s fallback: the hostname, never empty. */
function hostnameId(deps: MachineIdDeps): string {
  return (deps.hostname ?? hostname)() || "localhost";
}

/**
 * `paths.machine_id`: Linux's machine-id, the Mac's hardware UUID (the same
 * value gethostuuid(2) gives the service), Windows's MachineGuid, else the
 * hostname (never empty).
 */
export function machineId(deps: MachineIdDeps = {}): string {
  return osMachineId(deps) ?? hostnameId(deps);
}

/** `paths.machine_key` for a given machine id. */
export function machineKeyFor(id: string): string {
  return createHash("sha256")
    .update(Buffer.from(MACHINE_KEY_PREFIX, "utf-8"))
    .update(Buffer.from([MACHINE_KEY_SEPARATOR]))
    .update(Buffer.from(id, "utf-8"))
    .digest("hex")
    .slice(0, MACHINE_KEY_LEN);
}

export interface MachineKeysDeps extends MachineIdDeps {
  /** Milliseconds, for the retry of a failed id read. Defaults to `Date.now`. */
  now?: () => number;
  /** Told once, the first time the OS's id cannot be read. */
  log?: (message: string) => void;
}

export interface MachineKeys {
  /** The service locks to try, in order (`paths.lock_path`): the OS id's key, then the hostname's. */
  lockFiles(env: PathEnv, platform: NodeJS.Platform, log?: (message: string) => void): string[];
}

/**
 * This machine's service-folder keys. The OS id's key is kept once read (the
 * service computes its own once too); a failed read is asked again after
 * MACHINE_ID_RETRY_MS, and meanwhile the hostname's key, never kept, is the
 * only one tried. See the module docstring.
 */
export function createMachineKeys(deps: MachineKeysDeps = {}): MachineKeys {
  let osKey: string | undefined;
  let lastTry = Number.NEGATIVE_INFINITY;
  let told = false;
  return {
    lockFiles(env, platform, log = deps.log) {
      const now = (deps.now ?? Date.now)();
      if (osKey === undefined && now - lastTry >= MACHINE_ID_RETRY_MS) {
        lastTry = now;
        const found = osMachineId({ ...deps, platform: deps.platform ?? platform });
        if (found) {
          osKey = machineKeyFor(found);
        } else if (!told) {
          told = true;
          log?.(
            "probe service: no machine id from the OS (machine-id, ioreg or MachineGuid); " +
              "looking for the service under the hostname's key, and asking again in a minute",
          );
        }
      }
      const hostKey = machineKeyFor(hostnameId({ ...deps, platform: deps.platform ?? platform }));
      const keys = osKey === undefined || osKey === hostKey ? [hostKey] : [osKey, hostKey];
      return keys.map((key) => join(serviceRootDir(env, platform), key, SERVICE_LOCK_NAME));
    },
  };
}

/** This process's machine keys (what the readers below use by default). */
const machineKeys = createMachineKeys();

/** `paths.lock_path`: the first service lock to try on this machine. */
export function serviceLockFile(env: PathEnv = process.env, platform: NodeJS.Platform = process.platform): string {
  return machineKeys.lockFiles(env, platform)[0];
}

/** A `.hosted` marker's identity: a new or rewritten one (`tap record` writes it whole) is another. */
export interface MarkerStamp {
  ino: string;
  mtimeNs: string;
}

/** The session's `.hosted` marker as it is now (lstat, no link followed), or null when there is none. */
export function hostedMarkerStamp(sessionId: string): MarkerStamp | null {
  try {
    const info = lstatSync(hostedFile(sessionId), { bigint: true });
    return { ino: String(info.ino), mtimeNs: String(info.mtimeNs) };
  } catch {
    return null;
  }
}

export function sameMarkerStamp(a: MarkerStamp | null, b: MarkerStamp | null): boolean {
  return a === null || b === null ? a === b : a.ino === b.ino && a.mtimeNs === b.mtimeNs;
}

/**
 * The number in a watcher file of this user's, else null (`_own_pid`): opened
 * with O_NOFOLLOW | O_NONBLOCK, then checked on the open file (a regular file,
 * this user's), so neither a planted link nor a FIFO is read.
 */
export function ownPid(path: string): number | null {
  const flags = fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0) | (fsConstants.O_NONBLOCK ?? 0);
  let fd: number;
  try {
    fd = openSync(path, flags);
  } catch {
    return null;
  }
  try {
    const info = fstatSync(fd);
    if (!info.isFile()) return null;
    if (typeof process.getuid === "function" && info.uid !== process.getuid()) return null;
    const buffer = Buffer.alloc(WATCHER_FILE_MAX_BYTES);
    const text = buffer.subarray(0, readSync(fd, buffer, 0, WATCHER_FILE_MAX_BYTES, 0)).toString("ascii").trim();
    return /^\d+$/.test(text) ? Number(text) : null;
  } catch {
    return null;
  } finally {
    try {
      closeSync(fd);
    } catch {
      // closed already
    }
  }
}

export interface ServiceProbeDeps {
  /** Defaults to PROC_DIR. */
  procDir?: string;
  /** Defaults to `process.platform`: `/proc` is read on Linux only. */
  platform?: NodeJS.Platform;
  /** The service locks to try, in order. Defaults to this machine's (`MachineKeys.lockFiles`). */
  serviceLocks?: () => string[];
  /** process.kill-shaped: signal 0 asks whether the pid is alive. */
  kill?: (pid: number, signal: number) => void;
  /** Where a failed machine-id read is told, once. */
  log?: (message: string) => void;
}

function isFolder(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Is `pid` this user's Probe service, alive? (`_is_the_service`.) On Linux its
 * `/proc` command line names `probe.service`; elsewhere (and on a Linux with no
 * `/proc`) it is the pid the service holding its lock wrote there first, and
 * signal 0 reaches it.
 */
export function isTheService(pid: number | null, deps: ServiceProbeDeps = {}): boolean {
  if (pid === null || !Number.isInteger(pid) || pid <= 1) return false;
  const platform = deps.platform ?? process.platform;
  if (platform === OsPlatform.Linux) {
    const proc = deps.procDir ?? PROC_DIR;
    try {
      return readFileSync(join(proc, String(pid), "cmdline")).includes(SERVICE_COMMAND_MARK);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") return false;
      if (isFolder(join(proc, "self"))) return false; // /proc is there and this pid is not: it is gone
    }
  }
  const locks = (deps.serviceLocks ?? (() => machineKeys.lockFiles(process.env, platform, deps.log)))();
  const kill = deps.kill ?? ((p: number, s: number) => process.kill(p, s));
  for (const lock of locks) {
    try {
      const first = readFileSync(lock, "utf-8").slice(0, LOCK_FILE_MAX_CHARS).trim().split(/\s+/)[0];
      if (first !== String(pid)) continue;
      kill(pid, 0);
      return true;
    } catch {
      // no such lock, or the pid it names is gone: the next lock
    }
  }
  return false;
}

export interface HostedDeps extends ServiceProbeDeps {
  /** Defaults to `ownPid`. */
  readOwnPid?: (path: string) => number | null;
}

/** The Probe service's pid when its marker says it records `sessionId` and it is that service, else null. */
export function hostedServicePid(sessionId: string, deps: HostedDeps = {}): number | null {
  const pid = (deps.readOwnPid ?? ownPid)(hostedFile(sessionId));
  return isTheService(pid, deps) ? pid : null;
}
