"""Shared test fakes: scripted narrator, sender recorder, virtual clock."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional


class VirtualClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@dataclass
class RecordedSend:
    participant_id: str
    content: str
    at_index: int


class SenderRecorder:
    """Records outgoing deliveries; optionally fails selected targets."""

    def __init__(self) -> None:
        self.sent: list[RecordedSend] = []
        self.fail_participants: set[str] = set()
        self.hook: Optional[Callable[[RecordedSend], None]] = None
        self._index = 0

    async def __call__(self, story, participant, content: str) -> bool:
        if participant.id in self.fail_participants:
            return False
        self._index += 1
        record = RecordedSend(participant.id, content, self._index)
        self.sent.append(record)
        if self.hook is not None:
            self.hook(record)
        return True

    def texts(self) -> list[str]:
        return [s.content for s in self.sent]


@dataclass
class ScriptedResponse:
    """One queued raw model decision (dict or callable)."""

    value: Any
    delay_seconds: float = 0.0


class ScriptedNarrator:
    """Deterministic narrator returning pre-queued JSON payloads."""

    def __init__(self) -> None:
        self.queue: list[ScriptedResponse] = []
        self.calls: list[dict[str, Any]] = []
        self.default: dict[str, Any] = {"script": "The afternoon passes quietly."}
        self.fail_next = 0

    def enqueue(self, payload: dict[str, Any], delay_seconds: float = 0.0) -> None:
        self.queue.append(ScriptedResponse(payload, delay_seconds))

    def _next(self) -> Any:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("scripted provider failure")
        if self.queue:
            item = self.queue.pop(0)
            if item.delay_seconds:
                raise TimeoutError("simulated timeout")
            return item.value
        return dict(self.default)

    async def decide_raw(self, request, *, system_prompt: str, temperature: float,
                         top_p: float, max_tokens: int, timeout_seconds: float,
                         response_json: bool, max_repairs: int = 1):
        self.calls.append({
            "phase": request.phase.value if hasattr(request.phase, "value") else str(request.phase),
            "participant": request.participant.id if request.participant else None,
            "user_message": request.user_message,
            "due_intents": [i.type for i in request.due_intents],
            "system_prompt_len": len(system_prompt),
        })
        return self._next(), []

    async def compact_raw(self, *, payload: dict[str, Any], system_prompt: str,
                          temperature: float, top_p: float, max_tokens: int,
                          timeout_seconds: float, response_json: bool):
        self.calls.append({"kind": "compact"})
        if self.queue:
            return self.queue.pop(0).value
        return {"scene": {"hook": "quiet", "summary": "quiet day"}}

    async def analyze_alter(self, request_payload: dict[str, Any], *,
                            system_prompt: str, temperature: float, top_p: float,
                            max_tokens: int, timeout_seconds: float,
                            response_json: bool):
        self.calls.append({"kind": "alter"})
        return {"description": "氛围转向轻松。"}


class SilentEmbedder:
    async def embed(self, input_text: str) -> list[float]:
        return []


def user_decision(script: str, reply_mode: str = "immediate",
                  content: str | None = None, **extra: Any) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "script": script,
        "interaction": {"seen": True, "reply": {"mode": reply_mode}},
    }
    if reply_mode == "delayed":
        decision["interaction"]["reply"]["sendAt"] = extra.pop(
            "sendAt", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        )
    if content is not None:
        decision["interaction"]["reply"]["content"] = content
    decision.update(extra)
    return decision


async def wait_for(condition: Callable[[], bool], timeout: float = 3.0,
                   interval: float = 0.02) -> bool:
    import asyncio

    waited = 0.0
    while waited < timeout:
        if condition():
            return True
        await asyncio.sleep(interval)
        waited += interval
    return condition()


async def wait_for_async(condition: Callable[[], Any], timeout: float = 3.0,
                         interval: float = 0.02) -> bool:
    """Poll an async condition until truthy or timeout."""
    import asyncio

    waited = 0.0
    while waited < timeout:
        if await condition():
            return True
        await asyncio.sleep(interval)
        waited += interval
    return bool(await condition())
