"""CTS - Comic To Story."""

import os as _os

__version__ = "0.1.0"

# CTS is a local, private tool: keep CrewAI from phoning home or opening trace prompts.
for _key, _value in {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "CREWAI_DISABLE_TRACKING": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "OTEL_SDK_DISABLED": "true",
}.items():
    _os.environ.setdefault(_key, _value)
