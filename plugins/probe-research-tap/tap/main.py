"""Daemon loop — `python -m tap watch ...`.

Spawned by hooks/session-start.sh. Reads new transcript content, batches +
enqueues, drains the outbox, sleeps, repeats.

Adaptive cadence: ticks at the active interval (default 60s) while the
transcript is advancing; after IDLE_THRESHOLD_TICKS consecutive empty ticks
falls back to the idle interval (default 300s). A user typing in CC gets
near-real-time ingestion; an idle session stops generating backend traffic.
Set sync_interval_seconds in .config for a flat cadence that disables
adaptive switching.

Exits cleanly on:
  - SIGTERM/SIGINT
  - shutdown sentinel /tmp/probe-research-tap-watcher-<sid>.shutdown
  - killswitch ~/.claude/plugins/probe-research-tap/.disabled
  - cwd matching .disabled_paths
  - 401 halt from the server
  - transcript file missing for 5 ticks (file deleted / session torn down)
  - orphan session detected (no process holds the transcript open) —
    happens when CC is hard-killed (SIGKILL / OS reboot / force-quit) and
    SessionEnd never fires; touches the shutdown sentinel so the wrapper
    exits too instead of respawning a doomed daemon
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from tap import config as cfg
from tap import killswitch, outbox
from tap.outbox import HaltError
from tap.storage import FileOffset, Storage
from tap.transcript import read_new, validate_json

log = logging.getLogger("probe-research-tap")

# Drain budget per tick — keep ticking responsive even if many batches are due.
MAX_DRAIN_PER_TICK = 64

# Switch to idle cadence after this many consecutive empty ticks (no new
# transcript bytes). 2 means: a single empty tick stays on active in case
# the user is mid-sentence; two in a row means they've stopped typing.
IDLE_THRESHOLD_TICKS = 2

# Run the orphan-session check (lsof on transcript) every N ticks. At the
# active interval, 12 ticks ≈ 12 minutes; at idle, ≈ 1 hour. lsof is a
# subprocess and we don't need fast detection — orphans only matter for
# tidy cleanup.
ORPHAN_CHECK_EVERY_TICKS = 12

# Hard cap on how long we'll wait for lsof to return; if it hangs, we'd
# rather assume "alive" and skip than block the tick.
ORPHAN_LSOF_TIMEOUT_S = 5

# After a 401 halt, a daemon start older than this cooldown clears the latch and
# re-probes instead of staying wedged forever. A transient 401 (member removed
# then re-added with the SAME still-valid token) leaves no fingerprint change to
# self-clear on, so the cooldown is the only path back — a periodic re-probe that
# self-heals. 1h keeps the re-POST rate on a genuinely-dead token negligible.
HALT_RETRY_AFTER_SECONDS = 3600

_shutdown_requested = False


def _batch_seq_meta_key(session_id: str) -> str:
    return f"last_batch_seq:{session_id}"


def _read_int_meta(storage: Storage, key: str, *, default: int) -> int:
    """Read a meta value as int, returning `default` for missing/malformed."""
    raw = storage.get_meta(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("meta[%s]=%r is not an int; treating as %d", key, raw, default)
        return default


def _install_signal_handlers() -> None:
    def _handler(_sig: int, _frame: object) -> None:
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _shutdown_observed(c: cfg.WatchConfig) -> bool:
    return (
        _shutdown_requested
        or c.shutdown_sentinel.exists()
        or cfg.killswitch_active()
    )


def _transcript_has_active_reader(path: Path) -> bool | None:
    """True/False if lsof can determine; None if lsof is unavailable.

    `lsof -t -- <path>` lists PIDs that hold an open fd on `path`. The daemon
    itself opens the transcript only briefly inside _tick_read, so when this
    function runs (after the tick's read+enqueue completed) the daemon's own
    fd is closed and won't show up. CC keeps the transcript fd open for the
    session's lifetime, so an empty result means CC is dead.

    Returning None (lsof not installed, weird container, timeout) is treated
    by the caller as "can't tell, assume alive" — we never orphan-exit on
    ambiguous signal.
    """
    try:
        result = subprocess.run(
            ["lsof", "-t", "--", str(path)],
            capture_output=True,
            timeout=ORPHAN_LSOF_TIMEOUT_S,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return bool(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap watch")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    # Optional on purpose: nothing in tap/ reads plugin_root, and a future hook
    # change that stops passing it must not argparse-exit the daemon into a
    # silent capture outage. session-start.sh still passes it (harmless).
    parser.add_argument("--plugin-root", required=False, default=None, type=Path)
    args = parser.parse_args(argv)

    log_dir = cfg.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    # FileHandler only — the wrapper bash already redirects this python
    # process's stdout+stderr into the same log file via `>>"$LOG" 2>&1`.
    # Adding a StreamHandler too would double every line in the log.
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / f"{args.session_id}.log")],
    )
    _install_signal_handlers()

    if cfg.killswitch_active():
        log.info("killswitch active, exiting")
        return 0
    if cfg.cwd_disabled(args.cwd):
        log.info("cwd %s matched .disabled_paths, exiting", args.cwd)
        return 0

    token = cfg.load_token()
    if not token:
        log.info(
            "no ingest token configured (PROBE_INGEST_TOKEN or ingest_token in %s); "
            "run `probe login` first — skipping",
            cfg.probe_config_path(),
        )
        return 0

    # Resolve the backend host once up front. No hardcoded fallback: if it's
    # unset, there's nothing to ship to, so stop cleanly instead of crash-
    # looping against a host that doesn't exist. The wrapper respawns on any
    # exit code, so we touch the shutdown sentinel (same mechanism the orphan-
    # exit path uses) to actually stop it for this session.
    try:
        cfg.api_base_url()
    except cfg.APIBaseURLUnset as exc:
        log.error("%s; not starting daemon", exc)
        with contextlib.suppress(OSError):
            cfg.shutdown_sentinel(args.session_id).touch()
        return 0

    active_s, idle_s = cfg.intervals()
    config = cfg.WatchConfig(
        session_id=args.session_id,
        transcript_path=args.transcript,
        cwd=args.cwd,
        plugin_root=args.plugin_root,
        token=token,
        active_interval_s=active_s,
        idle_interval_s=idle_s,
    )

    storage = Storage(cfg.state_db_path())

    # 401-halt latch. There is no pairing step to clear it, so a daemon start
    # decides whether the halt still holds. It clears (and resumes) in ANY of
    # three cases, so a halt is never held longer than it can be justified:
    #   (a) the configured ingest token differs from the one the server rejected
    #       (user ran `probe login` or changed PROBE_INGEST_TOKEN) — the fix;
    #   (b) the halt is older than HALT_RETRY_AFTER_SECONDS — a periodic re-probe
    #       that self-heals a transient 401 (e.g. member removed then re-added
    #       with the SAME still-valid token, which leaves no fingerprint change);
    #   (c) no rejected-token fingerprint was recorded — a crash could split the
    #       timestamp from the fingerprint (now written atomically, but an old
    #       split state may persist), and we do not hold a halt we can't justify.
    if storage.get_meta("last_401_at"):
        rejected_fp = storage.get_meta("last_401_token_sha256")
        last_401_at = _read_int_meta(storage, "last_401_at", default=0)
        now = int(time.time())
        token_changed = bool(rejected_fp) and rejected_fp != outbox.token_fingerprint(token)
        cooldown_expired = last_401_at > 0 and (now - last_401_at) > HALT_RETRY_AFTER_SECONDS
        no_fingerprint = not rejected_fp
        if token_changed or cooldown_expired or no_fingerprint:
            reason = (
                "ingest token changed since last 401" if token_changed
                else "halt cooldown expired; re-probing" if cooldown_expired
                else "no rejected-token fingerprint recorded"
            )
            log.info("clearing 401 halt (%s) and resuming", reason)
            storage.delete_meta("last_401_at")
            storage.delete_meta("last_401_token_sha256")
        else:
            log.warning(
                "halted: last_401_at set — fix PROBE_INGEST_TOKEN or run "
                "`probe login` with a valid ingest token to resume"
            )
            storage.close()
            return 1

    log.info(
        "tap starting session=%s transcript=%s cwd=%s active=%ds idle=%ds",
        config.session_id, config.transcript_path, config.cwd,
        config.active_interval_s, config.idle_interval_s,
    )
    try:
        return _run_loop(config, storage)
    finally:
        storage.close()
        log.info("tap exited")


def _run_loop(c: cfg.WatchConfig, storage: Storage) -> int:
    base_url = cfg.api_base_url()

    # Device identity: nothing mints one server-side anymore (no pairing), so
    # the daemon owns it — generate once, persist in meta, send in every batch
    # body. The backend passes it through to the engine, which uses it as the
    # device external id.
    #
    # Mint atomically: a fresh install with two CC sessions in the same minute
    # would otherwise have both daemons read device_id="" and write DIFFERENT
    # uuids (last-writer-wins), forking machine identity. insert_meta_if_absent
    # is a single atomic INSERT ... ON CONFLICT DO NOTHING + re-read, so both
    # converge on the first writer's id. We generate unconditionally (cheap) and
    # let the atomic insert decide the winner.
    minted = uuid.uuid4().hex
    device_id = storage.insert_meta_if_absent("device_id", minted)
    if device_id == minted:
        log.info("generated device_id=%s", device_id)

    # Resume batch_seq across daemon restarts.
    #
    # batch_seq must stay monotonic/unique because the R2 storage key the
    # upstream writes is "<session>:<batch_seq>". Per prbe-knowledge origin/main
    # (post-migration-0026) the ingest queue COALESCES on the bare session_id —
    # it does NOT dedup on source_event_id — so a reset seq would not be dropped
    # at the queue; instead it would re-derive an EARLIER storage key and
    # overwrite that batch's blob in R2 (last-write-wins), losing data. Keeping
    # the seq durable and always-increasing keeps every batch's R2 key distinct.
    #
    # max_batch_seq(outbox) only knows about batches still queued locally;
    # successful drains delete those rows, so it returns -1 after the daemon
    # catches up and restarts. We keep a durable high-water mark in `meta`
    # under "last_batch_seq:<session>" and bump it after every enqueue, so a
    # restart picks up at last_seq+1 instead of 0.
    seq_meta_key = _batch_seq_meta_key(c.session_id)
    batch_seq = max(
        storage.max_batch_seq(c.session_id),
        _read_int_meta(storage, seq_meta_key, default=-1),
    ) + 1

    missing_ticks = 0
    tick_count = 0
    empty_ticks = 0
    in_idle_mode = False
    in_killswitch_mode = False

    # Track whether we ever saw a process holding the transcript fd. Without
    # this gate, an early lsof miss (e.g. before CC has fully opened the file)
    # would orphan-exit a healthy daemon. We only treat "no reader" as orphan
    # if we previously observed a reader.
    seen_active_reader = False

    while not _shutdown_observed(c):
        tick_count += 1

        # Ingestion killswitch poll (fetched + cached for 5min). This is the
        # SEAM for a future customer-level pause: the status endpoint is static
        # ({"ingest_enabled": true}) today, so this never trips in production
        # yet. If it ever reports paused we skip the entire tick — no tail, no
        # enqueue, no drain — and byte_offset stays put so the next enabled tick
        # catches up automatically. On poll error we fail OPEN inside
        # is_ingestion_enabled itself; here we just consume the (enabled, reason)
        # tuple.
        ks_enabled, ks_reason = killswitch.is_ingestion_enabled(
            token=c.token, base_url=base_url
        )
        if not ks_enabled:
            if not in_killswitch_mode:
                log.info(
                    "ingestion paused via global killswitch (reason=%s)",
                    ks_reason or "no reason given",
                )
                in_killswitch_mode = True
            time.sleep(c.idle_interval_s)
            continue
        elif in_killswitch_mode:
            log.info("ingestion resumed; global killswitch released")
            in_killswitch_mode = False

        try:
            read = _tick_read(c, storage)
        except FileNotFoundError:
            missing_ticks += 1
            log.warning("transcript missing (tick %d): %s", missing_ticks, c.transcript_path)
            if missing_ticks >= 5:
                log.warning("transcript missing for %d ticks, exiting", missing_ticks)
                return 0
            read = None
        else:
            missing_ticks = 0

        if read is not None:
            new_lines, line_no_base, commit_offset = read
            committed = False
            if new_lines:
                now = int(time.time())
                body = outbox.build_batch_body(
                    device_id=device_id,
                    session_id=c.session_id,
                    batch_seq=batch_seq,
                    cwd=str(c.cwd),
                    base_line_no=line_no_base,
                    lines=new_lines,
                )
                if body is None:
                    # Sanitizer dropped every event in this tick (e.g. a tick
                    # that only saw stop_hook_summary + turn_duration). No
                    # webhook to ship, but the lines were "processed" — commit
                    # the offset so we don't re-read them next tick.
                    commit_offset()
                    committed = True
                else:
                    try:
                        outbox.enqueue(
                            storage=storage,
                            session_id=c.session_id,
                            batch_seq=batch_seq,
                            cwd=str(c.cwd),
                            body=body,
                            now=now,
                        )
                        # Persist the high-water mark BEFORE incrementing so a
                        # crash here doesn't reset the counter on restart.
                        storage.set_meta(seq_meta_key, str(batch_seq))
                        batch_seq += 1
                        commit_offset()
                        committed = True
                    except Exception:
                        # Offset NOT advanced; same lines are re-read next tick.
                        log.exception("enqueue failed; lines will be re-read next tick")
            if not committed and not new_lines:
                # No lines this tick — still refresh last_seen_at + inode/size.
                commit_offset()

        # Drain a bounded number of rows.
        try:
            drained = 0
            while drained < MAX_DRAIN_PER_TICK and outbox.drain_once(
                storage=storage, token=c.token, base_url=base_url,
                session_id=c.session_id,
            ):
                drained += 1
        except HaltError as e:
            log.error("halt: %s", e)
            return 1
        except Exception:
            log.exception("drain raised; will retry next tick")

        # Orphan-session detection. CC keeps the transcript fd open for the
        # session's lifetime; if no process holds it, the session is gone.
        # Only trips after we've previously observed a reader, so a startup
        # race or a system without lsof can't false-positive us into exit.
        if tick_count % ORPHAN_CHECK_EVERY_TICKS == 0:
            has_reader = _transcript_has_active_reader(c.transcript_path)
            if has_reader is True:
                seen_active_reader = True
            elif has_reader is False and seen_active_reader:
                log.info(
                    "no process holds %s open; CC session ended without SessionEnd, exiting",
                    c.transcript_path,
                )
                # Touch the sentinel so the wrapper exits instead of respawning
                # us into the same dead-session state.
                with contextlib.suppress(OSError):
                    c.shutdown_sentinel.touch()
                return 0

        # Adaptive cadence: a tick that produced new lines resets to active;
        # IDLE_THRESHOLD_TICKS empty ticks in a row promotes to idle. We
        # treat "transcript missing" the same as empty since there's nothing
        # to ship either way.
        had_lines = read is not None and bool(read[0])
        if had_lines:
            empty_ticks = 0
            if in_idle_mode:
                log.info("activity resumed; switching to active cadence (%ds)", c.active_interval_s)
                in_idle_mode = False
        else:
            empty_ticks += 1
            if empty_ticks == IDLE_THRESHOLD_TICKS and not in_idle_mode:
                log.info("idle for %d ticks; switching to idle cadence (%ds)",
                         empty_ticks, c.idle_interval_s)
                in_idle_mode = True
        sleep_s = c.idle_interval_s if in_idle_mode else c.active_interval_s

        # Sleep in 1s slices so SIGTERM/sentinel/killswitch are responsive.
        slept = 0
        while slept < sleep_s and not _shutdown_observed(c):
            time.sleep(1)
            slept += 1

    return 0


def _tick_read(
    c: cfg.WatchConfig, storage: Storage
) -> tuple[list[bytes], int, Callable[[], None]]:
    """Read new lines from the transcript and validate; do NOT persist offset.

    Returns (validated_lines, base_line_no_for_first_line, commit_fn). The
    caller invokes commit_fn once it has successfully enqueued a batch (or
    decided to commit even with no new lines). Until then, the cursor stays
    where it was so a failed enqueue re-reads the same bytes next tick.
    """
    path_str = str(c.transcript_path)
    prev = storage.get_offset(path_str)
    prev_byte = prev.byte_offset if prev else 0
    last_line_no = prev.last_line_no if prev else 0

    res = read_new(c.transcript_path, prev_byte)

    valid: list[bytes] = []
    invalid_count = 0
    for line in res.lines:
        if validate_json(line):
            valid.append(line)
        else:
            invalid_count += 1

    base_line_no = last_line_no
    new_last_line_no = last_line_no + len(res.lines)

    if invalid_count:
        log.warning("dropped %d malformed JSON lines this tick", invalid_count)

    def commit() -> None:
        storage.upsert_offset(FileOffset(
            path=path_str,
            session_id=c.session_id,
            cwd=str(c.cwd),
            last_line_no=new_last_line_no,
            last_seen_at=int(time.time()),
            inode=res.inode,
            size=res.file_size,
            byte_offset=res.new_byte_offset,
        ))

    return valid, base_line_no, commit


if __name__ == "__main__":
    sys.exit(main())
