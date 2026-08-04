/**
 * The launcher's version gate.
 *
 * This is here because the first published build got it wrong in the most
 * expensive possible way: it handed off to any `probe` that existed on PATH,
 * so every EXISTING user — the people most likely to run the command — got
 * their old CLI and none of the wizard. `npx probe-research doctor` failed with
 * "No such command".
 */

const { test } = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const BIN = path.join(__dirname, "..", "bin", "probe-research.js");
const PKG = require("../package.json");

// The comparator, lifted by evaluating the module's own source so the test
// cannot drift from the shipped implementation.
const source = require("node:fs").readFileSync(BIN, "utf8");
const atLeast = new Function(
  `${source.match(/function atLeast[\s\S]*?\n}\n/)[0]}; return atLeast;`,
)();

test("atLeast compares numerically, not lexically", () => {
  // The bug a string compare would produce: "0.9.0" > "0.10.0".
  assert.equal(atLeast("0.10.0", "0.9.0"), true);
  assert.equal(atLeast("0.9.0", "0.10.0"), false);
});

test("atLeast treats an equal version as good enough", () => {
  assert.equal(atLeast("0.10.0", "0.10.0"), true);
});

test("atLeast handles the four-part versions this project also uses", () => {
  assert.equal(atLeast("0.23.4.0", "0.23.3.1"), true);
  assert.equal(atLeast("0.23.3.1", "0.23.4.0"), false);
});

test("atLeast treats a missing segment as zero", () => {
  assert.equal(atLeast("1.0", "1.0.0"), true);
  assert.equal(atLeast("1.0.1", "1.0"), true);
});

test("the stale install that shipped the bug is rejected", () => {
  // 0.8.2 was on the maintainer's PATH and has neither `wizard` nor `doctor`.
  assert.equal(atLeast("0.8.2", PKG.version), false);
});

test("bin is executable and parses", () => {
  execFileSync(process.execPath, ["--check", BIN]);
  const mode = require("node:fs").statSync(BIN).mode;
  assert.ok(mode & 0o111, "bin must be executable in the published tarball");
});

test("every fallback constrains the version", () => {
  // Unpinned, `uv tool run --from probe-research` reuses an already-installed
  // old tool and the version gate above buys nothing.
  // Match the whole uv call, not a fixed argument order — inserting a flag
  // before --from must not silently stop this test matching anything.
  const fallbacks = source.match(/"tool",\s*"run",[^\]]+/g) || [];
  assert.ok(fallbacks.length >= 2, "expected uv fallbacks");
  for (const f of fallbacks) {
    assert.ok(/spec|>=/.test(f), `unconstrained uv fallback: ${f}`);
  }
  assert.ok(/"--spec", spec/.test(source), "pipx fallback must be constrained too");
});

test("the CLI floor is NOT this package's own version", () => {
  // npm and PyPI release independently. 0.10.1 was a launcher-only fix with no
  // matching PyPI release, so pinning `==<our version>` asked for a version
  // that does not exist and broke the command outright.
  assert.ok(/const MIN_CLI = "/.test(source), "MIN_CLI must be its own constant");
  assert.ok(
    !/wanted\s*=\s*require\(.*package\.json.*\)\.version/.test(source),
    "the resolved spec must not be derived from the npm package version",
  );
});

test("the spec is a floor, never an exact pin", () => {
  // `==` demands a PyPI release that may not exist for a launcher-only bump.
  assert.ok(/\$\{DIST\}>=\$\{target\}/.test(source));
  assert.ok(!/\$\{DIST\}==/.test(source));
  // `target` is the newest KNOWN version, never below the floor.
  assert.ok(
    /const target = latest && atLeast\(latest, wanted\) \? latest : wanted/.test(source),
    "the resolved spec must prefer the published latest and never drop below MIN_CLI",
  );
});

test("MIN_CLI actually exists on PyPI", () => {
  // The floor has to name a real release or every fallback fails to resolve.
  const floor = source.match(/const MIN_CLI = "([^"]+)"/)[1];
  assert.equal(floor, "0.36.0", "0.36.0 is the first release carrying `probe backfill`");
});

