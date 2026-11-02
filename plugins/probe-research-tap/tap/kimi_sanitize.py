"""Moved to `tap.transcripts.sanitizers.kimi_code` (agent:18, tap 0.10.34);
kept: `tap.sources` loads a sanitizer as `tap.<registry key>` (harnesses.json `sanitizer`)."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module(".transcripts.sanitizers.kimi_code", __package__)
