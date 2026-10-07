import { execFile } from "node:child_process";
import { accessSync, chmodSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  INLINE_BRIDGE_MIN_CLI,
  INLINE_NOTICE,
  INLINE_READS,
  INLINE_READS_OLD_CLI,
  cliHasInlineBridge,
  forgetInlineShown,
  inlineNotice,
  inlineNoticeDue,
  localCliVersion,
  markInlineShown,
  type CliVersionDeps,
} from "../src/inlineNotice.js";

const SID = "11111111-2222-3333-4444-555555555555";
let tmp: string;
let env: Record<string, string>;
const sessions = () => join(tmp, "state", "probe", "sessions");
const write = (name: string, body: string) => {
  mkdirSync(sessions(), { recursive: true });
  writeFileSync(join(sessions(), `${SID}.${name}`), body);
};

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-inline-"));
  env = { XDG_STATE_HOME: join(tmp, "state"), HOME: tmp };
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("the inline notice (/probe inline)", () => {
  it("names this package's main-agent skills, which ship with it", () => {
    const pkg = join(__dirname, "..");
    const notice = inlineNotice(pkg, true);
    expect(notice).not.toContain("{");
    for (const name of ["track-work", "edit-notes"]) {
      const path = join(pkg, "skills", name, "SKILL.md");
      expect(notice).toContain(path);
      expect(readFileSync(path, "utf8")).toContain(`name: ${name}`);
    }
    expect(INLINE_NOTICE.length).toBeLessThan(1000);
  });

  it("is due once per stretch, and again after a compaction or resume", () => {
    expect(inlineNoticeDue(SID, env)).toBeNull();
    write("state", "full\n");
    write("inline", JSON.stringify({ from: "daemon", since: 1791266269.25 }));
    expect(inlineNoticeDue(SID, env)).toBe(1791266269.25);
    markInlineShown(SID, 1791266269.25, env);
    expect(inlineNoticeDue(SID, env)).toBeNull();
    forgetInlineShown(SID, env);
    expect(inlineNoticeDue(SID, env)).toBe(1791266269.25);
    markInlineShown(SID, 1791266269.25, env);
    write("inline", JSON.stringify({ from: "daemon", since: 1791266300 }));
    expect(inlineNoticeDue(SID, env)).toBe(1791266300);
  });

  it("names the read bridge only beside a CLI that has it", () => {
    const pkg = join(__dirname, "..");
    expect(inlineNotice(pkg, true).endsWith(INLINE_READS)).toBe(true);
    expect(inlineNotice(pkg, false).endsWith(INLINE_READS_OLD_CLI)).toBe(true);
    for (const bridge of [true, false]) {
      expect(Buffer.byteLength(inlineNotice(pkg, bridge))).toBeLessThan(2048);
      expect(inlineNotice(pkg, bridge)).not.toContain("{");
    }
    expect(INLINE_BRIDGE_MIN_CLI).toBe("0.220.0");
    for (const ok of ["probe 0.220.0", "probe 0.231.4", "probe 1.0.0", "0.220.0+local"]) {
      expect(cliHasInlineBridge(ok)).toBe(true);
    }
    for (const old of ["probe 0.219.9", "probe 0.9.300", "garbled", "", null, "probe 0.220.0rc1"]) {
      expect(cliHasInlineBridge(old)).toBe(false);
    }
  });

  it("asks the real CLI its version, and a missing or stuck one is unknown", async () => {
    const bin = join(tmp, "bin");
    mkdirSync(bin);
    const deps = (pathDir: string, timeoutMs?: number): CliVersionDeps => ({
      execFile: (command, args, options, callback) =>
        execFile(command, args, options, (error, stdout, stderr) => callback(error, String(stdout), String(stderr))),
      existsSync,
      isExecutable: (path) => {
        try {
          accessSync(path, constants.X_OK);
          return true;
        } catch {
          return false;
        }
      },
      env: { PATH: pathDir, HOME: tmp },
      timeoutMs,
    });
    expect(await localCliVersion(deps(bin))).toBeNull();
    writeFileSync(join(bin, "probe"), "#!/bin/sh\necho 'probe 0.220.0'\n");
    chmodSync(join(bin, "probe"), 0o755);
    expect(await localCliVersion(deps(bin))).toBe("probe 0.220.0");
    writeFileSync(join(bin, "probe"), "#!/bin/sh\nexec /bin/sleep 5\n");
    const started = Date.now();
    expect(await localCliVersion(deps(bin, 200))).toBeNull();
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it("reads the Python hooks' spelling of the stretch as the same number", () => {
    write("state", "full\n");
    write("inline", JSON.stringify({ from: "off", since: 100 }));
    write("inline-shown", "100.0\n"); // `repr(100.0)`, as session_marker writes it
    expect(inlineNoticeDue(SID, env)).toBeNull();
  });
});
