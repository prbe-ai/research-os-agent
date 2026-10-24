/**
 * A session the Probe service records (plan A2, `tap record`): no wrapper, so no
 * `.pid` file, and a `.hosted` marker naming the service's pid instead. pi's
 * readers (`/probe-status`, `isDaemonAlive`, the post-spawn wait) used to look
 * only for the `.pid` file, so every such session read "not capturing" and every
 * start of one waited the full 2 s and logged "spawn failed?" (audit #8, #9).
 *
 * Real files in /tmp and a real process whose command line names
 * `probe.service`: the check reads `/proc/<pid>/cmdline` (Linux) or the service's
 * lock file (macOS), and a fake of either would prove only the fake.
 */
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { isDaemonAlive, waitForSpawnConfirmation } from "../src/core/daemon.js";
import {
  createMachineKeys,
  hostedMarkerStamp,
  hostedServicePid,
  isTheService,
  machineId,
  machineKeyFor,
  ownPid,
  serviceLockFile,
} from "../src/core/hosted.js";
import { hostedFile, serviceRootDir, WATCHER_PREFIX } from "../src/core/paths.js";
import { buildStatusReport } from "../src/status.js";

const HAS_PROC = existsSync("/proc/self");

const children: ChildProcess[] = [];
const cleanup: string[] = [];
afterEach(() => {
  for (const child of children.splice(0)) child.kill("SIGKILL");
  for (const path of cleanup.splice(0)) rmSync(path, { force: true, recursive: true });
});

/** A live process; `probe.service` in its command line when `named`. */
async function liveProcess(named: boolean): Promise<number> {
  const args = ["-e", "setTimeout(() => {}, 60000)", ...(named ? ["probe.service"] : [])];
  const child = spawn(process.execPath, args, { stdio: "ignore" });
  children.push(child);
  const pid = child.pid!;
  // Until it has exec'd, /proc shows the parent's command line.
  for (let i = 0; i < 100 && HAS_PROC; i++) {
    try {
      const cmdline = readFileSync(`/proc/${pid}/cmdline`, "utf-8");
      if (cmdline.includes("setTimeout")) break;
    } catch {
      // not there yet
    }
    await new Promise((r) => setTimeout(r, 20));
  }
  return pid;
}

function marker(content: string): { sid: string; path: string } {
  const sid = `pi-hosted-test-${randomUUID()}`;
  const path = `/tmp/${WATCHER_PREFIX}-watcher-${sid}.hosted`;
  cleanup.push(path);
  writeFileSync(path, content, { mode: 0o600 });
  return { sid, path };
}

const realDeps = {
  existsSync,
  readFileSync: (p: string) => readFileSync(p, "utf-8"),
  kill: (p: number, s: number | string) => process.kill(p, s as number),
};

