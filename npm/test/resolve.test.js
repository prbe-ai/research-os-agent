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

test("every fallback pins the version", () => {
  // Unpinned, `uv tool run --from probe-research` reuses an already-installed
  // old tool and the version gate above buys nothing.
  const fallbacks = source.match(/"tool", "run", "--from", [^\]]+/g) || [];
  assert.ok(fallbacks.length >= 2, "expected uv fallbacks");
  for (const f of fallbacks) {
    assert.ok(/spec|==/.test(f), `unpinned uv fallback: ${f}`);
  }
  assert.ok(/"--spec", spec/.test(source), "pipx fallback must be pinned too");
});

test("never falls back to a bare pip install", () => {
  // On a researcher's machine that usually means conda or system Python.
  assert.ok(!/pip3?\s+install/.test(source.replace(/^\s*\*.*$/gm, "")));
});