test("the floor rejects every CLI whose wizard crashes", () => {
  // 0.26.0 through 0.27.0 die with KeyError: Capability.AUTO_UPDATE on any
  // fresh `probe wizard` (#120). They all cleared the old 0.24.0 floor, so the
  // launcher handed users to a CLI that could not finish a setup — and npx is
  // the from-zero entry point, so it has to be the repair path too.
  const floor = source.match(/const MIN_CLI = "([^"]+)"/)[1];
  for (const broken of ["0.26.0", "0.26.3", "0.26.4", "0.27.0"]) {
    assert.equal(
      atLeast(broken, floor),
      false,
      `${broken} crashes on a fresh wizard and must not be handed off to`,
    );
  }
  assert.equal(atLeast(floor, floor), true, "the floor itself must be accepted");
});

test("the floor rejects every CLI without `probe backfill`", () => {
  // The dashboard's last onboarding step hands out `npx probe-research
  // backfill`. Arguments are forwarded verbatim to whatever `probe` resolves,
  // so a pre-0.36.0 install on PATH answers a command the product just told
  // the user to run with `No such command 'backfill'`. Nothing in the copied
  // string differs; only the floor can catch this.
  const floor = source.match(/const MIN_CLI = "([^"]+)"/)[1];
  for (const noBackfill of ["0.27.1", "0.32.0", "0.34.0", "0.35.0"]) {
    assert.equal(
      atLeast(noBackfill, floor),
      false,
      `${noBackfill} has no \`backfill\` command and must not be handed off to`,
    );
  }
});

test("arguments are forwarded, so `npx probe-research backfill` reaches the CLI", () => {
  // The whole dashboard hand-off depends on this: with no args the launcher
  // runs the wizard, with args it forwards them verbatim. If it ever hardcoded
  // ["wizard"] the copied command would silently open the menu instead.
  assert.ok(
    /const forwarded = args\.length \? args : \["wizard"\]/.test(source),
    "args must pass through; only the empty case defaults to the wizard",
  );
  for (const call of source.match(/"tool",\s*"run",[^\]]+/g) || []) {
    assert.ok(/\.\.\.forwarded/.test(call), `uv fallback drops the arguments: ${call}`);
  }
  assert.ok(/"--spec", spec, "probe", \.\.\.forwarded/.test(source), "pipx fallback drops args");
});

test("every fallback refreshes, or users freeze on their first version", () => {
  // uv caches the ENVIRONMENT it built for a requirement, so `>=0.10.0` keeps
  // serving whatever it resolved the first time. Measured: after 0.10.1 was
  // published, an unrefreshed run still returned 0.10.0 — meaning the launcher
  // would silently never deliver another update.
  //
  // `--refresh-package` is NOT enough; it refreshes metadata and reuses the
  // built environment. It has to be the full `--refresh`.
  const uvCalls = source.match(/"tool",\s*"run",[^\]]+/g) || [];
  assert.ok(uvCalls.length >= 2, "expected uv fallbacks");
  for (const call of uvCalls) {
    assert.ok(/"--refresh"/.test(call), `uv fallback must refresh: ${call}`);
    assert.ok(!/--refresh-package/.test(call), "refresh-package is insufficient");
  }
  assert.ok(/"--no-cache"/.test(source), "pipx fallback must bypass its cache");
});

test("never falls back to a bare pip install", () => {
  // On a researcher's machine that usually means conda or system Python.
  assert.ok(!/pip3?\s+install/.test(source.replace(/^\s*\*.*$/gm, "")));
});

// --- currency check -------------------------------------------------------
//
// The floor answers "is this install too old to work". It cannot answer "is
// this install the latest", because a floor stays satisfied forever. `npx
// <tool>` means run the latest, so a satisfied floor was freezing every
// existing user on whatever they already had -- the same freeze the `--refresh`
// flag exists to prevent in the fetch branch, left standing in the handoff one.

test("the handoff is gated on being current, not only above the floor", () => {
  // The whole bug in one assertion: the early return must not be reachable
  // from the floor check alone.
  assert.ok(
    /await latestVersion\(\)/.test(source),
    "the handoff branch must consult the published manifest",
  );
  assert.ok(
    !/if \(found && atLeast\(found, wanted\)\) return run\("probe", forwarded\)/.test(source),
    "handing off on the floor alone is the freeze this check removes",
  );
});

