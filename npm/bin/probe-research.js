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

/**
 * Where the published-version manifest lives. The SessionStart hook already
 * reads this exact endpoint to decide whether to nudge, so the launcher and the
 * nudge cannot disagree about what "latest" means.
 *
 * `PROBE_BASE_URL` is honoured for the same reason the hook honours it: a
 * self-hosted tenant's latest is not this one's, and asking the wrong host
 * would tell them they are behind forever.
 */
const MANIFEST_PATH = "/v1/client-version";
const DEFAULT_BASE = "https://api.research.prbe.ai";
/** How long a currency check may delay a command that could already run. */
const MANIFEST_TIMEOUT_MS = 1500;

/**
 * The minimum CLI version this launcher needs — NOT this package's own version.
 *
 * npm and PyPI release INDEPENDENTLY. This package can take a launcher-only
 * patch (as 0.10.1 did) with no corresponding CLI release, so pinning
 * `probe-research==<our version>` resolves to a PyPI version that does not
 * exist. That is exactly how 0.10.1 shipped broken.
 *
 * Three reasons to bump it, and only three:
 *   1. the launcher starts depending on a NEW CLI feature;
 *   2. the plugin's skill prose starts referencing new CLI verbs, so a fresh
 *      install can never pair old-CLI with new-prose (0066 tags review);
 *   3. a shipped CLI version is KNOWN BROKEN and this is the repair path.
 *
 * (3) is why the floor was 0.27.1. 0.26.0 through 0.27.0 crash on every fresh
 * `probe wizard` — a KeyError the moment the plan names auto-update (#120) —
 * and they all cleared the previous 0.24.0 floor, so this launcher happily
 * handed users straight to a CLI that could not complete a setup. `npx
 * probe-research` IS the advertised from-zero entry point; it has to be able
 * to repair the thing it installed. A healthy 0.26.x install pays one refetch,
 * which is the right trade against a broken one staying broken forever.
 *
 * (1) is why it is now 0.36.0. The dashboard's last onboarding step hands out
 * `npx probe-research backfill`, and `probe backfill` does not exist before
 * 0.36.0. Arguments are forwarded to whatever `probe` this launcher resolves,
 * so under the old floor a user with 0.35.0 on PATH gets `No such command
 * 'backfill'` from a command the product just told them to run. A floor is the
 * only thing that can catch it: the copied command is identical either way.
 */
const MIN_CLI = "0.36.0";
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

function baseUrl() {
  const env = process.env.PROBE_BASE_URL;
  if (env && /^https?:\/\//.test(env)) return env.replace(/\/+$/, "");
  return DEFAULT_BASE;
}

/**
 * The newest published CLI, or null if that cannot be established quickly.
 *
 * NULL IS THE IMPORTANT RETURN. Offline, behind a proxy, self-hosted with the
 * route absent, or simply slow -- every one of those must leave a working local
 * install runnable. A currency check that can strand a user offline is worse
 * than the staleness it fixes, so every failure here falls open.
 */
async function latestVersion() {
  try {
    const res = await fetch(`${baseUrl()}${MANIFEST_PATH}`, {
      signal: AbortSignal.timeout(MANIFEST_TIMEOUT_MS),
      headers: { accept: "application/json" },
    });
    if (!res.ok) return null;
    const body = await res.json();
    const found = body && body.cli && body.cli.latest;
    return typeof found === "string" && /^\d+(\.\d+)+$/.test(found) ? found : null;
  } catch {
    return null;
  }
}

function has(cmd) {
  // ONE shell string, never (command, args[], shell:true): Node deprecated that
  // pairing in DEP0190 because the args are concatenated rather than escaped,
  // and it printed a security warning on every single launcher run. `cmd` is a
  // literal at all three call sites, so there is nothing to escape here -- but
  // a warning users see on the from-zero entry point is its own problem.
  const line = process.platform === "win32" ? `where ${cmd}` : `command -v ${cmd}`;
  const r = spawnSync(line, { stdio: "ignore", shell: true });
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

async function main() {
  const args = process.argv.slice(2);
  // `npx probe-research` with no arguments runs the wizard — that is the entire
  // reason this package exists, so it should not require remembering a verb.
  const forwarded = args.length ? args : ["wizard"];
  const wanted = MIN_CLI;

  // Hand off to an existing install only when it is BOTH above the floor and
  // current. The floor alone is not enough, and that was the bug: `npx <tool>`
  // means "run the latest", but a floor is satisfied forever, so anyone who had
  // ever installed the CLI was frozen on it and npx silently never delivered
  // another update.
  //
  // That is the same failure the `--refresh` flag below exists to fix -- uv
  // serving whatever it resolved on day one -- in the other branch. It was
  // fixed there and left standing here.
  let latest = null;
  if (has("probe")) {
    const found = installedVersion();
    if (found && atLeast(found, wanted)) {
      latest = await latestVersion();
      // Unknown latest means run what we have: see latestVersion's contract.
      if (!latest || atLeast(found, latest)) return run("probe", forwarded);
      console.error(
        `probe-research: probe ${found} is behind ${latest}; fetching the latest…`,
      );
    } else {
      console.error(
        `probe-research: installed probe ${found || "(unknown version)"} is older than ` +
          `${wanted}; fetching the latest…`,
      );
    }
  }
  // A FLOOR, not an exact pin. Unpinned, `uv tool run --from probe-research`
  // reuses an already-installed 0.8.2 tool; pinned exactly, it demands a PyPI
  // version that may not exist. `>=` fixes both.
  // Resolve to the newest version we actually know about, not merely the floor.
  // `uv tool run` reuses an installed tool that satisfies the constraint, so a
  // stale 0.46.0 satisfies `>=0.36.0` and uv hands back the very version we
  // just decided was out of date -- the currency check above would print
  // "fetching the latest" and then change nothing. Measured exactly that before
  // this line existed.
  const target = latest && atLeast(latest, wanted) ? latest : wanted;
  const spec = `${DIST}>=${target}`;
  // REFRESH. uv caches the ENVIRONMENT it built for a requirement, so a range
  // like `>=0.10.0` keeps serving whatever it resolved the FIRST time — every
  // user frozen on the version they happened to run on day one, with the
  // launcher silently never delivering another update.
  //
  // It has to be the full `--refresh`: `--refresh-package` only refreshes
  // package METADATA and still reuses the built environment (measured — it
  // kept returning 0.10.0 after 0.10.1 was published). The full refresh costs
  // ~100ms, which is nothing for a command that only runs when there is no
  // usable local install.
  if (has("uv")) {
    return run("uv", ["tool", "run", "--refresh", "--from", spec, "probe", ...forwarded]);
  }
  if (has("pipx")) {
    return run("pipx", ["run", "--no-cache", "--spec", spec, "probe", ...forwarded]);
  }

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
  return run(uv, [
    "tool", "run", "--refresh", "--from", spec, "probe", ...forwarded,
  ]);
}

main().catch((err) => {
  // A throw here would exit 0 under an unhandled rejection on some Node builds,
  // which reads as success to whatever invoked the launcher.
  console.error(`probe-research: ${err && err.message ? err.message : err}`);
  process.exit(1);
});
