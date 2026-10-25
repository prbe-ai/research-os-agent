"""A tap fix reaches a session that is already open, at its next event (plan C1, C3).

A coding agent keeps the hooks it loaded at a session's start, and each tap
entry runs a script from the folder the session started with
(`${CLAUDE_PLUGIN_ROOT}`, Codex's `$PLUGIN_ROOT`, Kimi's `$KIMI_PLUGIN_ROOT`).
An update installs a new folder beside it; until now the open session kept
running the old scripts (#3338's Python picker never reached one). So:

  * hooks.json's entries are FROZEN (pinned here): Codex trusts each by a hash
    of the whole entry and silently skips a changed one until the person
    approves it again, so changing one would switch capture off for every
    Codex user. What may change lives behind them, in the scripts;
  * every script an entry runs first hands the event to the INSTALLED copy's
    same script (`hooks/installed-tap.sh`), resolved as the Probe plugin's
    dispatcher resolves its own (`dispatch.installed_root`): Claude Code's
    install ledger, the newest version folder in Codex's cache, the folder
    Kimi's plugin list records; anything unexpected runs the event where it is.

These drive the REAL entry commands (hooks.json, and the tap's Kimi manifest)
against version folders laid out as each agent keeps them. Each copy's scripts
carry one extra line right after their `set` line, past the hand-over, that
writes which copy ran the event (and with what root, arguments and payload) and
stops there, so nothing else in them runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import pytest

TAP = Path(__file__).resolve().parent.parent
HOOKS = TAP / "hooks"
CLAUDE_ENTRIES = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
KIMI_ENTRIES = json.loads((TAP / ".kimi-plugin" / "plugin.json").read_text(encoding="utf-8"))["hooks"]
SCRIPT = {"SessionStart": "session-start.sh", "UserPromptSubmit": "ensure-daemon.sh",
          "SessionEnd": "session-end.sh", "Stop": "turn-end.sh"}
MARKET = "research-os-agent"
PLUGIN = "probe-research-tap"
OLD, NEW = "0.10.18", "0.10.19"
MANIFESTS = (".claude-plugin", ".codex-plugin", ".kimi-plugin")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell")


# --- the frozen entries -----------------------------------------------------------

#: One hash per hooks.json entry. Changing one switches that hook off for every
#: Codex user until they approve it again in /hooks, with nothing in their
#: session saying so. Change the script it runs instead.
PINNED = {
    "SessionStart": "137dfa32ac77abd6",
    "UserPromptSubmit": "b2e19d419b3e38de",
    "SessionEnd": "7d33e4db9ee10730",
    "Stop": "8472c9fc0adc9bf4",
}


def _entry_hash(event: str, matcher, hook: dict) -> str:
    return hashlib.sha256(json.dumps([event, matcher, hook], sort_keys=True).encode()).hexdigest()[:16]


def test_the_entries_are_frozen():
    actual = {
        event: _entry_hash(event, group.get("matcher"), hook)
        for event, groups in CLAUDE_ENTRIES.items() for group in groups for hook in group["hooks"]
    }
    assert actual == PINNED, "hooks/hooks.json changed: every Codex user would have to approve the tap's hooks again"


def test_every_entry_script_hands_over_before_it_reads_anything():
    """The hand-over is the first thing each script does: before its `set`
    line, before stdin is read (the installed copy reads the payload), before
    session-end.sh's deadline starts."""
    call = 'tap_run_installed "${BASH_SOURCE[0]}" "$@"'
    for event, script in SCRIPT.items():
        text = (HOOKS / script).read_text(encoding="utf-8")
        assert call in text, f"{script} never hands over"
        before = [line for line in text[: text.index(call)].splitlines() if not line.lstrip().startswith("#")]
        for later in ("set -", "$(cat", "HOOK_INPUT", "sleep "):
            assert not any(later in line for line in before), f"{script}: `{later}` comes before the hand-over"
        command = CLAUDE_ENTRIES[event][0]["hooks"][0]["command"]
        assert f'"$ROOT/hooks/{script}"' in command, f"the {event} entry runs {script}"


# --- the layouts ---------------------------------------------------------------------


