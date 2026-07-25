#!/usr/bin/env node
/**
 * `npx probe-research` — the setup wizard, from zero.
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
 *   1. `probe` on PATH AND new enough -> exec it (the common re-run case)
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

/** Compare dotted numeric versions. Returns true when `a` >= `b`. */
function atLeast(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return true;
}

/**
 * The version of an existing `probe`, or null if it cannot be determined.
 *
 * Existence is NOT enough. Every current user already has some `probe`, and
 * they are exactly the people most likely to run this command — handing off to
 * whatever ancient build happens to be on PATH gives them a CLI with none of
 * the wizard in it, which is precisely the bug this check exists to stop.
 */
function installedVersion() {
  const r = spawnSync("probe", ["--version"], { encoding: "utf8", shell: false });
  if (r.status !== 0 || !r.stdout) return null;
  const m = r.stdout.match(/(\d+\.\d+\.\d+(?:\.\d+)?)/);
  return m ? m[1] : null;
}

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
  // `npx probe-research` with no arguments runs the wizard — that is the entire
  // reason this package exists, so it should not require remembering a verb.
  const forwarded = args.length ? args : ["wizard"];
  const wanted = require("../package.json").version;

  // Only hand off to an existing install if it is at least as new as this
  // launcher. Otherwise fall through and let uv/pipx fetch a matching CLI.
  if (has("probe")) {
    const found = installedVersion();
    if (found && atLeast(found, wanted)) return run("probe", forwarded);
    console.error(
      `probe-research: installed probe ${found || "(unknown version)"} is older than ` +
        `${wanted}; fetching ${wanted}…`,
    );
  }
  // PIN the version. Unpinned, `uv tool run --from probe-research` happily
  // reuses an already-installed 0.8.2 tool and we are back where we started.
  const spec = `${DIST}==${wanted}`;
  if (has("uv")) return run("uv", ["tool", "run", "--from", spec, "probe", ...forwarded]);
  if (has("pipx")) return run("pipx", ["run", "--spec", spec, "probe", ...forwarded]);

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
  return run(uv, ["tool", "run", "--from", `${DIST}==${wanted}`, "probe", ...forwarded]);
}

main();
