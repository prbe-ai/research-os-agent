#!/usr/bin/env node
/**
 * `npx probe-research setup` — the one command, from zero.
 *
 * Probe Research is a PYTHON CLI. This package exists only so the entry point
 * matches the shape people already have muscle memory for (`npx <tool>`), the
 * same way Claude Code ships an npm package that installs a native binary and
 * never invokes Node at runtime.
 *
 * It resolves a real `probe` and hands over. It is NOT a reimplementation, so
 * there is nothing here that can drift from the CLI's behaviour.
 *
 * Resolution order, best first:
 *   1. `probe` already on PATH        -> exec it (the common re-run case)
 *   2. `uv`                           -> uvx, no install, no state left behind
 *   3. `pipx`                         -> isolated install
 *   4. bootstrap uv, then uvx         -> the from-zero path
 *
 * We deliberately do NOT fall back to bare `pip install`: on a researcher's
 * machine that usually means a conda/system environment, and silently mutating
 * it to run an installer is exactly the kind of thing that breaks a training
 * run three days later.
 */

const { spawnSync } = require("node:child_process");
const os = require("node:os");

const DIST = "probe-research";
const UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh";

function has(cmd) {
  const probe = process.platform === "win32" ? "where" : "command";
  const args = process.platform === "win32" ? [cmd] : ["-v", cmd];
  const r = spawnSync(probe, args, { stdio: "ignore", shell: true });
  return r.status === 0;
}

function run(cmd, args) {
  // stdio inherit is load-bearing: the wizard is INTERACTIVE (arrow-key menu)
  // and opens a browser. Capturing its output would break both.
  const r = spawnSync(cmd, args, { stdio: "inherit", shell: false });
  if (r.error) {
    console.error(`probe-research: could not run ${cmd}: ${r.error.message}`);
    process.exit(1);
  }
  process.exit(r.status === null ? 1 : r.status);
}

function main() {
  const args = process.argv.slice(2);
  // `npx probe-research` with no arguments means setup — that is the entire
  // reason this package exists, so it should not require remembering a verb.
  const forwarded = args.length ? args : ["setup"];

  if (has("probe")) return run("probe", forwarded);
  if (has("uv")) return run("uv", ["tool", "run", "--from", DIST, "probe", ...forwarded]);
  if (has("pipx")) return run("pipx", ["run", "--spec", DIST, "probe", ...forwarded]);

  if (process.platform === "win32") {
    console.error(
      `probe-research: needs uv or pipx on PATH.\n` +
        `  Install uv: https://docs.astral.sh/uv/getting-started/installation/`,
    );
    process.exit(1);
  }

  console.error("probe-research: installing uv (one-time)…");
  const boot = spawnSync("sh", ["-c", UV_INSTALL], { stdio: "inherit" });
  if (boot.status !== 0) {
    console.error(
      `probe-research: could not install uv automatically.\n  Run: ${UV_INSTALL}`,
    );
    process.exit(1);
  }
  // The installer drops uv in ~/.local/bin, which is not on THIS process's PATH
  // because it was resolved before the install ran.
  const uv = `${os.homedir()}/.local/bin/uv`;
  return run(uv, ["tool", "run", "--from", DIST, "probe", ...forwarded]);
}

main();