describe.runIf(HAS_PROC)("a session the Probe service records (Linux: /proc)", () => {
  it("/probe-status says the session is capturing, and by whom", async () => {
    const pid = await liveProcess(true);
    const { sid, path } = marker(String(pid));
    const tapState = mkdtempSync(join(tmpdir(), "pi-hosted-tap-"));
    cleanup.push(tapState);

    const report = buildStatusReport(sid, {
      ...realDeps,
      isExecutable: () => false,
      env: { ...process.env, PROBE_PI_TAP_PLUGIN_DIR: tapState },
      extensionDir: resolve(__dirname, ".."),
      cwd: tmpdir(),
    });

    expect(report).toContain("this session:  capturing");
    expect(report).not.toContain("not capturing");
    expect(report).toContain("Probe service");
    expect(report).toContain(path);
  });

  it("isDaemonAlive reads a live service's marker as alive", async () => {
    const pid = await liveProcess(true);
    const { sid } = marker(String(pid));
    expect(isDaemonAlive(sid, realDeps)).toBe(true);
  });

  it("a session start the service took ends the wait at once, without 'spawn failed?'", async () => {
    const pid = await liveProcess(true);
    const { sid } = marker(String(pid));
    const logged: string[] = [];
    let sleeps = 0;
    // `tap start` found the service recording the session and exited 0 (its marker unchanged).
    await waitForSpawnConfirmation(
      sid,
      {
        ...realDeps,
        log: (m) => logged.push(m),
        sleep: async () => {
          sleeps += 1;
        },
      },
      { spawned: { markerBefore: hostedMarkerStamp(sid), exitCode: () => 0 } },
    );
    expect(sleeps).toBe(0);
    expect(logged.join("\n")).not.toContain("spawn failed?");
  });

  it("a marker the service writes during the wait ends it then", async () => {
    const pid = await liveProcess(true);
    const sid = `pi-hosted-test-${randomUUID()}`;
    const path = `/tmp/${WATCHER_PREFIX}-watcher-${sid}.hosted`;
    cleanup.push(path);
    const logged: string[] = [];
    let sleeps = 0;
    await waitForSpawnConfirmation(sid, {
      ...realDeps,
      log: (m) => logged.push(m),
      sleep: async () => {
        sleeps += 1;
        if (sleeps === 3) writeFileSync(path, String(pid), { mode: 0o600 });
      },
    });
    expect(sleeps).toBe(3);
    expect(logged).toEqual([]);
  });

  it("a live marker from before the spawn is no confirmation while `tap start` has not answered", async () => {
    const pid = await liveProcess(true);
    const { sid } = marker(String(pid));
    const logged: string[] = [];
    const answer = await waitForSpawnConfirmation(
      sid,
      { ...realDeps, log: (m) => logged.push(m), sleep: async () => {} },
      { maxPolls: 3, spawned: { markerBefore: hostedMarkerStamp(sid), exitCode: () => undefined } },
    );
    expect(answer).not.toBe("hosted");
    expect(logged.join("\n")).toContain("spawn failed?");
  });

  it("the service's rewrite of the marker (a new file, as `tap record` writes it) is confirmation", async () => {
    const pid = await liveProcess(true);
    const { sid, path } = marker(String(pid));
    const stamp = hostedMarkerStamp(sid);
    expect(stamp).not.toBeNull();
    let sleeps = 0;
    const answer = await waitForSpawnConfirmation(
      sid,
      {
        ...realDeps,
        log: () => {},
        sleep: async () => {
          sleeps += 1;
          if (sleeps === 2) {
            writeFileSync(`${path}.tmp`, String(pid), { mode: 0o600 });
            execFileSync("mv", ["-f", `${path}.tmp`, path]);
          }
        },
      },
      { spawned: { markerBefore: stamp, exitCode: () => undefined } },
    );
    expect(answer).toBe("hosted");
    expect(sleeps).toBe(2);
  });

  describe("is not fooled by", () => {
    it("a live process that is not the service (pid reuse)", async () => {
      const pid = await liveProcess(false);
      const { sid } = marker(String(pid));
      expect(isDaemonAlive(sid, realDeps)).toBe(false);
    });

    it("a service that is gone", async () => {
      const pid = await liveProcess(true);
      children.splice(0).forEach((c) => c.kill("SIGKILL"));
      await new Promise((r) => setTimeout(r, 200));
      const { sid } = marker(String(pid));
      expect(isDaemonAlive(sid, realDeps)).toBe(false);
    });

    it("pid 1, or no number at all", () => {
      expect(isDaemonAlive(marker("1").sid, realDeps)).toBe(false);
      expect(isDaemonAlive(marker("0").sid, realDeps)).toBe(false);
      expect(isDaemonAlive(marker("not-a-pid").sid, realDeps)).toBe(false);
      expect(isDaemonAlive(marker("").sid, realDeps)).toBe(false);
    });

    it("a symlink planted where the marker goes", async () => {
      const pid = await liveProcess(true);
      const dir = mkdtempSync(join(tmpdir(), "pi-hosted-link-"));
      cleanup.push(dir);
      const target = join(dir, "pid");
      writeFileSync(target, String(pid));
      const sid = `pi-hosted-test-${randomUUID()}`;
      const path = `/tmp/${WATCHER_PREFIX}-watcher-${sid}.hosted`;
      cleanup.push(path);
      symlinkSync(target, path);
      expect(isDaemonAlive(sid, realDeps)).toBe(false);
    });

    it("a FIFO planted where the marker goes (no writer: a blocking open would hang)", () => {
      const sid = `pi-hosted-test-${randomUUID()}`;
      const path = `/tmp/${WATCHER_PREFIX}-watcher-${sid}.hosted`;
      cleanup.push(path);
      execFileSync("mkfifo", [path]);
      expect(ownPid(path)).toBeNull();
      expect(isDaemonAlive(sid, realDeps)).toBe(false);
    });

    it("a folder planted where the marker goes", () => {
      const sid = `pi-hosted-test-${randomUUID()}`;
      const path = `/tmp/${WATCHER_PREFIX}-watcher-${sid}.hosted`;
      cleanup.push(path);
      mkdirSync(path);
      expect(isDaemonAlive(sid, realDeps)).toBe(false);
    });
  });
});

