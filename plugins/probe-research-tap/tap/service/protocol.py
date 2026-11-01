"""`tap.service.protocol` as it was before the protocol split (agent:16): every name re-exported from
the vendored pieces beside it. Tap-owned, not a vendored copy."""

from __future__ import annotations

from .env import (
    _REPORTS_OFF as _REPORTS_OFF,
)
from .env import (
    BASE_URL_ENV as BASE_URL_ENV,
)
from .env import (
    CONTEXT_ENV as CONTEXT_ENV,
)
from .env import (
    CREDENTIAL_ENV as CREDENTIAL_ENV,
)
from .env import (
    HOOK_EVENT_ID_LEN as HOOK_EVENT_ID_LEN,
)
from .env import (
    HOOK_LANES as HOOK_LANES,
)
from .env import (
    HOOK_QUEUE_ENV_CREDENTIAL as HOOK_QUEUE_ENV_CREDENTIAL,
)
from .env import (
    HOOK_QUEUE_ENV_NAMES as HOOK_QUEUE_ENV_NAMES,
)
from .env import (
    HOOK_QUEUE_ENV_PREFIXES as HOOK_QUEUE_ENV_PREFIXES,
)
from .env import (
    HOOK_QUEUE_ENV_SECRET as HOOK_QUEUE_ENV_SECRET,
)
from .env import (
    HOOK_WAIT_MS as HOOK_WAIT_MS,
)
from .env import (
    MAX_CONTEXT_VALUE as MAX_CONTEXT_VALUE,
)
from .env import (
    MAX_HOOK_ENV as MAX_HOOK_ENV,
)
from .env import (
    MAX_HOOK_ENV_VALUE as MAX_HOOK_ENV_VALUE,
)
from .env import (
    MAX_HOOK_PATH as MAX_HOOK_PATH,
)
from .env import (
    MAX_HOOK_QUEUE_ENV as MAX_HOOK_QUEUE_ENV,
)
from .env import (
    MAX_HOOK_QUEUE_ITEM as MAX_HOOK_QUEUE_ITEM,
)
from .env import (
    MAX_HOOK_WAIT_MS as MAX_HOOK_WAIT_MS,
)
from .env import (
    MAX_TELEMETRY_ENTRIES as MAX_TELEMETRY_ENTRIES,
)
from .env import (
    REPORT_ENV as REPORT_ENV,
)
from .env import (
    RUNTIME_ENV_NAMES as RUNTIME_ENV_NAMES,
)
from .env import (
    RUNTIME_ENV_PREFIXES as RUNTIME_ENV_PREFIXES,
)
from .env import (
    SECRET_ENV_WORDS as SECRET_ENV_WORDS,
)
from .env import (
    hook_queue_env as hook_queue_env,
)
from .env import (
    proxy_userinfo as proxy_userinfo,
)
from .env import (
    report_consent as report_consent,
)
from .env import (
    runtime_env_name as runtime_env_name,
)
from .env import (
    secret_env as secret_env,
)
from .params import (
    _DELIVERER as _DELIVERER,
)
from .params import (
    AGENT_CHARS as AGENT_CHARS,
)
from .params import (
    MAILBOX_ID as MAILBOX_ID,
)
from .params import (
    MAILBOX_WAIT_S as MAILBOX_WAIT_S,
)
from .params import (
    MAX_MAILBOX_CLAIM as MAX_MAILBOX_CLAIM,
)
from .params import (
    MAX_MAILBOX_WAIT_S as MAX_MAILBOX_WAIT_S,
)
from .params import (
    MAX_STATE_DIR as MAX_STATE_DIR,
)
from .params import (
    MESSAGE_FILE as MESSAGE_FILE,
)
from .params import (
    SID_CHARS as SID_CHARS,
)
from .params import (
    _need_mailbox as _need_mailbox,
)
from .params import (
    _need_path as _need_path,
)
from .params import (
    is_agent_id as is_agent_id,
)
from .params import (
    need_background as need_background,
)
from .params import (
    need_bool as need_bool,
)
from .params import (
    need_choice as need_choice,
)
from .params import (
    need_context as need_context,
)
from .params import (
    need_env as need_env,
)
from .params import (
    need_hook_queue as need_hook_queue,
)
from .params import (
    need_hook_run as need_hook_run,
)
from .params import (
    need_int as need_int,
)
from .params import (
    need_mailbox_answer as need_mailbox_answer,
)
from .params import (
    need_mailbox_claim as need_mailbox_claim,
)
from .params import (
    need_mailbox_post as need_mailbox_post,
)
from .params import (
    need_owner as need_owner,
)
from .params import (
    need_path as need_path,
)
from .params import (
    need_sessions as need_sessions,
)
from .params import (
    need_sid as need_sid,
)
from .params import (
    need_state_dir as need_state_dir,
)
from .params import (
    need_str as need_str,
)
from .params import (
    need_telemetry as need_telemetry,
)
from .params import (
    need_wait as need_wait,
)
from .vocabularies import (
    ACTIONS as ACTIONS,
)
from .vocabularies import (
    APP_RESULT_TIMEOUT_S as APP_RESULT_TIMEOUT_S,
)
from .vocabularies import (
    APP_RESULTS as APP_RESULTS,
)
from .vocabularies import (
    BACKGROUND_REFUSALS as BACKGROUND_REFUSALS,
)
from .vocabularies import (
    CAPS as CAPS,
)
from .vocabularies import (
    CHECK_TRIGGERS as CHECK_TRIGGERS,
)
from .vocabularies import (
    CLIENTS as CLIENTS,
)
from .vocabularies import (
    DESKTOP_READS as DESKTOP_READS,
)
from .vocabularies import (
    HARNESS_RECEIPT_FIELDS as HARNESS_RECEIPT_FIELDS,
)
from .vocabularies import (
    MAILBOX_PARTS as MAILBOX_PARTS,
)
from .vocabularies import (
    MAX_APP_ERROR as MAX_APP_ERROR,
)
from .vocabularies import (
    MAX_SUBSCRIBE_SESSIONS as MAX_SUBSCRIBE_SESSIONS,
)
from .vocabularies import (
    MODES as MODES,
)
from .vocabularies import (
    NOTES_MODES as NOTES_MODES,
)
from .vocabularies import (
    ORIGIN_DESKTOP as ORIGIN_DESKTOP,
)
from .vocabularies import (
    RECEIPT_FIELDS as RECEIPT_FIELDS,
)
from .vocabularies import (
    RECEIPT_SCOPE_FIELDS as RECEIPT_SCOPE_FIELDS,
)
from .vocabularies import (
    REPORT_FOR_CHECK as REPORT_FOR_CHECK,
)
from .vocabularies import (
    REPORT_TRIGGERS as REPORT_TRIGGERS,
)
from .vocabularies import (
    SESSION_TOPICS as SESSION_TOPICS,
)
from .vocabularies import (
    TOPICS as TOPICS,
)
from .vocabularies import (
    Action as Action,
)
from .vocabularies import (
    ActionState as ActionState,
)
from .vocabularies import (
    AppCheckReason as AppCheckReason,
)
from .vocabularies import (
    AppReportIgnored as AppReportIgnored,
)
from .vocabularies import (
    AppResult as AppResult,
)
from .vocabularies import (
    AppStepOutcome as AppStepOutcome,
)
from .vocabularies import (
    BackgroundRefusal as BackgroundRefusal,
)
from .vocabularies import (
    BackgroundState as BackgroundState,
)
from .vocabularies import (
    BusyHolder as BusyHolder,
)
from .vocabularies import (
    Cap as Cap,
)
from .vocabularies import (
    CheckOutcome as CheckOutcome,
)
from .vocabularies import (
    CheckTrigger as CheckTrigger,
)
from .vocabularies import (
    Client as Client,
)
from .vocabularies import (
    DeferReason as DeferReason,
)
from .vocabularies import (
    DesktopOp as DesktopOp,
)
from .vocabularies import (
    EndReason as EndReason,
)
from .vocabularies import (
    JobOutcome as JobOutcome,
)
from .vocabularies import (
    MailboxPart as MailboxPart,
)
from .vocabularies import (
    Mode as Mode,
)
from .vocabularies import (
    NotesMode as NotesMode,
)
from .vocabularies import (
    RefusedReason as RefusedReason,
)
from .vocabularies import (
    ReportTrigger as ReportTrigger,
)
from .vocabularies import (
    State as State,
)
from .vocabularies import (
    StepState as StepState,
)
from .vocabularies import (
    StopReason as StopReason,
)
from .vocabularies import (
    Topic as Topic,
)
from .vocabularies import (
    UpdateStep as UpdateStep,
)
from .vocabularies import (
    WorkerState as WorkerState,
)
from .vocabularies import (
    _optional_version as _optional_version,
)
from .vocabularies import (
    need_app_check as need_app_check,
)
from .vocabularies import (
    need_app_result as need_app_result,
)
from .wire import (
    API_VERSION as API_VERSION,
)
from .wire import (
    ERROR_MESSAGES as ERROR_MESSAGES,
)
from .wire import (
    JSONRPC as JSONRPC,
)
from .wire import (
    MAX_LINE_BYTES as MAX_LINE_BYTES,
)
from .wire import (
    MAX_WAIT_S as MAX_WAIT_S,
)
from .wire import (
    METHODS as METHODS,
)
from .wire import (
    ErrorCode as ErrorCode,
)
from .wire import (
    Method as Method,
)
from .wire import (
    RpcError as RpcError,
)
from .wire import (
    encode as encode,
)
from .wire import (
    error as error,
)
from .wire import (
    event as event,
)
from .wire import (
    invalid_params as invalid_params,
)
from .wire import (
    parse_request as parse_request,
)
from .wire import (
    request as request,
)
from .wire import (
    result as result,
)
