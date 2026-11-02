"""Moved to `tap.transcripts.journal` (agent:18, tap 0.10.34);
kept: every Probe service, old or new, imports it from the installed tap."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module(".transcripts.journal", __package__)
