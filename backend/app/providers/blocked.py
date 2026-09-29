"""Refuse direct paid-API clients.

Old LLM, persona, config, OASIS, and in-tool generation paths must not open
their own network clients. They fail closed instead of falling back to a
paid API.
"""

from __future__ import annotations


class DirectModelCallBlocked(RuntimeError):
    """Raised when code tries to call a model outside the gateway."""


class BlockedPaidClient:
    """Stand-in that raises on every attribute access."""

    def __getattr__(self, name: str):
        refuse_direct_model_call("直接模型客户端已关闭")


def refuse_direct_model_call(reason: str) -> None:
    raise DirectModelCallBlocked(
        f"{reason}。生成、修复和能力探测必须经过调用次数与墙钟网关，"
        "不会使用 OpenAI 或 xAI 的 API key。"
    )
