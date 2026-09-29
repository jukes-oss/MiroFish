"""Freeze role routing before a run starts.

Later timeouts, 429s, or quality failures must not change this profile.
"""

from __future__ import annotations

import json
from typing import Any

from .limits import (
    CLEANUP_RESERVE_SECONDS,
    DAY_MAX_REQUESTS,
    GROK_MODEL_ID,
    LLM_CONCURRENCY,
    MAX_ATTEMPTS_PER_BATCH,
    MAX_OUTPUT_BYTES,
    OLLAMA_MODEL_ID,
    REPORT_RESERVE_CALLS,
    REPORT_RESERVE_WALL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    ROLES,
    RUN_MAX_REQUESTS,
    RUN_MAX_WALL_SECONDS,
)

_ROUTES = {
    "mixed": {
        "persona": "subscription_cli",
        "agent": "ollama",
        "report": "subscription_cli",
        "ontology": "ollama",
        "extractor": "ollama",
        "config": "ollama",
        "interview": "ollama",
    },
    "subscription-only": {
        "persona": "subscription_cli",
        "agent": "subscription_cli",
        "report": "subscription_cli",
        "ontology": "ollama",
        "extractor": "ollama",
        "config": "ollama",
        "interview": "ollama",
    },
}


def build_capability_profile(
    *,
    execution_profile: str,
    subscription_cli: str,
    cli_path: str,
    ollama_base_url: str,
    ollama_model: str | None,
) -> dict[str, Any]:
    if execution_profile not in _ROUTES:
        raise ValueError("execution_profile 只能是 mixed 或 subscription-only。")
    if subscription_cli not in ("grok", "codex"):
        raise ValueError("订阅 CLI 只能是 grok 或 codex。")
    model = (ollama_model or OLLAMA_MODEL_ID).strip() or OLLAMA_MODEL_ID
    if subscription_cli == "grok":
        sub_model: str | None = GROK_MODEL_ID
        sub_model_status = "configured_only"
        channel = "grok_cli"
    else:
        sub_model = None
        sub_model_status = "unknown"
        channel = "codex_cli"
    routes: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        adapter = _ROUTES[execution_profile][role]
        if adapter == "subscription_cli":
            routes[role] = {
                "adapter": adapter,
                "channel": channel,
                "model_id": sub_model,
                "model_id_status": sub_model_status,
                "executable": cli_path,
            }
        else:
            routes[role] = {
                "adapter": adapter,
                "channel": "ollama",
                "model_id": model,
                "model_id_status": "configured_only",
                "base_url": ollama_base_url,
            }
    return {
        "schema_version": "2.0",
        "execution_profile": execution_profile,
        "subscription_cli": subscription_cli,
        "routes": routes,
        "moderator": "rules",
        "silent_fallback": False,
        "limits": {
            "run_max_requests": RUN_MAX_REQUESTS,
            "run_max_wall_seconds": RUN_MAX_WALL_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "llm_concurrency": LLM_CONCURRENCY,
            "day_max_requests": DAY_MAX_REQUESTS,
            "max_attempts_per_batch": MAX_ATTEMPTS_PER_BATCH,
            "report_reserve_calls": REPORT_RESERVE_CALLS,
            "report_reserve_wall_seconds": REPORT_RESERVE_WALL_SECONDS,
            "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
        "tools": {
            "requested": "off",
            "verified": "待验",
        },
        "unverified": {
            "login": "待验",
            "model_id": "待验",
            "headless_invocation": "待验",
            "ollama_address": "待验",
        },
    }


def dump_profile(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_profile(payload: str | None) -> dict[str, Any] | None:
    if not payload:
        return None
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        return None
    return loaded
