"""Process and HTTP adapters.

Real Grok, Codex, and Ollama are not contacted by these tests. Headless
flags, login, model ids, and the Ollama address stay unverified.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .limits import MAX_OUTPUT_BYTES
from .registry import kill_process_group

_META_PREFIX = b"__MIROFISH_META__"
_BLOCKED_ENV_PARTS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "AUTHORIZATION",
    "EMAIL",
    "CREDENTIAL",
)


@dataclass
class AdapterOutcome:
    started: bool
    text: str = ""
    exit_code: int | None = None
    http_status: int | None = None
    error_code: str | None = None
    token_usage_status: str = "missing"
    queue_status: str = "none"
    truncated: bool = False
    internal_attempts: int | None = None
    model_id_reported: str | None = None


def sanitized_child_env() -> dict[str, str]:
    """Copy the parent environment without secrets or email-shaped values."""

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(part in upper for part in _BLOCKED_ENV_PARTS):
            continue
        if "@" in value:
            continue
        env[key] = value
    env["MIROFISH_TOOLS"] = "off"
    env["MIROFISH_CLI_CONTRACT"] = "v1-unverified"
    return env


def messages_to_prompt(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "")
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def chat_completions_url(base_url: str) -> str:
    trimmed = base_url.strip().rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed + "/chat/completions"
    return trimmed + "/v1/chat/completions"


def invoke_subscription_cli(
    *,
    executable: str,
    prompt: str,
    timeout: float,
    cancel_check,
    register_process,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    extra_args: list[str] | None = None,
) -> AdapterOutcome:
    """Start a fixed executable with argv data. Do not use a shell.

    Extra flags go after the prompt so argv[2] stays the prompt text.
    """

    try:
        process = subprocess.Popen(
            [executable, "-p", prompt, *(extra_args or [])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sanitized_child_env(),
            start_new_session=True,
        )
    except FileNotFoundError:
        return AdapterOutcome(started=False, error_code="cli_missing")
    except OSError:
        return AdapterOutcome(started=False, error_code="cli_not_started")

    register_process(process)
    meta: dict = {}
    body = bytearray()
    truncated = False
    read_error: list[str] = []

    def _read() -> None:
        nonlocal truncated
        stream = process.stdout
        if stream is None:
            return
        try:
            first = stream.readline()
        except Exception:
            read_error.append("read")
            return
        if first.startswith(_META_PREFIX):
            raw = first[len(_META_PREFIX):].strip()
            try:
                loaded = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                if isinstance(loaded, dict):
                    meta.update(loaded)
            except json.JSONDecodeError:
                body.extend(first)
        elif first:
            body.extend(first)
        while len(body) <= max_output_bytes:
            chunk = stream.read(4096)
            if not chunk:
                return
            room = max_output_bytes - len(body)
            if len(chunk) > room:
                body.extend(chunk[:room])
                truncated = True
                kill_process_group(process)
                return
            body.extend(chunk)

    reader = threading.Thread(target=_read, name="cli-stdout", daemon=True)
    reader.start()
    deadline = time.monotonic() + max(0.05, timeout)
    timed_out = False
    cancelled = False
    while process.poll() is None:
        if cancel_check():
            cancelled = True
            kill_process_group(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            kill_process_group(process)
            break
        time.sleep(0.02)
    reader.join(timeout=1)
    stderr = b""
    if process.stderr is not None:
        try:
            stderr = process.stderr.read() or b""
        except Exception:
            stderr = b""
    exit_code = process.poll()
    if not cancelled:
        try:
            cancelled = bool(cancel_check())
        except Exception:
            cancelled = cancelled
    text = body.decode("utf-8", errors="replace")
    token_status = "missing"
    if "tokens" in meta and meta.get("tokens") not in (None, ""):
        token_status = "present"
    internal = meta.get("internal_attempts")
    internal_attempts = int(internal) if isinstance(internal, int) and internal >= 1 else None
    queue_status = "none"
    if meta.get("rate_limited") or meta.get("http_status") == 429 or b"429" in stderr:
        queue_status = "rate_limited"
    elif meta.get("queued"):
        queue_status = "queued"
    error_code = None
    if cancelled:
        error_code = "cancelled"
    elif timed_out:
        error_code = "timeout"
    elif truncated:
        error_code = "truncated"
    elif queue_status == "rate_limited":
        error_code = "rate_limited"
    elif exit_code not in (0, None):
        error_code = "cli_exit"
    return AdapterOutcome(
        started=True,
        text=text,
        exit_code=exit_code,
        http_status=429 if queue_status == "rate_limited" else None,
        error_code=error_code,
        token_usage_status=token_status,
        queue_status=queue_status,
        truncated=truncated,
        internal_attempts=internal_attempts,
        model_id_reported=meta.get("model_id") if isinstance(meta.get("model_id"), str) else None,
    )


def invoke_ollama(
    *,
    base_url: str,
    model: str,
    messages: list[dict],
    timeout: float,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> AdapterOutcome:
    """Call an OpenAI-compatible local endpoint without an API key."""

    url = chat_completions_url(base_url)
    payload = {
        "model": model,
        "messages": [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in messages
        ],
        "temperature": 0,
        "max_tokens": 4096,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_output_bytes + 1)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        raw = error.read(max_output_bytes + 1)
        return _from_http(raw, error.code, max_output_bytes, started=True)
    except (TimeoutError, urllib.error.URLError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return AdapterOutcome(started=True, error_code="timeout", token_usage_status="missing")
        return AdapterOutcome(started=False, error_code="ollama_unreachable")
    return _from_http(raw, status, max_output_bytes, started=True)


def _part_text(part, *, include_thinking: bool) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    kind = str(part.get("type") or "").lower()
    if kind in {"thinking", "reasoning"} and not include_thinking:
        return ""
    text = part.get("text")
    if isinstance(text, str):
        return text
    content = part.get("content")
    return content if isinstance(content, str) else ""


def _content_text(content, *, include_thinking: bool) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_part_text(part, include_thinking=include_thinking) for part in content)
    return ""


def _message_text(message: dict) -> str:
    """Read assistant text. Use reasoning only when content itself is empty."""

    raw = _content_text(message.get("content"), include_thinking=False)
    if raw.strip():
        return raw
    for key in ("reasoning", "reasoning_content", "thinking"):
        fallback = message.get(key)
        if isinstance(fallback, str) and fallback.strip():
            return fallback
        joined = _content_text(fallback, include_thinking=True)
        if joined.strip():
            return joined
    return raw


def _from_http(raw: bytes, status: int, limit: int, *, started: bool) -> AdapterOutcome:
    truncated = len(raw) > limit
    payload = raw[:limit]
    text = ""
    token_status = "missing"
    try:
        loaded = json.loads(payload.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        usage = loaded.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") not in (None, ""):
            token_status = "present"
        choices = loaded.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                text = _message_text(message)
        if not text and isinstance(loaded.get("error"), dict):
            text = ""
    error_code = None
    queue_status = "none"
    if status == 429:
        error_code = "rate_limited"
        queue_status = "rate_limited"
    elif status >= 400:
        error_code = "http_error"
    elif truncated:
        error_code = "truncated"
    return AdapterOutcome(
        started=started,
        text=text,
        http_status=status,
        error_code=error_code,
        token_usage_status=token_status,
        queue_status=queue_status,
        truncated=truncated,
        internal_attempts=1,
    )
