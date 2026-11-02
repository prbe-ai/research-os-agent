"""Moved to `tap.transcripts.credential_scan` (agent:18, tap 0.10.34);
this path stays importable for one release."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module(".transcripts.credential_scan", __package__)
