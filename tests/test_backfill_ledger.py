"""The ledger: what a backfill has already done, and what a crash left behind.

Two invariants carry the weight here. A unit that started and never finished
must come back as outstanding, or a resume silently drops whatever was in flight
when the process died. And a torn final line must cost one record, not the whole
import's history.
"""

from __future__ import annotations

import json

import pytest

from probe.cli import backfill_ledger as bl


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    return d


def _units(*specs):
    return [
        bl.Unit(unit_id=uid, project=proj, paths=tuple(paths))
        for uid, proj, paths in specs
    ]


# -- the basic fold ----------------------------------------------------------


def test_an_unopened_ledger_reads_as_empty(tmp_path):
    assert bl.Ledger(tmp_path / "nope.jsonl").read().planned is False


def test_census_then_plan_folds_into_state(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.open_import(tmp_path, files=42, bytes_=1234)
    led.record_plan(_units(("u1", "odyssey", ["a.py"]), ("u2", "esm3", ["b.py"])), ["odyssey", "esm3"])
    st = led.read()
    assert (st.census_files, st.census_bytes) == (42, 1234)
    assert sorted(st.projects) == ["esm3", "odyssey"]
    assert st.progress() == (0, 2)


def test_a_finished_unit_stops_being_outstanding(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"]), ("u2", "p", ["b"])), ["p"])
    led.start_unit("u1", session_id="sess-1")
    led.finish_unit("u1", ok=True, enqueued=7)
    st = led.read()
    assert st.progress() == (1, 2)
    assert [r.unit.unit_id for r in st.outstanding()] == ["u2"]
    assert st.units["u1"].enqueued == 7


def test_a_failed_unit_stays_outstanding_and_keeps_its_error(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    led.start_unit("u1")
    led.finish_unit("u1", ok=False, error="agent died")
    st = led.read()
    assert [r.unit.unit_id for r in st.outstanding()] == ["u1"]
    assert st.units["u1"].error == "agent died"


# -- the crash signal --------------------------------------------------------


def test_a_unit_left_running_is_treated_as_crashed_not_done(tmp_path, state_dir):
    """The whole reason this is a log and not a status field."""
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    led.start_unit("u1", session_id="sess-9")
    st = led.read()
    assert st.units["u1"].state is bl.UnitState.RUNNING
    assert [r.unit.unit_id for r in st.outstanding()] == ["u1"]
    assert st.units["u1"].session_id == "sess-9"


def test_retries_are_counted_so_a_poison_unit_is_visible(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    for _ in range(3):
        led.start_unit("u1")
        led.finish_unit("u1", ok=False, error="boom")
    assert led.read().units["u1"].attempts == 3


def test_a_retry_after_a_failure_can_succeed(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    led.start_unit("u1")
    led.finish_unit("u1", ok=False, error="boom")
    led.start_unit("u1")
    led.finish_unit("u1", ok=True, enqueued=3)
    st = led.read()
    assert st.outstanding() == []
    assert st.units["u1"].error is None


# -- durability --------------------------------------------------------------


def test_a_torn_final_line_costs_one_record_not_the_history(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"]), ("u2", "p", ["b"])), ["p"])
    led.finish_unit("u1", ok=True)
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": "unit", "unit_id": "u2", "sta')  # killed mid-write
    st = led.read()
    assert st.units["u1"].state is bl.UnitState.DONE
    assert st.units["u2"].state is bl.UnitState.PENDING


def test_a_record_for_an_unknown_unit_is_ignored(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    led.finish_unit("ghost", ok=True)
    assert led.read().progress() == (0, 1)


def test_the_ledger_is_written_outside_the_folder_being_imported(tmp_path, state_dir):
    """A shared drive is routinely read-only, and writing bookkeeping into
    somebody else's dataset directory is rude even when it is permitted."""
    drive = tmp_path / "drive"
    drive.mkdir()
    led = bl.Ledger.for_folder(drive)
    led.open_import(drive, files=1, bytes_=1)
    assert state_dir in led.path.parents
    assert drive not in led.path.parents
    assert not list(drive.iterdir()), "the import folder must be left untouched"


def test_the_ledger_file_is_not_world_readable(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.open_import(tmp_path, files=1, bytes_=1)
    assert (led.path.stat().st_mode & 0o077) == 0


def test_a_replan_replaces_the_units_rather_than_merging_them(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("old", "p", ["a"])), ["p"])
    led.record_plan(_units(("new", "q", ["b"])), ["q"])
    st = led.read()
    assert set(st.units) == {"new"}
    assert st.projects == ["q"]


# -- approval ----------------------------------------------------------------


def test_an_approved_plan_records_it_so_resume_does_not_re_ask(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    assert led.read().approved_at is None
    led.record_approval()
    assert led.read().approved_at is not None


# -- identity across a remount ----------------------------------------------


def test_the_same_path_resumes_exactly(tmp_path, state_dir):
    folder = tmp_path / "drive"
    (folder / "michael").mkdir(parents=True)
    led = bl.Ledger.for_folder(folder)
    led.open_import(folder, files=1, bytes_=1)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    found = bl.find_resumable(folder)
    assert found is not None
    assert found[0].path == led.path


def test_a_folder_that_came_back_on_another_mount_point_is_still_found(tmp_path, state_dir):
    """Keying purely on the path is how a resumed import silently starts over
    when a shared drive remounts somewhere else."""
    first = tmp_path / "mnt" / "research"
    (first / "michael").mkdir(parents=True)
    (first / "xian").mkdir(parents=True)
    led = bl.Ledger.for_folder(first)
    led.open_import(first, files=2, bytes_=2)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])

    second = tmp_path / "Volumes" / "research"
    (second / "michael").mkdir(parents=True)
    (second / "xian").mkdir(parents=True)

    found = bl.find_resumable(second)
    assert found is not None, "a remounted folder should be offered, not ignored"
    assert found[0].path == led.path


def test_a_differently_shaped_folder_is_not_matched(tmp_path, state_dir):
    first = tmp_path / "a"
    (first / "michael").mkdir(parents=True)
    led = bl.Ledger.for_folder(first)
    led.open_import(first, files=1, bytes_=1)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])

    other = tmp_path / "b"
    (other / "totally-different").mkdir(parents=True)
    assert bl.find_resumable(other) is None


def test_a_finished_import_is_not_offered_as_resumable(tmp_path, state_dir):
    folder = tmp_path / "drive"
    folder.mkdir()
    led = bl.Ledger.for_folder(folder)
    led.open_import(folder, files=1, bytes_=1)
    led.record_plan(_units(("u1", "p", ["a"])), ["p"])
    led.finish_unit("u1", ok=True)
    assert bl.find_resumable(folder) is None


def test_the_fingerprint_ignores_dotfiles_so_tooling_noise_does_not_break_it(tmp_path):
    folder = tmp_path / "drive"
    (folder / "michael").mkdir(parents=True)
    before = bl.fingerprint(folder)
    (folder / ".DS_Store").write_text("noise")
    (folder / ".probe").mkdir()
    assert bl.fingerprint(folder) == before


# -- shape -------------------------------------------------------------------


def test_a_unit_is_a_set_of_paths_not_a_directory(tmp_path, state_dir):
    """Classification is per-file, so a unit spans directories by construction."""
    led = bl.Ledger.for_folder(tmp_path)
    scattered = ("michael/train.py", "shared/data/rows.csv", "xian/notes.md")
    led.record_plan(_units(("u1", "odyssey", list(scattered))), ["odyssey"])
    unit = led.read().units["u1"].unit
    assert unit.paths == scattered
    assert unit.files == 3


def test_every_record_carries_the_schema_and_a_timestamp(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    led.open_import(tmp_path, files=1, bytes_=1)
    rec = json.loads(led.path.read_text().splitlines()[0])
    assert rec["schema"] == bl.SCHEMA and rec["at"]
