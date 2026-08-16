"""Probe Research coding-agent transcript tap daemon.

Tails an active Claude Code transcript or Codex rollout, batches new lines,
and ships them to the matching Research OS ingestion endpoint.
Auth is device pairing (`python -m tap pair <token>`): a dashboard-minted
pairing token is exchanged for a device token, and the backend host is read
from the token's `iss` claim — no hardcoded host. The manual/self-host
alternative is the probe CLI (`probe login`), with PROBE_INGEST_TOKEN and
PROBE_BASE_URL env overrides. State and lifecycle are owned by Claude Code's
session hooks — daemon dies when the session ends.
"""

__version__ = "0.4.1"
