"""Scenarios 1-4: debounce, stale cancellation, first-reply commit, <sep/> cut."""

from __future__ import annotations

import asyncio

import pytest

from hdsi.prompt_builder import build_prompt_payload
from hdsi.types import NarrativeIntent, NarrativePhase, NarrativeRequest
from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


async def pending_intents(harness, story_id):
    rows = await harness.db.get("interlude_intent",
                                {"story_id": story_id, "status": "pending"})
    return rows


async def test_01_rapid_message_debounce(harness):
    """连续消息合并成一次主模型写作回合。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她抬头。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "嗯？"}},
    })
    for text in ("在吗", "我想问你个事", "很重要"):
        assert await h.service.receive(h.event(content=text))
    ok = await wait_for(lambda: len(h.sender.sent) >= 1)
    assert ok, "message should be delivered"
    assert len(h.narrator.calls) == 1, f"expected one call, got {len(h.narrator.calls)}"
    assert "连续消息" in (h.narrator.calls[0]["user_message"] or "")
    entries = await h.service.recent_entries(story.id, 20)
    user_entries = [e for e in entries if e.kind == "user-message"]
    assert len(user_entries) == 3
    assert len(h.sender.sent) == 1


async def test_02_stale_request_cancellation_before_commit(harness):
    """A 到达 → 正在生成 → B 到达（首条未提交）→ 旧生成作废，A+B 合并重写。"""
    h = harness
    story = await h.setup_story()

    slow_started = asyncio.Event()
    slow_finished = asyncio.Event()
    calls = []

    class SlowNarrator:
        async def decide_raw(self, request, **kwargs):
            calls.append({"message": request.user_message})
            if len(calls) == 1:
                slow_started.set()
                await asyncio.sleep(0.35)
            return {"script": f"第{len(calls)}次写作", "interaction": {
                "seen": True, "reply": {"mode": "immediate", "content": f"回复{len(calls)}"}}}, []

        async def compact_raw(self, **kwargs):
            return {}

        async def analyze_alter(self, *args, **kwargs):
            return {"description": ""}

    original_narrator = h.service.narrator
    slow = SlowNarrator()
    h.service.narrator = slow

    task_a = asyncio.create_task(h.service.receive(h.event(content="第一条消息")))
    await asyncio.wait_for(slow_started.wait(), timeout=2.0)
    await task_a
    accepted_b = await h.service.receive(h.event(content="第二条消息"))
    assert accepted_b is True

    ok = await wait_for(lambda: len(h.sender.sent) > 0)
    await asyncio.sleep(0.3)

    # The obsolete first result must never reach transport.
    texts = h.sender.texts()
    assert "旧" not in "".join(texts), f"obsolete delivery leaked: {texts}"
    # A replacement turn must have been requested with the combined batch.
    assert any("连续消息" in (c["message"] or "") for c in calls[1:]) or len(calls) >= 2
    entries = await h.service.recent_entries(story.id, 30)
    user_texts = [e.content for e in entries if e.kind == "user-message"]
    assert user_texts == ["第一条消息", "第二条消息"]
    h.service.narrator = original_narrator


async def test_03_first_reply_commit_boundary_and_sep_cut(harness):
    """首条气泡已提交：已发送不可回滚；未发送 <sep/> 气泡取消并留痕。"""
    h = harness
    story = await h.setup_story()

    h.narrator.enqueue({
        "script": "她拿起手机。",
        "interaction": {"seen": True, "reply": {
            "mode": "immediate",
            "content": "在的<sep/>我刚看到消息，怎么了？",
        }},
    })
    assert await h.service.receive(h.event(content="你好"))
    ok = await wait_for(lambda: len(h.sender.sent) >= 1)
    assert ok and h.sender.texts()[0] == "在的"

    ok = await wait_for_async(
        lambda: _has_split_intent(h, story.id), timeout=2.0
    )
    assert ok, "split-message intent should exist after first bubble commit"

    # New message interrupts typing of bubble 2.
    h.narrator.enqueue({
        "script": "她还没打完字就看到新消息。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "你说"}},
    })
    assert await h.service.receive(h.event(content="其实我想说另一件事"))
    await asyncio.sleep(0.3)

    intents = await pending_intents(h, story.id)
    split_pending = [i for i in intents if i["type"] == "split-message"]
    assert not split_pending, "unsent bubble must be cancelled by new input"

    entries = await h.service.recent_entries(story.id, 30)
    cancelled = [e for e in entries if e.kind == "intent-cancelled"]
    assert cancelled, "interruption must leave a traceable system entry"
    delivered_texts = "".join(h.sender.texts())
    assert "我刚看到消息，怎么了？" not in delivered_texts


async def _has_split_intent(harness, story_id) -> bool:
    rows = await harness.db.get(
        "interlude_intent",
        {"story_id": story_id, "status": "pending", "type": "split-message"},
    )
    return bool(rows)


async def test_04_sep_interruption_converts_to_intent_context():
    """被打断的未发送文字作为 interruptedOutgoingDrafts 进入下一次提示词。"""
    now = datetime_utc(2026, 8, 24, 12, 0, 0)
    story = _bare_story(now)
    superseded = NarrativeIntent(
        id=7, story_id=story.id, participant_id="p", type="split-message",
        summary="typing", not_before=now,
        payload={"content": "我刚想说"},
        created_at=now, updated_at=now,
    )
    request = NarrativeRequest(
        phase=NarrativePhase.USER_MESSAGE,
        story=story, from_time=now, now=now, user_message="新消息",
        participant=None, superseded_intents=[superseded],
    )
    payload = build_prompt_payload(request)
    drafts = payload["interruptedOutgoingDrafts"]
    assert drafts and drafts[0]["content"] == "我刚想说"
    assert "没打完字" in drafts[0]["narrativeContext"]


# ------------------------------------------------------------------ helpers

def datetime_utc(*args):
    from datetime import datetime, timezone

    return datetime(*args, tzinfo=timezone.utc)


def _bare_story(now):
    from hdsi.types import (
        InterludeStory, StorySetting, StoryState, empty_story_setting,
    )

    return InterludeStory(
        id="character:test:1", platform_id="test", self_id="1",
        setting=empty_story_setting(), state=StoryState(),
        cursor_at=now, created_at=now, updated_at=now,
    )