test("an unknown latest still runs the local install", () => {
  // Offline, proxied, self-hosted without the route, or just slow. A currency
  // check that strands a user offline is worse than the staleness it fixes.
  assert.ok(
    /if \(!latest \|\| atLeast\(found, latest\)\) return run\("probe", forwarded\)/.test(source),
    "a null latest must fall open to the local install",
  );
  assert.ok(/return null;/.test(source), "manifest failures must resolve to null");
});

test("every manifest failure path is caught, none throw", () => {
  const fn = source.match(/async function latestVersion\(\)[\s\S]*?\n}\n/)[0];
  assert.ok(/try \{/.test(fn) && /\} catch \{/.test(fn), "must not propagate a fetch error");
  assert.ok(/AbortSignal\.timeout\(/.test(fn), "must bound how long it can stall");
  assert.ok(/res\.ok/.test(fn), "a non-200 is not a version");
});

test("the currency check cannot stall a command for long", () => {
  // It runs on every invocation that has a usable local probe, i.e. the common
  // path. A slow manifest must not be felt as a slow CLI.
  const ms = parseInt(source.match(/const MANIFEST_TIMEOUT_MS = (\d+)/)[1], 10);
  assert.ok(ms > 0 && ms <= 2000, `timeout must be short and non-zero, got ${ms}`);
});

test("a malformed version in the manifest is rejected, not compared", () => {
  // atLeast() parses junk as zeros, so "latest": "banana" would compare as
  // 0.0.0 and silently mean "you are current" forever.
  const fn = source.match(/async function latestVersion\(\)[\s\S]*?\n}\n/)[0];
  assert.ok(/typeof found === "string"/.test(fn), "must type-check the field");
  assert.ok(/\\d\+\(\\\.\\d\+\)\+|\\d/.test(fn), "must shape-check the version string");
});

test("the manifest host follows PROBE_BASE_URL", () => {
  // A self-hosted tenant's latest is not this one's. Asking the wrong host
  // tells them they are behind forever, and every run refetches.
  assert.ok(/PROBE_BASE_URL/.test(source), "must honour the base-url override");
  assert.ok(
    /\/v1\/client-version/.test(source),
    "must read the same manifest the SessionStart nudge reads",
  );
});

test("a rejected promise exits non-zero", () => {
  // An unhandled rejection can exit 0 on some Node builds, which reads as
  // success to whatever invoked the launcher.
  assert.ok(/main\(\)\.catch\(/.test(source), "main must have a rejection handler");
  assert.ok(
    /process\.exit\(1\)/.test(source.match(/main\(\)\.catch\([\s\S]*$/)[0]),
    "the rejection handler must exit non-zero",
  );
});

test("the fetch resolves to the latest, not merely the floor", () => {
  // The half-fix that shipped nothing: detecting "0.46.0 is behind 0.47.0" and
  // then fetching `>=0.36.0` makes uv reuse the installed 0.46.0, so the
  // launcher prints "fetching the latest" and changes nothing. Measured.
  assert.ok(
    !/"--from", `\$\{DIST\}>=\$\{wanted\}`/.test(source),
    "no fetch may be constrained by the floor when a newer version is known",
  );
  const uvCalls = source.match(/"tool",\s*"run",[^\]]+/g) || [];
  for (const call of uvCalls) {
    assert.ok(/"--from", spec/.test(call), `fetch must use the resolved spec: ${call}`);
  }
});

test("has() does not pair an args array with shell:true", () => {
  // DEP0190. Node deprecated it because the args are concatenated, not escaped,
  // and it printed a security warning on every run of the from-zero entry point.
  const fn = source.match(/function has\(cmd\)[\s\S]*?\n}\n/)[0];
  assert.ok(!/spawnSync\([^,]+,\s*\w*args\w*,/.test(fn), "must not pass an args array");
  assert.ok(/spawnSync\(line, \{/.test(fn), "must pass a single shell string");
});
