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
  assert.ok(/\$\{DIST\}>=\$\{wanted\}/.test(source));
  assert.ok(!/\$\{DIST\}==\$\{wanted\}/.test(source));
});

test("MIN_CLI actually exists on PyPI", () => {
  // The floor has to name a real release or every fallback fails to resolve.
  const floor = source.match(/const MIN_CLI = "([^"]+)"/)[1];
  assert.equal(floor, "0.27.1", "0.27.1 is the first release whose wizard does not crash");
});

test("the floor rejects every CLI whose wizard crashes", () => {
  // 0.26.0 through 0.27.0 die with KeyError: Capability.AUTO_UPDATE on any
  // fresh `probe wizard` (#120). They all cleared the old 0.24.0 floor, so the
  // launcher handed users to a CLI that could not finish a setup — and npx is
  // the from-zero entry point, so it has to be the repair path too.
  for (const broken of ["0.26.0", "0.26.3", "0.26.4", "0.27.0"]) {
    assert.equal(
      atLeast(broken, "0.27.1"),
      false,
      `${broken} crashes on a fresh wizard and must not be handed off to`,
    );
  }
  assert.equal(atLeast("0.27.1", "0.27.1"), true, "the fix itself must be accepted");
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
