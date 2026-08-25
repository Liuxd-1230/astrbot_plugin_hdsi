"""LLM JSON extraction with bounded repair.

Port of narrator.ts parseJsonResponse / jsonCandidates / balancedJsonValues:
raw text → code fences → balanced JSON values, respecting quoted braces.
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str, source: str = "provider") -> dict[str, Any]:
    normalized = re.sub(r"[\u200b-\u200d\u2060]", "", (text or "").lstrip("\ufeff")).strip()
    last_error: Exception = ValueError("No JSON object found.")
    for candidate in _json_candidates(normalized):
        try:
            value = json.loads(candidate)
        except ValueError as error:
            last_error = error
            continue
        if isinstance(value, dict):
            return value
        last_error = ValueError("JSON root is not an object.")
    raise ValueError(f"{source} returned invalid JSON ({last_error}).")


def _json_candidates(text: str) -> list[str]:
    if not text:
        return []
    candidates: dict[str, None] = {}

    def add(value: str) -> None:
        trimmed = value.lstrip("\ufeff").strip()
        if trimmed:
            candidates.setdefault(trimmed, None)

    add(text)
    for match in re.finditer(r"```(?:json|javascript|js|jsonc)?\s*", text, re.I):
        body_start = match.end()
        closing = text.find("```", body_start)
        add(text[body_start:] if closing < 0 else text[body_start:closing])
    for candidate in list(candidates):
        for value in _balanced_json_values(candidate):
            candidates.setdefault(value, None)
    return list(candidates)


def _balanced_json_values(text: str) -> list[str]:
    values: list[str] = []
    for start, opening in enumerate(text):
        if opening not in "{[":
            continue
        stack = ["}" if opening == "{" else "]"]
        in_string = False
        escaped = False
        for index in range(start + 1, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in ("}", "]"):
                if not stack or stack[-1] != char:
                    break
                stack.pop()
                if not stack:
                    values.append(text[start:index + 1])
                    break
    return values


def extract_chat_text(response: Any) -> str:
    """Normalize OpenAI-compatible response shapes into one plain string."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    choice = None
    choices = response.get("choices") if isinstance(response, dict) else None
    if isinstance(choices, list) and choices:
        choice = choices[0]
    values: list[Any] = []
    if isinstance(choice, dict):
        message = choice.get("message")
        if isinstance(message, dict):
            values += [message.get("content"), message.get("reasoning_content"), message.get("refusal")]
        values.append(choice.get("text"))
    if isinstance(response, dict):
        values.append(response.get("output_text"))
        content = response.get("content")
        values.append(content)
    for value in values:
        text = flatten_chat_text(value)
        if text.strip():
            return text.strip()
    return ""


def flatten_chat_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(flatten_chat_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        content = value.get("content")
        if isinstance(content, (str, list)):
            return flatten_chat_text(content)
        output_text = value.get("output_text")
        if isinstance(output_text, (str, list)):
            return flatten_chat_text(output_text)
    return ""


REPAIR_INSTRUCTION = (
    "\n\nYour previous reply was not valid JSON. Reply again with ONLY one JSON "
    "object matching the original contract. No Markdown fences, no commentary."
)


async def request_json_with_repair(
    call: Any,
    payload_builder,
    max_repairs: int = 1,
) -> tuple[dict[str, Any], int]:
    """Run ``call`` and retry once with an appended repair instruction.

    Returns (payload, attempts_used). Raises on final failure so callers can
    fall back to the persisted narrative-retry path without corrupting state.
    """

    attempts = 0
    last_error: Exception | None = None
    for attempt in range(1 + max(0, max_repairs)):
        attempts += 1
        text = await call(repair=attempt > 0)
        try:
            return extract_json_object(text, "Narrative provider"), attempts
        except ValueError as error:
            last_error = error
    raise last_error if last_error else ValueError("empty provider response")
