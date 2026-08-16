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

On the way out (every path except the killswitch, whose contract is "ship
nothing") the daemon reads one last transcript tail and enqueues a FINALIZE for
the session. That finalize is what tells the engine the session is over, and
completion is the only thing that triggers its knowledge-unit extraction —
qa, code_change, decision, file_ref. Without it a session is captured as a live
transcript and never mined, until the server-side nightly sweep notices it has
been quiet for hours.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from tap import config as cfg
from tap import killswitch, outbox, reconcile
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
    def _handler(sig: int, _frame: object) -> None:
        global _shutdown_requested
        _shutdown_requested = True
        # The dying side's half of killer-side attribution: `probe`'s
        # _stop_daemon() journals every SIGTERM it sends (its pid + argv + the
        # target pid), and this line is the arrival timestamp it correlates
        # against. A TERM logged here with NO matching stop-daemon journal
        # entry means something else killed the daemon — which is exactly the
        # open question this anchor exists to answer. Best-effort: a logging
        # failure must never disturb shutdown. (Safe in CPython: handlers run
        # in the main thread between bytecodes, and logging's lock is an RLock,
        # so re-entering a log call the signal interrupted cannot deadlock.)
        with contextlib.suppress(Exception):
            log.info(
                "signal %s received (unix=%.3f pid=%d); shutting down",
                signal.Signals(sig).name,
                time.time(),
                os.getpid(),
            )

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _shutdown_observed(c: cfg.WatchConfig) -> bool:
    return _shutdown_requested or c.shutdown_sentinel.exists() or cfg.killswitch_active()


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


def _resolve_codex_transcript(transcript_dir: Path, session_id: str) -> Path | None:
    """Find Codex's date-partitioned rollout for one session id."""
    if not transcript_dir.is_dir():
        return None
    matches = sorted(transcript_dir.rglob(f"*{session_id}.jsonl"))
    return matches[0] if matches else None


def _wait_for_codex_transcript(
    transcript_dir: Path,
    session_id: str,
    *,
    poll_interval_s: float = 2.0,
    max_wait_s: int = 1800,
) -> Path | None:
    """Wait because Codex may fire SessionStart before creating its rollout."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        if _shutdown_requested or cfg.killswitch_active():
            return None
        path = _resolve_codex_transcript(transcript_dir, session_id)
        if path is not None:
            return path
        time.sleep(poll_interval_s)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap watch")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--transcript-dir", type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    # Optional on purpose: nothing in tap/ reads plugin_root, and a future hook
    # change that stops passing it must not argparse-exit the daemon into a
    # silent capture outage. session-start.sh still passes it (harmless).
    parser.add_argument("--plugin-root", required=False, default=None, type=Path)
    args = parser.parse_args(argv)
    if args.transcript is None and args.transcript_dir is None:
        parser.error("--transcript or --transcript-dir is required")
    if cfg.capture_source() != "codex" and args.transcript is None:
        parser.error("--transcript is required for Claude Code capture")

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

    transcript_path = args.transcript
    if transcript_path is None:
        assert args.transcript_dir is not None
        transcript_path = _wait_for_codex_transcript(args.transcript_dir, args.session_id)
        if transcript_path is None:
            log.info("Codex rollout did not appear before shutdown/timeout")
            with contextlib.suppress(OSError):
                cfg.shutdown_sentinel(args.session_id).touch()
            return 0

    active_s, idle_s = cfg.intervals()
    config = cfg.WatchConfig(
        session_id=args.session_id,
        transcript_path=transcript_path,
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
                "ingest token changed since last 401"
                if token_changed
                else "halt cooldown expired; re-probing"
                if cooldown_expired
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
        config.session_id,
        config.transcript_path,
        config.cwd,
        config.active_interval_s,
        config.idle_interval_s,
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
    batch_seq = (
        max(
            storage.max_batch_seq(c.session_id),
            _read_int_meta(storage, seq_meta_key, default=-1),
        )
        + 1
    )

    missing_ticks = 0
    tick_count = 0
    empty_ticks = 0
    in_idle_mode = False
    in_killswitch_mode = False
    ingestion_globally_enabled = True

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
        ks_enabled, ks_reason = killswitch.is_ingestion_enabled(token=c.token, base_url=base_url)
        ingestion_globally_enabled = ks_enabled
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
            batch_seq = _enqueue_read(c, storage, device_id, seq_meta_key, batch_seq, read)

        # Drain a bounded number of rows.
        try:
            _drain_pending(c, storage, base_url)
        except HaltError as e:
            log.error("halt: %s", e)
            return 1
        except Exception:
            log.exception("drain raised; will retry next tick")

        # Reconciliation sweep. Every live daemon periodically checks EVERY
        # local transcript against its stored cursor and drains EVERY due outbox
        # row, so a session whose daemon never attached (a resume that spawned
        # nothing, a fork whose file appeared after we gave up) is recovered by
        # whichever daemon is running next. Runs on the first tick so a single
        # new session is enough to start recovering, then on a slow cadence.
        #
        # A lease keeps concurrent daemons from sweeping at once. Failures are
        # swallowed by design: this is a safety net, and a net that can stop
        # capture is worse than no net. HaltError still propagates — a dead
        # token means the same thing here as anywhere else.
        if tick_count == 1 or tick_count % reconcile.RECONCILE_EVERY_TICKS == 0:
            try:
                res = reconcile.sweep(
                    storage,
                    token=c.token,
                    base_url=base_url,
                    device_id=device_id,
                )
                if res.files_backfilled or res.rows_drained:
                    log.info(
                        "reconcile: %d gap(s) found, %d file(s) backfilled (%d bytes, "
                        "%d batches), %d orphan row(s) drained",
                        res.gaps_found,
                        res.files_backfilled,
                        res.bytes_backfilled,
                        res.batches_enqueued,
                        res.rows_drained,
                    )
            except HaltError as e:
                log.error("halt during reconcile: %s", e)
                return 1
            except Exception:
                log.exception("reconcile sweep raised; continuing")

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
                # BREAK, not return: this is a session that ended WITHOUT a
                # SessionEnd hook — force-quit, SIGKILL, OS reboot — which is
                # precisely the case with no other way to say goodbye. Returning
                # here skipped both the final transcript tail and the finalize,
                # so a hard-killed session lost its last bytes and waited on the
                # server-side idle sweep to be mined at all. Falling through to
                # the shutdown path gives it the same ending a clean exit gets.
                break

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
                log.info(
                    "idle for %d ticks; switching to idle cadence (%ds)",
                    empty_ticks,
                    c.idle_interval_s,
                )
                in_idle_mode = True
        sleep_s = c.idle_interval_s if in_idle_mode else c.active_interval_s

        # Sleep in 1s slices so SIGTERM/sentinel/killswitch are responsive.
        slept = 0
        while slept < sleep_s and not _shutdown_observed(c):
            time.sleep(1)
            slept += 1

    # SessionEnd/SIGTERM commonly lands during the cadence sleep. Codex writes
    # the final response before firing the hook, so stopping here without one
    # last tail loses everything written since the previous tick (up to five
    # minutes in idle mode). Enqueue first so a transient network failure is
    # durable in SQLite; then make one bounded best-effort drain.
    #
    # A local killswitch is different from an ordinary shutdown: its contract
    # is "ship nothing", so do not read or drain after it becomes active.
    if not cfg.killswitch_active() and ingestion_globally_enabled:
        log.info("shutdown observed; capturing final transcript tail")
        try:
            final_read = _tick_read(c, storage)
        except FileNotFoundError:
            final_read = None
            # A fork/compaction leg gets a SessionStart for a NEW session id
            # whose file CC has not written yet (and may never write, if it
            # keeps appending to the original). Shutting down here used to be
            # the end of it — the file would appear afterwards, at megabytes,
            # with nothing left to notice. It is now recoverable: this daemon's
            # log file is what marks the session as one capture was live for,
            # so the reconciler adopts the file whenever it shows up. WARNING,
            # not INFO, because a run that captured nothing at all should be
            # visible in the log rather than read as a clean exit.
            log.warning(
                "transcript never materialised for this session; leaving it to the "
                "reconciler to adopt if it appears: %s",
                c.transcript_path,
            )
        except Exception:
            final_read = None
            log.exception("final transcript read failed")
        if final_read is not None:
            batch_seq = _enqueue_read(c, storage, device_id, seq_meta_key, batch_seq, final_read)

        # Say goodbye. The engine only runs its knowledge-unit extraction
        # (qa / code_change / decision / file_ref) on a session marked
        # COMPLETE, and this is the only signal that says so at the moment the
        # session actually ends — otherwise the session sits open until the
        # server-side nightly sweep notices it has gone quiet for hours.
        #
        # Enqueued, not POSTed inline: it goes through the same durable outbox
        # as every batch, so a network blip at shutdown leaves it queued for
        # the next daemon or the reconciler's global drain instead of losing
        # the ending. It is enqueued AFTER the final tail so it drains last
        # (rows are claimed by ascending id) and the engine sees the whole
        # transcript before it is told the session is over.
        batch_seq = _enqueue_finalize(c, storage, seq_meta_key, batch_seq)

        try:
            _drain_pending(c, storage, base_url)
        except HaltError as exc:
            log.error("halt during final drain: %s", exc)
            return 1
        except Exception:
            # The batch is already durable in the outbox and a future daemon
            # will retry it; shutdown must not turn a network blip into loss.
            log.exception("final drain raised; batch remains queued")

    return 0


def _enqueue_read(
    c: cfg.WatchConfig,
    storage: Storage,
    device_id: str,
    seq_meta_key: str,
    batch_seq: int,
    read: tuple[list[bytes], int, Callable[[int | None], None]],
) -> int:
    """Durably enqueue one transcript read and return the next batch sequence.

    The read is SPLIT into gateway-sized batches. One tick can hold far more
    than the gateway's 2MB body cap — the first tick against a transcript that
    already has history, a catch-up after the 300s idle cadence, or a daemon
    that started late — and an oversized body comes back 413, which
    `httpclient.classify` calls POISON and the outbox DROPS. That silently lost
    the whole tick, permanently. `reconcile.chunk_lines` is the same splitter
    the backfill path has always used against the same cap; the live tail was
    the one caller that never got it.
    """
    raw_lines, line_no_base, commit_offset = read
    if not raw_lines:
        # No lines this tick — still refresh last_seen_at + inode/size.
        commit_offset(None)
        return batch_seq

    line_no = line_no_base
    consumed = 0
    invalid = 0

    for group in reconcile.chunk_lines(raw_lines):
        valid = [ln for ln in group if validate_json(ln)]
        invalid += len(group) - len(valid)
        if valid:
            body = outbox.build_batch_body(
                device_id=device_id,
                session_id=c.session_id,
                batch_seq=batch_seq,
                cwd=str(c.cwd),
                base_line_no=line_no,
                lines=valid,
            )
            # None means the sanitizer dropped every event in this group (a
            # chunk of pure bookkeeping). The lines were still processed, so
            # they count as consumed rather than being re-read forever.
            if body is not None:
                try:
                    outbox.enqueue(
                        storage=storage,
                        session_id=c.session_id,
                        batch_seq=batch_seq,
                        cwd=str(c.cwd),
                        body=body,
                        now=int(time.time()),
                    )
                except Exception:
                    # Commit what DID enqueue and leave the rest for next tick.
                    # Bailing without a partial commit would re-read the whole
                    # tick and re-ship the already-queued groups under fresh
                    # sequence numbers — duplicated events, not lost ones.
                    log.exception(
                        "enqueue failed after %d line(s); committing those and "
                        "retrying the rest next tick",
                        consumed,
                    )
                    break
                # Persist the high-water mark BEFORE incrementing so a crash
                # here does not reset the counter on restart.
                storage.set_meta(seq_meta_key, str(batch_seq))
                batch_seq += 1
        line_no += len(group)
        consumed += len(group)

    if invalid:
        log.warning("dropped %d malformed JSON lines this tick", invalid)
    if consumed:
        commit_offset(None if consumed >= len(raw_lines) else consumed)
    return batch_seq


def _enqueue_finalize(
    c: cfg.WatchConfig,
    storage: Storage,
    seq_meta_key: str,
    batch_seq: int,
) -> int:
    """Durably enqueue this session's finalize and return the next sequence.

    Takes a batch_seq of its own rather than reusing the last one: the outbox
    is UNIQUE(session_id, batch_seq), so sharing a number with a queued batch
    would raise and lose the goodbye. The number is otherwise meaningless to
    the server — the gateway validates a finalize against a two-field model and
    never reads a sequence off it — it exists here purely to order the row.

    Best-effort by design. A session that fails to enqueue its finalize is
    exactly the case the server-side nightly sweep exists for, so this must
    never escalate: shutdown continues, the last batches still drain, and the
    session gets mined a few hours later instead of immediately.
    """
    try:
        outbox.enqueue(
            storage=storage,
            session_id=c.session_id,
            batch_seq=batch_seq,
            cwd=str(c.cwd),
            body=outbox.build_finalize_body(session_id=c.session_id),
            now=int(time.time()),
        )
        storage.set_meta(seq_meta_key, str(batch_seq))
        log.info("enqueued session finalize (batch_seq=%d)", batch_seq)
        return batch_seq + 1
    except Exception:
        log.exception(
            "could not enqueue session finalize; the server-side idle sweep "
            "will finalize this session instead"
        )
        return batch_seq


def _drain_pending(c: cfg.WatchConfig, storage: Storage, base_url: str) -> None:
    """Drain a bounded number of rows for this session."""
    drained = 0
    while drained < MAX_DRAIN_PER_TICK and outbox.drain_once(
        storage=storage,
        token=c.token,
        base_url=base_url,
        session_id=c.session_id,
    ):
        drained += 1


def _tick_read(
    c: cfg.WatchConfig, storage: Storage
) -> tuple[list[bytes], int, Callable[[int | None], None]]:
    """Read new lines from the transcript; do NOT persist offset.

    Returns (raw_lines, base_line_no_for_first_line, commit_fn).

    Lines come back UNFILTERED. Validation moved to _enqueue_read, per chunk,
    because the cursor now advances by a COUNT of lines consumed and that count
    has to address the file: dropping malformed lines here would make "n lines
    consumed" stop mapping to a byte position, and a partial commit would land
    the cursor in the wrong place.

    commit_fn(consumed_lines) advances the cursor by that many lines; None means
    the whole read. Until it is called the cursor stays put, so a failed enqueue
    re-reads the same bytes next tick.
    """
    path_str = str(c.transcript_path)
    prev = storage.get_offset(path_str)
    prev_byte = prev.byte_offset if prev else 0
    last_line_no = prev.last_line_no if prev else 0

    res = read_new(c.transcript_path, prev_byte)

    def commit(consumed_lines: int | None = None) -> None:
        if consumed_lines is None or consumed_lines >= len(res.lines):
            byte_offset = res.new_byte_offset
            line_no = last_line_no + len(res.lines)
        else:
            # Partial: keep the bytes we actually shipped and leave the rest to
            # be re-read. Re-derived from the file rather than summed from line
            # lengths — split_lines strips \r and skips blanks, so summed
            # lengths are not a file position.
            byte_offset = reconcile.byte_offset_after(
                c.transcript_path, prev_byte, consumed_lines
            )
            line_no = last_line_no + consumed_lines
        storage.upsert_offset(
            FileOffset(
                path=path_str,
                session_id=c.session_id,
                cwd=str(c.cwd),
                last_line_no=line_no,
                last_seen_at=int(time.time()),
                inode=res.inode,
                size=res.file_size,
                byte_offset=byte_offset,
            )
        )

    return res.lines, last_line_no, commit


if __name__ == "__main__":
    sys.exit(main())
