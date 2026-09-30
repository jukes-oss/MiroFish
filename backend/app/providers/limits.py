"""Product defaults for call counts and wall-clock time.

Both mixed and subscription-only use these same caps.
"""

from __future__ import annotations

RUN_MAX_REQUESTS = 10
RUN_MAX_WALL_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 120
LLM_CONCURRENCY = 1
DAY_MAX_REQUESTS = 50
MAX_ATTEMPTS_PER_BATCH = 2
REPORT_RESERVE_CALLS = 2
REPORT_RESERVE_WALL_SECONDS = 240
CLEANUP_RESERVE_SECONDS = 5
MAX_OUTPUT_BYTES = 1_048_576

GROK_MODEL_ID = "grok-4.7"
OLLAMA_MODEL_ID = "qwen3.8:27b-mxfp8"
REPORT_RESERVE_BATCH = "__report_reserve__"

ROLES = (
    "persona",
    "agent",
    "report",
    "ontology",
    "extractor",
    "config",
    "interview",
)
RULE_ROLES = ("moderator",)

COUNTED_STATUSES = ("reserved", "in_flight", "completed", "failed", "uncertain")
PHYSICAL_STATUSES = ("in_flight", "completed", "failed", "uncertain")