/** No /proc (macOS): the pid the running service wrote first into its lock file, alive. */
describe("without /proc: the service's lock file names it", () => {
  function lockWith(content: string): { lock: string; procDir: string } {
    const dir = mkdtempSync(join(tmpdir(), "pi-hosted-lock-"));
    cleanup.push(dir);
    const lock = join(dir, "service.lock");
    writeFileSync(lock, content);
    return { lock, procDir: join(dir, "no-proc-here") };
  }

  it("is the service when the lock's first field is the pid and it is alive", () => {
    const { lock, procDir } = lockWith(`${process.pid} 1729700000.25\n`);
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [lock] })).toBe(true);
  });

  it("is not when the lock names another pid, or the pid is gone, or there is no lock", () => {
    const { lock, procDir } = lockWith(`${process.pid + 1} 1729700000.25\n`);
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [lock] })).toBe(false);
    const dead = lockWith("4194301 1729700000.25\n");
    const gone = () => {
      throw Object.assign(new Error("ESRCH"), { code: "ESRCH" });
    };
    expect(isTheService(4194301, { procDir: dead.procDir, platform: "darwin", serviceLocks: () => [dead.lock], kill: gone })).toBe(false);
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [join(procDir, "absent.lock")] })).toBe(false);
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [] })).toBe(false);
  });

  it("an empty lock (a service still starting) is no answer", () => {
    const { lock, procDir } = lockWith("");
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [lock] })).toBe(false);
  });

  it("tries every lock it is given: this machine's id key first, then the hostname key", () => {
    const { lock, procDir } = lockWith(`${process.pid} 1\n`);
    const absent = join(procDir, "absent.lock");
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [absent, lock] })).toBe(true);
    expect(isTheService(process.pid, { procDir, platform: "darwin", serviceLocks: () => [absent] })).toBe(false);
  });

  it("reads /proc only on Linux: a planted proc folder elsewhere (C:\\proc on Windows) decides nothing", () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-hosted-planted-proc-"));
    cleanup.push(dir);
    mkdirSync(join(dir, String(process.pid)));
    mkdirSync(join(dir, "self"));
    writeFileSync(join(dir, String(process.pid), "cmdline"), "python\0-m\0probe.service\0");
    expect(isTheService(process.pid, { procDir: dir, platform: "win32", serviceLocks: () => [] })).toBe(false);
    expect(isTheService(process.pid, { procDir: dir, platform: "darwin", serviceLocks: () => [] })).toBe(false);
    expect(isTheService(process.pid, { procDir: dir, platform: "linux", serviceLocks: () => [] })).toBe(true);
  });

  it("reads the marker through the same check", () => {
    const { lock, procDir } = lockWith(`${process.pid} 1\n`);
    const { sid } = marker(String(process.pid));
    expect(hostedServicePid(sid, { procDir, platform: "darwin", serviceLocks: () => [lock] })).toBe(process.pid);
    expect(hostedServicePid(`pi-hosted-test-${randomUUID()}`, { procDir, platform: "darwin", serviceLocks: () => [lock] })).toBeNull();
  });
});

describe("the watcher file reader", () => {
  it("reads this user's number, and nothing else", () => {
    expect(ownPid(marker("12345\n").path)).toBe(12345);
    expect(ownPid(marker(" 77 ").path)).toBe(77);
    expect(ownPid(marker("12a").path)).toBeNull();
    expect(ownPid(marker("-5").path)).toBeNull();
    expect(ownPid(hostedFile(`pi-hosted-test-${randomUUID()}`))).toBeNull();
  });
});

/**
 * The service's folder is keyed by this machine: `paths.machine_key`, re-stated
 * here. The cases are a fixture the Python is held to as well
 * (`agent/tests/test_pi_core_parity.py`), so neither copy can drift alone.
 */
