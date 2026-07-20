"""probe-research-tap tap daemon.

Tails the active Claude Code transcript, batches new lines, and ships them
to the Research OS backend's /ingest/v1/sessions/claude-code endpoint.
Credentials come from the probe CLI (`probe login`): the ingest token and
base URL are read from ~/.config/probe/config.json, with PROBE_INGEST_TOKEN
and PROBE_BASE_URL env overrides — there is no hardcoded host. State and
lifecycle are owned by Claude Code's session hooks — daemon dies when the
session ends.
"""

__version__ = "0.1.0"
