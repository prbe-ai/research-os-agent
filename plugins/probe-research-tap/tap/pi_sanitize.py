"""Moved to `tap.transcripts.sanitizers.pi` (agent:18, tap 0.10.34);
kept: `tap.sources` loads a sanitizer as `tap.<registry key>` (harnesses.json `sanitizer`)."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module(".transcripts.sanitizers.pi", __package__)
