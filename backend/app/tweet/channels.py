"""Presence checks for the subscription CLI and Ollama.

These checks look at configured paths and files. They do not execute a CLI,
open a network connection, or ask for an API key.
"""

from __future__ import annotations

import os
from pathlib import Path

ALLOWED_CLIS = ("grok", "codex")
NO_PAID_KEY = "不需要提供 OpenAI 或 xAI 的 API key。"


def channel_config() -> dict:
    cli = os.environ.get("SUBSCRIPTION_CLI", "grok").strip().lower()
    if cli == "codex":
        cli_path = os.environ.get("CODEX_CLI_PATH", "").strip()
    else:
        cli_path = os.environ.get("GROK_CLI_PATH", "").strip()
    model = os.environ.get("OLLAMA_MODEL", "qwen3.8:27b-mxfp8").strip()
    return {
        "subscription_cli": cli,
        "cli_path": cli_path,
        "login_path": os.environ.get("SUBSCRIPTION_CLI_LOGIN_PATH", "").strip(),
        "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", "").strip(),
        "ollama_model": model,
    }


def _file_ready(path: str) -> bool:
    if not path:
        return False
    candidate = Path(path)
    return candidate.is_file() and candidate.stat().st_size > 0


def channel_problems(config: dict | None = None) -> list[str]:
    """Return Chinese problems. An empty list means a run may start."""

    current = config or channel_config()
    problems: list[str] = []
    cli = current["subscription_cli"]
    if cli not in ALLOWED_CLIS:
        problems.append("所选订阅 CLI 无效。只能选择已登录的 grok 或 codex。")
    elif not _file_ready(current["cli_path"]):
        problems.append("未找到所选订阅 CLI 的可执行文件。")
    if not _file_ready(current["login_path"]):
        problems.append("未检测到所选订阅 CLI 的登录状态。")
    if not current["ollama_base_url"]:
        problems.append("未配置 Ollama 地址（OLLAMA_BASE_URL）。")
    if not current["ollama_model"]:
        problems.append("未配置 Ollama 模型标识。")
    return problems


def channel_error_message(problems: list[str]) -> str:
    detail = " ".join(problems)
    return f"{detail} 预演没有开始。{NO_PAID_KEY}"