describe("the service's machine key", () => {
  const fixture = JSON.parse(
    readFileSync(resolve(__dirname, "..", "..", "..", "tests", "fixtures", "service-machine-key.json"), "utf-8"),
  ) as { cases: { machine_id: string; machine_key: string }[] };

  it.each(fixture.cases)("$machine_id -> $machine_key, as the service computes it", ({ machine_id, machine_key }) => {
    expect(machineKeyFor(machine_id)).toBe(machine_key);
  });

  it("is Linux's machine-id, else dbus's, else the hostname", () => {
    const files: Record<string, string> = { "/var/lib/dbus/machine-id": "dbus-id\n" };
    const read = (path: string) => {
      if (!(path in files)) throw new Error("ENOENT");
      return files[path];
    };
    expect(machineId({ platform: "linux", readFile: read, hostname: () => "h" })).toBe("dbus-id");
    files["/etc/machine-id"] = "  etc-id \n";
    expect(machineId({ platform: "linux", readFile: read, hostname: () => "h" })).toBe("etc-id");
    expect(machineId({ platform: "linux", readFile: () => "\n", hostname: () => "box" })).toBe("box");
    expect(machineId({ platform: "linux", readFile: () => "", hostname: () => "" })).toBe("localhost");
  });

  it("is the Mac's hardware UUID from ioreg, upper case, else the hostname", () => {
    const ioreg = [
      '+-o J314sAP  <class IOPlatformExpertDevice, id 0x100000237, registered, matched, active, busy 0>',
      '    "IOPlatformSerialNumber" = "XYZ"',
      '    "IOPlatformUUID" = "4f3c2a1b-9d8e-4c7b-a6f5-0e1d2c3b4a59"',
    ].join("\n");
    expect(machineId({ platform: "darwin", ioreg: () => ioreg, hostname: () => "mac" })).toBe(
      "4F3C2A1B-9D8E-4C7B-A6F5-0E1D2C3B4A59",
    );
    expect(machineId({ platform: "darwin", ioreg: () => null, hostname: () => "mac.local" })).toBe("mac.local");
  });

  it("is Windows's MachineGuid from the registry, else the hostname", () => {
    const reg = [
      "",
      "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography",
      "    MachineGuid    REG_SZ    0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
      "",
    ].join("\r\n");
    expect(machineId({ platform: "win32", regQuery: () => reg, hostname: () => "pc" })).toBe("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0");
    expect(machineId({ platform: "win32", regQuery: () => null, hostname: () => "pc" })).toBe("pc");
  });

  /**
   * A machine key from the hostname is a fallback, never the answer to keep: the
   * OS id may have failed once (a slow `ioreg`), and a Mac's hostname changes with
   * its network. So the OS id's key is kept once had; until then it is asked
   * again (at most once a minute), and the hostname's key is computed fresh and
   * tried after it (the service itself may have fallen back to it).
   */
  it("never keeps a hostname key: retries the OS id, and tries the id key's lock before the hostname key's", () => {
    let ioregAnswers: (string | null)[] = [null, null, '  "IOPlatformUUID" = "4f3c2a1b-9d8e-4c7b-a6f5-0e1d2c3b4a59"'];
    let ioregCalls = 0;
    let host = "cafe.local";
    let clock = 0;
    const logged: string[] = [];
    const keys = createMachineKeys({
      platform: "darwin",
      ioreg: () => {
        ioregCalls += 1;
        return ioregAnswers.shift() ?? null;
      },
      hostname: () => host,
      now: () => clock,
      log: (m) => logged.push(m),
    });
    const env = { HOME: "/home/someone" };
    const lock = (key: string) => join("/home/someone/.local/state/probe/service", key, "service.lock");

    expect(keys.lockFiles(env, "darwin")).toEqual([lock(machineKeyFor("cafe.local"))]);
    expect(logged).toHaveLength(1);
    expect(logged[0]).toContain("machine id");
    // Within the minute: no second ioreg, and a new hostname gives a new key (not a kept one).
    host = "office.local";
    expect(keys.lockFiles(env, "darwin")).toEqual([lock(machineKeyFor("office.local"))]);
    expect(ioregCalls).toBe(1);
    // A minute on: asked again (fails again: still not logged twice), then answered and kept.
    clock = 61_000;
    keys.lockFiles(env, "darwin");
    expect(ioregCalls).toBe(2);
    expect(logged).toHaveLength(1);
    clock = 122_000;
    const uuidKey = machineKeyFor("4F3C2A1B-9D8E-4C7B-A6F5-0E1D2C3B4A59");
    expect(keys.lockFiles(env, "darwin")).toEqual([lock(uuidKey), lock(machineKeyFor("office.local"))]);
    expect(keys.lockFiles(env, "darwin")).toEqual([lock(uuidKey), lock(machineKeyFor("office.local"))]);
    expect(ioregCalls).toBe(3);
  });

  it("puts the lock under $HOME's service folder, never XDG_STATE_HOME (Windows: %LOCALAPPDATA%)", () => {
    const env = { HOME: "/home/someone", XDG_STATE_HOME: "/elsewhere" };
    expect(serviceRootDir(env, "linux")).toBe("/home/someone/.local/state/probe/service");
    expect(serviceLockFile(env, "darwin")).toMatch(/^\/home\/someone\/\.local\/state\/probe\/service\/[0-9a-f]{16}\/service\.lock$/);
    expect(serviceRootDir({ LOCALAPPDATA: "/c/Users/me/AppData/Local", USERPROFILE: "/c/Users/me" }, "win32")).toBe(
      join("/c/Users/me/AppData/Local", "probe", "service"),
    );
    expect(serviceRootDir({ USERPROFILE: "/c/Users/me" }, "win32")).toBe(join("/c/Users/me", "AppData", "Local", "probe", "service"));
  });
});