def _mark(version: str, script: str) -> str:
    return (
        'P="$(cat)"; printf "%s|%s|%s|%s|%s|%s|%s|%s\\n" "' + version + '" "' + script + '" '
        '"${CLAUDE_PLUGIN_ROOT:-}" "${PLUGIN_ROOT:-}" "${KIMI_PLUGIN_ROOT:-}" "${PROBE_TAP_FOLLOWED-unset}" "$*" "$P" '
        '>> "$TAP_TEST_MARKS"; exit 0\n'
    )


def _copy(root: Path, version: str, *, marked: bool = True) -> Path:
    """This branch's tap at `root`, at `version`; each script marked (see above)."""
    shutil.copytree(HOOKS, root / "hooks")
    shutil.copytree(TAP / "tap", root / "tap", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for folder in MANIFESTS:
        data = json.loads((TAP / folder / "plugin.json").read_text(encoding="utf-8"))
        data["version"] = version
        (root / folder).mkdir()
        (root / folder / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    if marked:
        for script in SCRIPT.values():
            path = root / "hooks" / script
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            at = next(i for i, line in enumerate(lines) if line.startswith("set -") and "pipefail" in line)
            lines.insert(at + 1, _mark(version, script))
            path.write_text("".join(lines), encoding="utf-8")
    return root


def _age(root: Path, seconds: float = 60) -> None:
    """Date a copy's top folders `seconds` back (`installed-tap.sh` `tap_settled`)."""
    when = time.time() - seconds
    for folder in (root, root / "hooks", root / "tap"):
        os.utime(folder, (when, when))


class Box:
    """A scratch home, with each agent's plugin folders where it keeps them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.marks = tmp / "marks.log"
        self.claude = tmp / "claude"
        self.codex = tmp / "codex"
        self.kimi = tmp / "kimi"
        (tmp / "home").mkdir()

    def claude_copy(self, version: str, **kw) -> Path:
        return _copy(self.claude / "plugins" / "cache" / MARKET / PLUGIN / version, version, **kw)

    def codex_copy(self, version: str, *, settled: bool = True, **kw) -> Path:
        """A version folder in Codex's cache; `settled`: written a minute ago, as
        a folder Codex finished installing (a fresh one may still be written)."""
        root = _copy(self.codex / "plugins" / "cache" / MARKET / PLUGIN / version, version, **kw)
        if settled:
            _age(root)
        return root

    def kimi_copy(self, version: str, **kw) -> Path:
        return _copy(self.kimi / "plugins" / "managed" / f"{PLUGIN}@{version}", version, **kw)

    def ledger(self, *paths, scope: str = "user", key: str = f"{PLUGIN}@{MARKET}", indent=2,
               raw: str | None = None) -> Path:
        ledger = self.claude / "plugins" / "installed_plugins.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        entries = [{"scope": scope, "installPath": str(p), "version": Path(p).name,
                    "installedAt": "2026-10-24T00:00:00.000Z", "gitCommitSha": "abc"} for p in paths]
        other = {"probe-research@" + MARKET: [{"scope": "user", "installPath": str(
            self.claude / "plugins" / "cache" / MARKET / "probe-research" / "0.171.0"), "version": "0.171.0"}]}
        doc = {"version": 2, "plugins": {**other, key: entries}}
        ledger.write_text(raw if raw is not None else json.dumps(doc, indent=indent), encoding="utf-8")
        return ledger

    def kimi_list(self, *roots, plugin: str = PLUGIN) -> Path:
        path = self.kimi / "plugins" / "installed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [{"id": "probe-research", "root": str(self.kimi / "plugins" / "managed" / "probe-research@0.171.0"),
                    "source": "local-path", "enabled": True}]
        records += [{"id": plugin, "root": str(r), "source": "local-path", "enabled": True,
                     "installedAt": "2026-10-24T00:00:00.000Z"} for r in roots]
        path.write_text(json.dumps({"version": 1, "plugins": records}, indent=2), encoding="utf-8")
        return path

    def env(self, **extra) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "CODEX_HOME": str(self.codex),
            "KIMI_CODE_HOME": str(self.kimi),
            "PROBE_OUTBOX_DIR": str(self.tmp / "outbox"),
            "PROBE_CONFIG_PATH": str(self.tmp / "absent.json"),
            "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(self.tmp / "tap-state"),
            "PRBE_CODEX_TAP_PLUGIN_DIR": str(self.tmp / "codex-tap-state"),
            "PROBE_TAP_HOSTED": "0",
            "PROBE_SERVICE_REPORTS": "0",
            "TAP_TEST_MARKS": str(self.marks),
        }
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run(self, event: str, env: dict, payload: str = '{"session_id": "s-1"}', kimi: bool = False):
        entries = KIMI_ENTRIES if kimi else None
        if kimi:
            (entry,) = [e for e in entries if e["event"] == event]
            command = entry["command"]
        else:
            command = CLAUDE_ENTRIES[event][0]["hooks"][0]["command"]
        return subprocess.run(command, shell=True, executable="/bin/bash", input=payload, text=True,
                              capture_output=True, env=env, timeout=30, cwd=str(self.tmp))

    def ran(self) -> list[dict]:
        try:
            lines = self.marks.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        keys = ("version", "script", "claude_root", "codex_root", "kimi_root", "followed", "args", "payload")
        return [dict(zip(keys, line.split("|", 7), strict=True)) for line in lines]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# --- Claude Code ---------------------------------------------------------------------


@pytest.mark.parametrize("event", list(SCRIPT))
@pytest.mark.parametrize("indent", [2, None], ids=["as-claude-writes-it", "compact"])
def test_an_open_claude_code_session_runs_the_installed_taps_script(box, event, indent):
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    box.ledger(new, indent=indent)
    done = box.run(event, box.env(CLAUDE_PLUGIN_ROOT=str(old)), payload='{"session_id": "s-1", "x": 1}')
    assert done.returncode == 0, done.stderr
    (ran,) = box.ran()
    assert (ran["version"], ran["script"]) == (NEW, SCRIPT[event])
    assert ran["claude_root"] == str(new), "the installed copy's own folder, as Claude Code would give it"
    assert ran["followed"] == "unset", "taken out, so nothing it starts inherits it"
    assert ran["payload"] == '{"session_id": "s-1", "x": 1}', "stdin reaches it unread"
    assert ran["args"] == ("--wait" if event == "SessionEnd" else "")


def test_a_rolled_back_release_stops_running_in_open_sessions_too(box):
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    box.ledger(old)
    assert box.run("Stop", box.env(CLAUDE_PLUGIN_ROOT=str(new))).returncode == 0
    assert [r["version"] for r in box.ran()] == [OLD]


def test_one_copy_listed_twice_is_still_one_copy(box):
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    box.ledger(new, new)
    box.run("Stop", box.env(CLAUDE_PLUGIN_ROOT=str(old)))
    assert [r["version"] for r in box.ran()] == [NEW]


def test_a_moved_ledger_is_the_one_followed(box, tmp_path):
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    moved = tmp_path / "moved"
    moved.mkdir()
    box.ledger(new).rename(moved / "installed_plugins.json")
    box.ledger(old)
    box.run("Stop", box.env(CLAUDE_PLUGIN_ROOT=str(old), CLAUDE_CODE_PLUGIN_CACHE_DIR=str(moved)))
    assert [r["version"] for r in box.ran()] == [NEW]


def _stays(box: Box, case: str) -> tuple[Path, dict]:
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    env = box.env(CLAUDE_PLUGIN_ROOT=str(old))
    if case == "no-ledger":
        pass
    elif case == "lists-this-copy":
        box.ledger(old)
    elif case == "two-copies":
        box.ledger(old, new)
    elif case == "project-scope":
        box.ledger(new, scope="project")
    elif case == "another-plugins-key":
        box.ledger(new, key=f"probe-research@{MARKET}")
    elif case == "another-plugins-folder":
        other = _copy(box.claude / "plugins" / "cache" / MARKET / "probe-research" / NEW, NEW)
        box.ledger(other)
    elif case == "linked-out-of-the-cache":
        outside = _copy(box.tmp / "elsewhere" / "0.10.20", "0.10.20")
        (old.parent / "0.10.20").symlink_to(outside)
        box.ledger(old.parent / "0.10.20")
    elif case == "no-tap-package-there":
        box.ledger(new)
        (new / "tap" / "__main__.py").unlink()
    elif case == "script-not-runnable-there":
        box.ledger(new)
        (new / "hooks" / "turn-end.sh").chmod(0o644)
    elif case == "no-manifest-there":
        box.ledger(new)
        shutil.rmtree(new / ".claude-plugin")
    elif case == "escaped-path":
        box.ledger(raw=json.dumps({"version": 2, "plugins": {f"{PLUGIN}@{MARKET}": [
            {"scope": "user", "installPath": str(new).replace("/", "\\/")}]}}))
    elif case == "unparseable":
        box.ledger(raw=f'{{"plugins": {{"{PLUGIN}@{MARKET}": [{{"scope": "user", "installPath": "{new}"')
    elif case == "too-big":
        ledger = box.ledger(new)
        ledger.write_text(ledger.read_text() + " " * 300_000)
    elif case == "a-fifo":
        (box.claude / "plugins").mkdir(parents=True, exist_ok=True)
        os.mkfifo(box.claude / "plugins" / "installed_plugins.json")
    elif case == "already-moved":
        box.ledger(new)
        env["PROBE_TAP_FOLLOWED"] = str(box.tmp / "the-copy-it-came-from")
    elif case == "plugin-dir-checkout":
        old = _copy(box.tmp / "checkout" / PLUGIN, OLD)
        box.ledger(new)
        env["CLAUDE_PLUGIN_ROOT"] = str(old)
    elif case == "codex-marks-it":
        box.ledger(new)
        env["PLUGIN_ROOT"] = str(old)
    return old, env


@pytest.mark.parametrize("case", [
    "no-ledger", "lists-this-copy", "two-copies", "project-scope", "another-plugins-key",
    "another-plugins-folder", "linked-out-of-the-cache", "no-tap-package-there", "script-not-runnable-there",
    "no-manifest-there", "escaped-path", "unparseable", "too-big", "a-fifo", "already-moved",
    "plugin-dir-checkout", "codex-marks-it",
])
def test_otherwise_the_event_runs_in_the_sessions_own_copy(box, case):
    """Nothing to hand to, a copy that is not this plugin's own version folder
    beside this one, or one that is not whole: the session's own copy runs it."""
    _old, env = _stays(box, case)
    done = box.run("Stop", env)
    assert done.returncode == 0, done.stderr
    assert [(r["version"], r["followed"]) for r in box.ran()] == [(OLD, "unset")], done.stderr


def test_a_copy_that_cannot_be_run_after_all_runs_the_event_here(box):
    """Gone between the check and the exec (here: a folder where the script
    should be, which passes `-x` and cannot be executed): here, not a failure."""
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    box.ledger(new)
    (new / "hooks" / "turn-end.sh").unlink()
    (new / "hooks" / "turn-end.sh").mkdir()
    done = box.run("Stop", box.env(CLAUDE_PLUGIN_ROOT=str(old)))
    assert done.returncode == 0, done.stderr
    assert [(r["version"], r["claude_root"], r["followed"]) for r in box.ran()] == [(OLD, str(old), "unset")]


def test_an_event_moves_at_most_once(box):
    """The installed copy is told it was handed the event: it runs it, and
    hands it no further, even when a third copy is listed by then."""
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    box.ledger(new)
    third = box.claude_copy("0.10.20")
    seen = box.tmp / "followed-at-the-hand-over"
    script = new / "hooks" / "turn-end.sh"
    text = script.read_text()
    script.write_text(text.replace("# An open session runs the INSTALLED", (
        f'printf "%s" "${{PROBE_TAP_FOLLOWED:-}}" > "{seen}"; '
        f'printf "%s" \'{{"version":2,"plugins":{{"{PLUGIN}@{MARKET}":[{{"scope":"user","installPath":"{third}"}}]}}}}\' '
        f'> "{box.claude}/plugins/installed_plugins.json"\n# An open session runs the INSTALLED'), 1))
    box.run("Stop", box.env(CLAUDE_PLUGIN_ROOT=str(old)))
    assert seen.read_text() == str(old)
    assert [r["version"] for r in box.ran()] == [NEW]


# --- Codex -----------------------------------------------------------------------------


@pytest.mark.parametrize("event", list(SCRIPT))
def test_an_open_codex_session_runs_the_newest_tap_in_codexs_cache(box, event):
    old = box.codex_copy(OLD)
    box.codex_copy("0.9.99")
    box.codex_copy(NEW)
    newest = box.codex_copy("0.10.20")
    half = box.codex_copy("0.10.21")
    (half / "tap" / "__init__.py").unlink()  # still being written: never takes an event
    outside = _copy(box.tmp / "elsewhere" / "0.10.22", "0.10.22")
    (old.parent / "0.10.22").symlink_to(outside)
    done = box.run(event, box.env(PLUGIN_ROOT=str(old)))
    assert done.returncode == 0, done.stderr
    (ran,) = box.ran()
    assert (ran["version"], ran["codex_root"], ran["followed"]) == ("0.10.20", str(newest), "unset")


def test_a_codex_session_on_the_newest_tap_runs_it_where_it_is(box):
    box.codex_copy("0.9.99")
    new = box.codex_copy(NEW)
    box.run("Stop", box.env(PLUGIN_ROOT=str(new)))
    assert [(r["version"], r["codex_root"]) for r in box.ran()] == [(NEW, str(new))]


def test_a_folder_outside_codexs_cache_never_looks_beside_itself(box):
    old = _copy(box.tmp / "elsewhere" / PLUGIN / OLD, OLD)
    _copy(box.tmp / "elsewhere" / PLUGIN / NEW, NEW)
    box.run("Stop", box.env(PLUGIN_ROOT=str(old)))
    assert [r["version"] for r in box.ran()] == [OLD]


def test_a_removed_codex_start_folder_still_runs_through_the_entrys_own_fallback(box):
    """Codex deletes the old version folder at an update: the frozen entry
    re-resolves to the folder that replaced it, which is the newest already."""
    old = box.codex_copy(OLD)
    new = box.codex_copy(NEW)
    shutil.rmtree(old)
    box.run("Stop", box.env(PLUGIN_ROOT=str(old)))
    assert [(r["version"], r["codex_root"], r["followed"]) for r in box.ran()] == [(NEW, str(new), "unset")]


# --- Kimi Code ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", list(SCRIPT))
def test_an_open_kimi_session_runs_the_tap_kimis_plugin_list_records(box, event):
    old, new = box.kimi_copy(OLD), box.kimi_copy(NEW)
    box.kimi_list(new)
    done = box.run(event, box.env(KIMI_PLUGIN_ROOT=str(old)), kimi=True)
    assert done.returncode == 0, done.stderr
    (ran,) = box.ran()
    assert (ran["version"], ran["kimi_root"], ran["followed"]) == (NEW, str(new), "unset")


@pytest.mark.parametrize("case", ["records-this-copy", "no-list", "another-plugins-folder", "two-records",
                                  "recorded-outside-managed", "not-whole-there"])
def test_otherwise_a_kimi_event_runs_where_it_is(box, case):
    old, new = box.kimi_copy(OLD), box.kimi_copy(NEW)
    if case == "records-this-copy":
        box.kimi_list(old)
    elif case == "another-plugins-folder":
        box.kimi_list(_copy(box.kimi / "plugins" / "managed" / f"probe-research-daemon@{NEW}", NEW),
                      plugin="probe-research-daemon")
    elif case == "two-records":
        box.kimi_list(new, old)
    elif case == "recorded-outside-managed":
        box.kimi_list(_copy(box.tmp / "elsewhere" / f"{PLUGIN}@{NEW}", NEW))
    elif case == "not-whole-there":
        box.kimi_list(new)
        shutil.rmtree(new / ".kimi-plugin")
    box.run("Stop", box.env(KIMI_PLUGIN_ROOT=str(old)), kimi=True)
    assert [(r["version"], r["followed"]) for r in box.ran()] == [(OLD, "unset")]


def test_a_kimi_session_whose_folder_was_pruned_runs_the_kept_one(box):
    """Probe prunes a Kimi version folder older than a week and leaves a link
    to the kept version in its place (`kimi_config._prune_versions`): the
    session's entries still find their scripts, and run the kept copy's."""
    old, new = box.kimi_copy(OLD), box.kimi_copy(NEW)
    box.kimi_list(new)
    shutil.rmtree(old)
    old.symlink_to(new.name)
    done = box.run("SessionEnd", box.env(KIMI_PLUGIN_ROOT=str(old)), kimi=True)
    assert done.returncode == 0, done.stderr
    assert [(r["version"], r["kimi_root"]) for r in box.ran()] == [(NEW, str(new))]


@pytest.mark.parametrize("event", list(SCRIPT))
def test_a_kimi_session_whose_folder_is_gone_runs_the_newest_installed_one(box, event):
    """No folder at all where the session started (an uninstall and a
    reinstall, a prune that could make no link): the entry finds the newest
    installed folder of the tap beside it, instead of running nothing."""
    old = box.kimi_copy(OLD)
    box.kimi_copy("0.10.0")
    new = box.kimi_copy(NEW)
    os.utime(box.kimi / "plugins" / "managed" / f"{PLUGIN}@0.10.0", (1, 1))
    box.kimi_list(new)
    shutil.rmtree(old)
    done = box.run(event, box.env(KIMI_PLUGIN_ROOT=str(old)), kimi=True)
    assert done.returncode == 0, done.stderr
    assert [(r["version"], r["kimi_root"]) for r in box.ran()] == [(NEW, str(new))]


def test_a_kimi_session_with_nothing_installed_beside_it_runs_nothing(box):
    old = box.kimi_copy(OLD)
    shutil.rmtree(old)
    done = box.run("Stop", box.env(KIMI_PLUGIN_ROOT=str(old)), kimi=True)
    assert (done.returncode, box.ran()) == (0, [])


# --- budgets ---------------------------------------------------------------------------


def _timed(box: Box, event: str, env: dict, payload: str, runs: int) -> list[float]:
    times = []
    for _ in range(runs):
        started = time.monotonic()
        done = box.run(event, env, payload=payload)
        times.append(time.monotonic() - started)
        assert done.returncode == 0, done.stderr
    return times


def test_the_hand_over_keeps_session_ends_budget(box):
    """Claude Code gives the whole SessionEnd phase 1.5 s. The real scripts,
    unmarked: an open session's SessionEnd on the old copy, moved to the new
    one, stays well inside it, and the move costs one bash start."""
    sid = f"pytest-installed-{uuid.uuid4()}"
    payload = json.dumps({"session_id": sid, "reason": "prompt_input_exit"})
    try:
        old, new = box.claude_copy(OLD, marked=False), box.claude_copy(NEW, marked=False)
        here = _timed(box, "SessionEnd", box.env(CLAUDE_PLUGIN_ROOT=str(new)), payload, 5)
        box.ledger(new)
        moved = _timed(box, "SessionEnd", box.env(CLAUDE_PLUGIN_ROOT=str(old)), payload, 5)
        stays = _timed(box, "SessionEnd", box.env(CLAUDE_PLUGIN_ROOT=str(new)), payload, 5)
    finally:
        for suffix in (".shutdown", ".pid", ".stopping", ".hosted"):
            Path(f"/tmp/{PLUGIN}-watcher-{sid}{suffix}").unlink(missing_ok=True)
    assert max(moved) < 1.5, moved
    overhead = statistics.median(moved) - statistics.median(here)
    assert overhead < 0.25, (here, moved, stays)
    assert statistics.median(stays) - statistics.median(here) < 0.25, (here, stays)


# --- review fixes: bash 3.2, half-written copies --------------------------------------

#: The bash the hooks run under in the timing test: macOS's /bin/bash is 3.2,
#: whose pattern operators grow much faster than the text. CI's macOS job and
#: the `bash:3.2` image set it; anywhere else it is this machine's /bin/bash.
TEST_BASH = os.environ.get("TAP_TEST_BASH", "/bin/bash")


def test_the_hand_over_never_runs_a_pattern_operator_over_the_whole_list():
    """bash 3.2 (macOS /bin/bash) took 1.8 s per event over an 8 KB ledger with
    `${TAP_TEXT//...}`, and ran past every hook's timeout at 36 KB. The list is
    searched with regular expressions only (the C library's, linear); pattern
    operators and `case` globs run on this plugin's own entry, never the list.
    Nor is it read with `read` (a system call per byte there)."""
    text = (HOOKS / "installed-tap.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("${TAP_TEXT//", "${TAP_TEXT#", "${TAP_TEXT%", "${TAP_TEXT/", '="$TAP_TEXT"', 'case "$TAP_TEXT"',
                   "read -r -d '' -n", " TAP_TEXT <"):
        assert banned not in code, f"`{banned}` over the whole list: bash 3.2 makes it super-linear"


def _ledger_of(box: Box, plugins: int, listed: Path) -> int:
    """A ledger as Claude Code writes it (indent 2) with `plugins` other plugins,
    this tap's entry in the middle; its size in bytes."""
    cache = box.claude / "plugins" / "cache"
    found = {}
    for i in range(plugins):
        if i == plugins // 2:
            found[f"{PLUGIN}@{MARKET}"] = [{"scope": "user", "installPath": str(listed), "version": listed.name,
                                            "installedAt": "2026-10-24T00:00:00.000Z",
                                            "lastUpdated": "2026-10-24T00:00:00.000Z", "gitCommitSha": "a" * 40}]
        name = f"some-plugin-{i:03d}"
        found[f"{name}@market-{i % 4}"] = [{"scope": "user", "installPath": str(cache / f"market-{i % 4}" / name / "1.2.3"),
                                           "version": "1.2.3", "installedAt": "2026-10-01T12:34:56.789Z",
                                           "lastUpdated": "2026-10-20T12:34:56.789Z", "gitCommitSha": f"{i:040x}"}]
    return len(box.ledger(raw=json.dumps({"version": 2, "plugins": found}, indent=2)).read_text())


def _bash_env(box: Box, **extra) -> dict:
    """The box's environment with TEST_BASH as `bash` on PATH, so the scripts'
    `#!/usr/bin/env bash` runs it too."""
    bin_dir = box.tmp / "bash-bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        (bin_dir / "bash").symlink_to(TEST_BASH)
    env = box.env(**extra)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # The locale a Mac runs its hooks in: bash 3.2 copies and matches text per
    # character in a UTF-8 one.
    env["LANG"] = env["LC_ALL"] = "en_US.UTF-8"
    return env


@pytest.mark.parametrize("plugins", [20, 100], ids=["20-plugins-7KB", "100-plugins-36KB"])
@pytest.mark.parametrize("case", ["stays", "moves"])
def test_the_hand_over_is_quick_on_this_bash_whatever_the_ledgers_size(box, plugins, case):
    """Every event, the hand-over part only (each copy's scripts stop at the
    line after it): well inside every hook's budget on bash 3.2 too, at a
    20-plugin (~7 KB) and a 100-plugin (~36 KB) ledger."""
    old, new = box.claude_copy(OLD), box.claude_copy(NEW)
    size = _ledger_of(box, plugins, old if case == "stays" else new)
    env = _bash_env(box, CLAUDE_PLUGIN_ROOT=str(old))
    version = subprocess.run(["bash", "--version"], capture_output=True, text=True, env=env).stdout.splitlines()[0]
    took = {}
    for event in SCRIPT:
        times = []
        for _ in range(3):
            started = time.monotonic()
            done = subprocess.run(CLAUDE_ENTRIES[event][0]["hooks"][0]["command"], shell=True, executable=TEST_BASH,
                                  input='{"session_id": "s-1"}', text=True, capture_output=True, env=env, timeout=30,
                                  cwd=str(box.tmp))
            times.append(time.monotonic() - started)
            assert done.returncode == 0, done.stderr
        took[event] = round(statistics.median(times) * 1000)
    expected = OLD if case == "stays" else NEW
    assert {r["version"] for r in box.ran()} == {expected}
    assert max(took.values()) < 100, f"{version}, {size} B ledger: {took} ms"


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("missing", ["hooks/tap-python.sh", "hooks/probe-hooks-trust.sh", "hooks/session-start.sh",
                                     "tap/main.py", "tap/hosted.py"])
def test_a_copy_missing_a_file_its_scripts_need_takes_no_event(box, harness, missing):
    """A copy caught mid-install (or mid-delete) without a file its scripts
    source, or a module the tap starts from: the event runs where it is."""
    if harness == "claude":
        old, new = box.claude_copy(OLD), box.claude_copy(NEW)
        box.ledger(new)
        env = box.env(CLAUDE_PLUGIN_ROOT=str(old))
    else:
        old, new = box.codex_copy(OLD), box.codex_copy(NEW)
        env = box.env(PLUGIN_ROOT=str(old))
    (new / missing).unlink()
    if harness == "codex":
        _age(new)
    for event in ("SessionStart", "Stop"):
        assert box.run(event, env).returncode == 0
    assert [r["version"] for r in box.ran()] == [OLD, OLD]


def test_a_codex_folder_still_being_written_takes_no_event_until_it_settles(box):
    """Codex picks by listing, and the newest folder may be one Codex is still
    writing (it removes the old one when done): a folder written in the last
    few seconds waits."""
    old = box.codex_copy(OLD)
    new = box.codex_copy(NEW, settled=False)
    box.run("Stop", box.env(PLUGIN_ROOT=str(old)))
    _age(new)
    box.run("Stop", box.env(PLUGIN_ROOT=str(old)))
    assert [r["version"] for r in box.ran()] == [OLD, NEW]


#: What each released tap requires of a copy before it hands an open session's
#: events to it (`installed-tap.sh` TAP_WHOLE_FILES as released), frozen: a tap
#: that lacks one of them never receives those sessions' events. The current
#: list may shrink in a later release; a frozen one never changes, and this
#: tree must hold every file of every one.
RELEASED_WHOLE_FILES = {
    "0.10.18": ("hooks/tap-python.sh", "hooks/probe-hooks-trust.sh", "hooks/session-start.sh", "tap/__init__.py",
                "tap/__main__.py", "tap/main.py", "tap/hosted.py"),
    # Not tap/capture_gate.py: an open 0.10.19 session must still follow a
    # rollback to a copy that has none (0.10.18).
    "0.10.19": ("hooks/tap-python.sh", "hooks/probe-hooks-trust.sh", "hooks/session-start.sh", "tap/__init__.py",
                "tap/__main__.py", "tap/main.py", "tap/hosted.py"),
}


def _current_whole_files() -> tuple:
    text = (HOOKS / "installed-tap.sh").read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("TAP_WHOLE_FILES="))
    return tuple(line.split("=", 1)[1].strip('"').split())


def test_every_file_a_released_tap_requires_before_it_hands_over_is_here():
    """A tap that drops one would never again receive an open session's
    events from a release that requires it. Keep a stub instead."""
    for release, files in RELEASED_WHOLE_FILES.items():
        for name in files:
            assert (TAP / name).is_file(), f"tap {release} requires {name} of the copy it hands over to"
    for script in SCRIPT.values():
        assert os.access(HOOKS / script, os.X_OK), script
    version = json.loads((TAP / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    if version in RELEASED_WHOLE_FILES:
        assert _current_whole_files() == RELEASED_WHOLE_FILES[version], f"tap {version}'s list is frozen"
    for name in _current_whole_files():
        assert (TAP / name).is_file(), name


def _release_0_10_17(root: Path) -> Path:
    """Tap 0.10.17 as released (its hooks, kept verbatim in fixtures; it has
    no hand-over of its own), marked like every copy here."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "tap-0.10.17" / "hooks"
    _copy(root, "0.10.17")
    shutil.rmtree(root / "hooks")
    shutil.copytree(fixture, root / "hooks")
    for script in SCRIPT.values():
        path = root / "hooks" / script
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        at = next(i for i, line in enumerate(lines) if line.startswith("set -") and "pipefail" in line)
        lines.insert(at + 1, _mark("0.10.17", script))
        path.write_text("".join(lines), encoding="utf-8")
    return root


@pytest.mark.parametrize("event", list(SCRIPT))
def test_a_rollback_to_the_released_0_10_17_is_followed(box, event):
    """The ledger points back at tap 0.10.17 (a revert): an open session on
    this tap runs 0.10.17's scripts, though 0.10.17 has no `installed-tap.sh`."""
    old = _release_0_10_17(box.claude / "plugins" / "cache" / MARKET / PLUGIN / "0.10.17")
    assert not (old / "hooks" / "installed-tap.sh").exists()
    new = box.claude_copy(OLD)
    box.ledger(old)
    done = box.run(event, box.env(CLAUDE_PLUGIN_ROOT=str(new)))
    assert done.returncode == 0, done.stderr
    (ran,) = box.ran()
    assert (ran["version"], ran["script"], ran["claude_root"]) == ("0.10.17", SCRIPT[event], str(old))
