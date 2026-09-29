"""Keep secrets, login emails, and draft text out of logs."""

from __future__ import annotations

import contextvars
import logging
import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(r"(?:sk-|xai-|Bearer\s+)[A-Za-z0-9_\-]{6,}", re.IGNORECASE)
_ASSIGNED = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*\S+"
)

_sensitive: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "mirofish_sensitive_text",
    default=(),
)


def push_sensitive(*texts: str) -> contextvars.Token:
    current = _sensitive.get()
    extra = tuple(text for text in texts if text)
    return _sensitive.set(current + extra)


def reset_sensitive(token: contextvars.Token) -> None:
    _sensitive.reset(token)


def redact(text: str, extras: tuple[str, ...] | None = None) -> str:
    cleaned = _EMAIL.sub("[redacted-email]", text)
    cleaned = _SECRET.sub("[redacted-secret]", cleaned)
    cleaned = _ASSIGNED.sub(lambda match: f"{match.group(1)}=[redacted-secret]", cleaned)
    for item in extras if extras is not None else _sensitive.get():
        if item and len(item) >= 4 and item in cleaned:
            cleaned = cleaned.replace(item, "[redacted-draft]")
    return cleaned


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        extras = _sensitive.get()
        record.msg = redact(str(record.msg), extras)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(item, extras) if isinstance(item, str) else item
                for item in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact(value, extras) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def install_redaction(logger: logging.Logger) -> None:
    if any(isinstance(item, SecretRedactionFilter) for item in logger.filters):
        return
    logger.addFilter(SecretRedactionFilter())
