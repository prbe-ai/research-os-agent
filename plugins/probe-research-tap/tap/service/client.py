"""`tap.service.client` as it was before the client split (agent:16): every name re-exported from the
vendored pieces beside it; `python -m tap.service.client` still runs the CLI. Tap-owned, not a vendored copy."""

from __future__ import annotations

import sys

from . import paths as paths
from . import procid as procid
from .calls import (
    CHECK_NOW_WAIT_S as CHECK_NOW_WAIT_S,
)
from .calls import (
    WAIT_FOR_PID_S as WAIT_FOR_PID_S,
)
from .calls import (
    _dict as _dict,
)
from .calls import (
    check_now as check_now,
)
from .calls import (
    desktop_request as desktop_request,
)
from .calls import (
    drain_outbox as drain_outbox,
)
from .calls import (
    end as end,
)
from .calls import (
    finish_recorders as finish_recorders,
)
from .calls import (
    hold_run as hold_run,
)
from .calls import (
    locations as locations,
)
from .calls import (
    owner_of as owner_of,
)
from .calls import (
    queue_hook as queue_hook,
)
from .calls import (
    refresh_manifest as refresh_manifest,
)
from .calls import (
    refresh_statusline as refresh_statusline,
)
from .calls import (
    register as register,
)
from .calls import (
    release_run as release_run,
)
from .calls import (
    send_telemetry as send_telemetry,
)
from .calls import (
    start_import as start_import,
)
from .calls import (
    sync_notes as sync_notes,
)
from .calls import (
    wait_for_exit as wait_for_exit,
)
from .calls import (
    watch_box as watch_box,
)
from .cli import (
    main as main,
)
from .env import (
    REPORT_ENV as REPORT_ENV,
)
from .env import (
    report_consent as report_consent,
)
from .service_api import (
    DESKTOP_TIMEOUT_S as DESKTOP_TIMEOUT_S,
)
from .service_api import (
    HANDOFF_BUDGET_S as HANDOFF_BUDGET_S,
)
from .service_api import (
    TIMEOUT_S as TIMEOUT_S,
)
from .service_api import (
    AnswerTimeout as AnswerTimeout,
)
from .service_api import (
    Connection as Connection,
)
from .service_api import (
    ServiceError as ServiceError,
)
from .service_api import (
    Unanswered as Unanswered,
)
from .service_api import (
    call as call,
)
from .service_api import (
    call_within as call_within,
)
from .service_api import (
    connect as connect,
)
from .start import (
    _BREAKAWAY as _BREAKAWAY,
)
from .start import (
    _DETACHED as _DETACHED,
)
from .start import (
    ENV_ALLOWLIST as ENV_ALLOWLIST,
)
from .start import (
    MAX_STARTS as MAX_STARTS,
)
from .start import (
    SPAWN_SETTLE_S as SPAWN_SETTLE_S,
)
from .start import (
    START_LOCK_WAIT_S as START_LOCK_WAIT_S,
)
from .start import (
    START_WINDOW_S as START_WINDOW_S,
)
from .start import (
    UNINSTALLING_MESSAGE as UNINSTALLING_MESSAGE,
)
from .start import (
    Ensured as Ensured,
)
from .start import (
    _children as _children,
)
from .start import (
    _clean_env as _clean_env,
)
from .start import (
    _lock as _lock,
)
from .start import (
    _pid_runs as _pid_runs,
)
from .start import (
    _read_starts as _read_starts,
)
from .start import (
    _reap as _reap,
)
from .start import (
    _spawn as _spawn,
)
from .start import (
    _venv_base as _venv_base,
)
from .start import (
    _without_venv_launcher as _without_venv_launcher,
)
from .start import (
    _write_starts as _write_starts,
)
from .start import (
    ensure as ensure,
)
from .start import (
    forget_start as forget_start,
)
from .start import (
    no_new_privs as no_new_privs,
)
from .start import (
    runtime_python as runtime_python,
)
from .start import (
    uninstall_barrier as uninstall_barrier,
)
from .start import (
    write_runtime as write_runtime,
)
from .vocabularies import (
    CHECK_TRIGGERS as CHECK_TRIGGERS,
)
from .vocabularies import (
    Client as Client,
)
from .vocabularies import (
    Mode as Mode,
)
from .wire import (
    API_VERSION as API_VERSION,
)
from .wire import (
    JSONRPC as JSONRPC,
)
from .wire import (
    MAX_LINE_BYTES as MAX_LINE_BYTES,
)
from .wire import (
    Method as Method,
)
from .wire import (
    encode as encode,
)
from .wire import (
    request as request,
)

if __name__ == "__main__":
    sys.exit(main())
