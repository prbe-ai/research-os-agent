"""`python -m tap hook-env --source ID [--payload [--transcript]]`: a hook's
per-harness values, read from the harness registry.

The tap's shell hooks (`hooks/*.sh`) spell Claude Code's and Codex's values in
branches of their own (`if [ "$SOURCE" = codex ]`). Those branches stay exactly
as they are. Every other hook-plugin harness (Kimi Code) asks this command
instead, so its values come from its registry row and a third harness needs no
third branch. Prints shell assignments, quoted for `eval`:

  TAP_PLUGIN_DIR      the capture state folder (`capture.plugin_dir_env`, else
                      `capture.plugin_dir` under $HOME)
  TAP_WATCHER_PREFIX  the /tmp file prefix (`capture.watcher_prefix`)
  TAP_MANIFEST_DIR    the plugin manifest folder (`plugin.manifest_dir`)
  TAP_HAS_TOKEN       "1" when this harness's capture token is configured, by
                      tap/config.py's own load_token(): the paired .token, the
                      row's token variable, and the probe CLI's token ONLY where
                      the row allows it (never for Kimi Code)

With `--payload`, the hook's JSON payload on stdin is put in the Claude Code
shape the rest of each script already reads, and printed as:

  TAP_HOOK_INPUT      the payload, `session_id` canonical (the harness's own
                      prefix stripped: Kimi's `session_<uuid>` -> `<uuid>`) and,
                      with `--transcript`, `transcript_path` filled in when the
                      harness sends none (Kimi: from its session index)
  TAP_SESSION_ID      the canonical session id ("" when the payload has none)

A source that is not a captured registry harness exits 2 and prints nothing:
the script then keeps its default (Claude Code) values, as it always did for
an unrecognised PROBE_TAP_SOURCE.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

from tap import config as cfg
from tap import kimi_discovery, sources

EXIT_UNKNOWN_SOURCE = 2

#: How a harness whose payload names no transcript finds it, by
#: `transcripts.format`.
_TRANSCRIPT_FINDERS = {"kimi_wire": kimi_discovery.find_transcript}


def normalize_payload(harness, raw: str, *, find_transcript: bool) -> tuple[str, str]:
    """(payload JSON in the Claude Code shape, canonical session id)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, ""
    if not isinstance(data, dict):
        return raw, ""
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return raw, ""
    session_id = harness.canonical_session_id(session_id)
    data["session_id"] = session_id
    path = data.get("transcript_path")
    if find_transcript and not (isinstance(path, str) and path):
        finder = _TRANSCRIPT_FINDERS.get((harness.transcripts or {}).get("format"))
        found = finder(harness, session_id) if finder else None
        if found is not None:
            data["transcript_path"] = str(found)
    return json.dumps(data), session_id


def main(argv: list[str] | None = None, *, stdin=None, out=None) -> int:
    parser = argparse.ArgumentParser(prog="tap hook-env")
    parser.add_argument("--source", required=True)
    parser.add_argument("--payload", action="store_true")
    parser.add_argument("--transcript", action="store_true")
    args = parser.parse_args(argv)
    out = sys.stdout if out is None else out

    source = args.source.strip().lower()
    if source not in sources.SOURCES:
        return EXIT_UNKNOWN_SOURCE
    harness = sources.harness(sources.get(source))
    if not harness.plugin:
        return EXIT_UNKNOWN_SOURCE  # not a hook-plugin harness: nothing here runs its hooks
    os.environ[cfg.SOURCE_ENV] = source  # tap/config.py reads the source from here

    values = {
        "TAP_PLUGIN_DIR": str(cfg.plugin_dir()),
        "TAP_WATCHER_PREFIX": cfg.watcher_prefix(),
        "TAP_MANIFEST_DIR": harness.plugin["manifest_dir"],
        "TAP_HAS_TOKEN": "1" if cfg.load_token() else "",
    }
    if args.payload:
        raw = (sys.stdin if stdin is None else stdin).read()
        payload, session_id = normalize_payload(harness, raw, find_transcript=args.transcript)
        values["TAP_HOOK_INPUT"] = payload
        values["TAP_SESSION_ID"] = session_id
    for name, value in values.items():
        out.write(f"{name}={shlex.quote(value)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
