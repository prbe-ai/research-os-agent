"""Moved to `tap.transcripts.transcripts` (agent:18, tap 0.10.34);
this path stays importable for one release."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module(".transcripts.transcripts", __package__)
