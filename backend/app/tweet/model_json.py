"""Pull one JSON object out of a CLI or Ollama reply.

Real replies often put prose, a thinking block, or a markdown fence around
the object. A later well-formed object is kept. Fields are not invented, and
a bare array is not wrapped into a batch.
"""

from __future__ import annotations

import json
import re

_THINKING = re.compile(
    r"<\s*(?:think|thinking)\s*>[\s\S]*?<\s*/\s*(?:think|thinking)\s*>",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n?([\s\S]*?)```", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_PREFERRED_KEYS = ("personas", "results", "rewrites")


def relax_json_strings(text: str) -> str:
    """Escape raw newlines inside JSON strings. Models wrap a 60-point persona."""

    if not isinstance(text, str) or not text:
        return text
    pieces: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                pieces.append(char)
                escape = False
                continue
            if char == "\\":
                pieces.append(char)
                escape = True
                continue
            if char == '"':
                pieces.append(char)
                in_string = False
                continue
            if char == "\n":
                pieces.append("\\n")
                continue
            if char == "\r":
                continue
            if char == "\t":
                pieces.append("\\t")
                continue
            pieces.append(char)
            continue
        if char == '"':
            in_string = True
        pieces.append(char)
    return "".join(pieces)


def parse_model_object(text: str | None) -> dict | None:
    """Return the model object, or None when the reply has no JSON object."""

    if not isinstance(text, str):
        return None
    cleaned = _THINKING.sub("", relax_json_strings(text).lstrip("\ufeff")).strip()
    if not cleaned:
        return None
    whole = _parse_whole(cleaned)
    if whole is not _FAILED:
        return whole
    found = _collect(cleaned)
    if not found:
        return None
    best = max(_rank(item) for item in found)
    if best <= 0:
        return found[-1]
    winners = [item for item in found if _rank(item) == best]
    return winners[-1]


def _parse_whole(text: str):
    loaded = _json_loads(text)
    if loaded is _FAILED:
        return _FAILED
    if isinstance(loaded, dict):
        return loaded
    if isinstance(loaded, str):
        return _dict_from_encoded_string(loaded)
    return None


def _collect(text: str) -> list[dict]:
    found: list[dict] = []
    for match in _FENCE.finditer(text):
        parsed = _parse_whole(match.group(1).strip())
        if isinstance(parsed, dict):
            found.append(parsed)
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        loaded, consumed = _raw_value(decoder, text[start:])
        if consumed is None:
            index = start + 1
            continue
        if isinstance(loaded, dict):
            found.append(loaded)
        elif isinstance(loaded, str):
            encoded = _dict_from_encoded_string(loaded)
            if isinstance(encoded, dict):
                found.append(encoded)
        index = start + consumed
    return found


def _raw_value(decoder: json.JSONDecoder, text: str):
    try:
        loaded, end = decoder.raw_decode(text)
        return loaded, end
    except json.JSONDecodeError:
        pass
    relaxed = _TRAILING_COMMA.sub(r"\1", text)
    if relaxed == text:
        return None, None
    try:
        loaded, end = decoder.raw_decode(relaxed)
    except json.JSONDecodeError:
        return None, None
    return loaded, end


def _json_loads(text: str):
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    if not stripped or stripped[0] not in '{["':
        return _FAILED
    relaxed = _TRAILING_COMMA.sub(r"\1", stripped)
    if relaxed == stripped:
        return _FAILED
    try:
        return json.loads(relaxed)
    except json.JSONDecodeError:
        return _FAILED


def _dict_from_encoded_string(value: str) -> dict | None:
    """Decode at most two layers when a model returns a JSON string."""

    current = value
    for _ in range(2):
        loaded = _json_loads(current.strip())
        if isinstance(loaded, dict):
            return loaded
        if not isinstance(loaded, str):
            return None
        current = loaded
    return None


def persona_items(text: str | None) -> list[dict]:
    """Persona objects a CLI actually prints: an array, a fence, or one object per line.

    A bare array is not a batch for other roles. Callers that want personas
    opt in here. Fields are only those the model wrote.
    """

    if not isinstance(text, str):
        return []
    cleaned = _THINKING.sub("", relax_json_strings(text).lstrip("\ufeff")).strip()
    if not cleaned:
        return []
    for value in _values(cleaned, "["):
        if not isinstance(value, list):
            continue
        items = [item for item in value if _persona_item(item)]
        if items:
            return items
    found: list[dict] = []
    seen: set[str] = set()
    for value in _values(cleaned, "{"):
        if isinstance(value, dict) and isinstance(value.get("personas"), list):
            items = [item for item in value["personas"] if _persona_item(item)]
            if items:
                return items
        if not _persona_item(value) or value["agent_id"] in seen:
            continue
        seen.add(value["agent_id"])
        found.append(value)
    return found


def _values(text: str, opener: str):
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        start = text.find(opener, index)
        if start < 0:
            return
        loaded, consumed = _raw_value(decoder, text[start:])
        if consumed is None:
            index = start + 1
            continue
        yield loaded
        index = start + consumed


def _persona_item(value) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
        return False
    return any(isinstance(value.get(key), str) for key in ("display_name", "bio", "persona", "avoid_speaking_when"))


def _rank(document: dict) -> int:
    if any(key in document for key in _PREFERRED_KEYS):
        return 2
    if document.get("schema_version") == "2.0":
        return 1
    return 0


class _Failed:
    pass


_FAILED = _Failed()
