"""Narrative model routing.

HDSI does not store API keys; every model call goes through AstrBot Provider.
A *slot* binds one duty (main narrative / compaction / alter / embedding) to
one or more AstrBot providers:

    inherit              -> current session provider at call time
    provider_id          -> that provider's currently selected model
    provider_id:model    -> explicit model override
    a,b,c                -> comma-separated failover chain

Provider selection keeps the original failover semantics: priority order,
per-provider attempt counts, failure cooldowns, round-robin option.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol


@dataclass(frozen=True)
class SlotBinding:
    kind: str  # inherit | provider | model
    provider_id: str = ""
    model: str = ""

    @property
    def key(self) -> str:
        if self.kind == "inherit":
            return "inherit"
        return f"{self.provider_id}:{self.model or '*'}"


def parse_slot(value: str | None) -> list[SlotBinding]:
    """Parse a comma-separated slot expression into ordered bindings."""
    text = (value or "").strip()
    if not text or text.lower() == "inherit":
        return [SlotBinding(kind="inherit")]
    bindings: list[SlotBinding] = []
    for chunk in text.split(","):
        part = chunk.strip()
        if not part:
            continue
        if part.lower() == "inherit":
            bindings.append(SlotBinding(kind="inherit"))
            continue
        if ":" in part:
            provider_id, _, model = part.partition(":")
            provider_id = provider_id.strip()
            model = model.strip()
            if not provider_id:
                continue
            bindings.append(SlotBinding(kind="model", provider_id=provider_id, model=model))
        else:
            bindings.append(SlotBinding(kind="provider", provider_id=part))
    return bindings or [SlotBinding(kind="inherit")]


class LlmCaller(Protocol):
    """Injected by the AstrBot adapter.

    Returns the assistant text for one chat completion, raising on transport
    errors so the router can move to the next candidate.
    """

    async def __call__(
        self,
        binding: SlotBinding,
        *,
        system_prompt: str,
        user_content: Any,
        image_urls: Optional[list[str]] = None,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float,
        response_json: bool = False,
    ) -> str: ...


@dataclass
class RouterOptions:
    failover_enabled: bool = True
    max_attempts_per_provider: int = field(default=1)
    cooldown_minutes: int = field(default=5)
    round_robin: bool = False


class ProviderRouter:
    """Tracks cooldowns and candidate order for one logical slot."""

    def __init__(self, options: RouterOptions | None = None) -> None:
        self.options = options or RouterOptions()
        self._cooldown_until: dict[str, float] = {}
        self._round_robin_offset = 0

    def select(self, bindings: list[SlotBinding]) -> list[SlotBinding]:
        now = time.monotonic()
        ready = [b for b in bindings if self._cooldown_until.get(b.key, 0) <= now]
        candidates = ready or bindings
        if self.options.round_robin and len(candidates) > 1:
            offset = self._round_robin_offset % len(candidates)
            self._round_robin_offset += 1
            candidates = candidates[offset:] + candidates[:offset]
        if not self.options.failover_enabled:
            candidates = candidates[:1]
        return candidates

    def mark_failure(self, binding: SlotBinding) -> None:
        self._cooldown_until[binding.key] = time.monotonic() + self.options.cooldown_minutes * 60

    def mark_success(self, binding: SlotBinding) -> None:
        self._cooldown_until.pop(binding.key, None)


class NarrativeProvider(Protocol):
    """Core-side view of the narrative model stack."""

    async def decide_raw(
        self,
        request,  # NarrativeRequest
        system_prompt: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float,
        response_json: bool,
        max_repairs: int = 1,
    ) -> tuple[dict[str, Any], list[Any]]:
        """Run one main-narrative completion; returns (raw decision dict, images echo)."""
        ...

    async def compact_raw(
        self,
        payload: dict[str, Any],
        system_prompt: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float,
        response_json: bool,
    ) -> dict[str, Any]: ...

    async def analyze_alter(
        self,
        request_payload: dict[str, Any],
        system_prompt: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float,
        response_json: bool,
    ) -> dict[str, Any]: ...


class Embedder(Protocol):
    async def embed(self, input_text: str) -> list[float]: ...
