"""Scenarios 5-9: immediate / silent / delayed / delayed cancellation / intent re-eval."""

from __future__ import annotations

import asyncio

import pytest

from hdsi.types import NarrativeIntent
from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


async def test_05_immediate_reply(harness):
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她正在整理书桌。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "我在"}},
    })
    assert await h.service.receive(h.event(content="在吗"))
    await wait_for(lambda: len(h.sender.sent) >= 1)
    assert h.sender.texts() == ["我在"]
    entries = await h.service.recent_entries(story.id, 20)
    assert any(e.kind == "character-message" and e.content == "我在" for e in entries)


async def test_06_silent_seen(harness):
    """seen=true + mode=none：角色看见但不回复，无投递。"""
    h = harness
    story = await h.setup_story()
    h.clock.advance(120)  # some time passed since story creation
    h.narrator.enqueue({
        "script": "她看了一眼消息，正忙着手头的事，先放下了手机。",
        "interaction": {"seen": True, "reply": {"mode": "none"}},
    })
    assert await h.service.receive(h.event(content="随便聊聊"))
    await asyncio.sleep(0.2)
    assert not h.sender.sent
    participant_rows = await h.db.get("interlude_participant", {})
    # seen marks unread as read
    state = _participant_state(participant_rows)
    assert state["unread_message_count"] == 0
    # cursor advanced even without visible message
    fresh = await h.service.get_story(story.id)
    assert fresh.cursor_at > story.cursor_at


async def test_06b_silent_unseen(harness):
    """seen=false：角色没有注意到消息。"""
    h = harness
    await h.setup_story()
    h.narrator.enqueue({
        "script": "手机在包里静音着。",
        "interaction": {"seen": False, "reply": {"mode": "none"}},
    })
    assert await h.service.receive(h.event(content="喂？"))
    await asyncio.sleep(0.2)
    rows = await h.db.get("interlude_participant", {})
    state = _participant_state(rows)
    assert state["unread_message_count"] >= 1


async def test_07_delayed_reply_creates_intent_not_delivery(harness):
    """delayed 回复创建到期意图，绝不立即发送，也不预发台词。"""
    h = harness
    story = await h.setup_story()
    send_at = _iso_offset(h, minutes=30)
    h.narrator.enqueue({
        "script": "她想等手头的实验告一段落再回。",
        "interaction": {"seen": True, "reply": {
            "mode": "delayed", "content": "刚看到，晚上聊", "sendAt": send_at}},
    })
    assert await h.service.receive(h.event(content="晚上有空吗"))
    await asyncio.sleep(0.15)
    assert not h.sender.sent, "delayed reply must not be delivered immediately"
    intents = await h.db.get("interlude_intent",
                             {"story_id": story.id, "type": "delayed-reply",
                              "status": "pending"})
    assert len(intents) == 1
    payload = intents[0]["payload"]
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    assert payload.get("content") == "刚看到，晚上聊"


async def test_08_delayed_cancellation_on_new_message(harness):
    """新消息取消未发送的延迟计划。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她决定晚点再回。",
        "interaction": {"seen": True, "reply": {
            "mode": "delayed", "content": "待会再说",
            "sendAt": _iso_offset(h, minutes=20)}},
    })
    assert await h.service.receive(h.event(content="先去忙吧"))
    await asyncio.sleep(0.1)

    # New user message cancels planned outgoing messages.
    h.narrator.enqueue({
        "script": "她看到新消息，重新想了想。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "嗯嗯"}},
    })
    assert await h.service.receive(h.event(content="对了还有一件事"))
    await asyncio.sleep(0.2)

    intents = await h.db.get("interlude_intent",
                             {"story_id": story.id, "status": "pending"})
    delayed_pending = [i for i in intents if i["type"] == "delayed-reply"]
    assert not delayed_pending, "planned delay must be cancelled by newer input"
    cancelled_entries = await h.db.fetch_all(
        "SELECT kind FROM interlude_script_entry WHERE story_id=? AND kind='intent-cancelled'",
        (story.id,),
    )
    assert cancelled_entries


async def test_09_intent_due_triggers_fresh_evaluation(harness):
    """延迟意图到期 → 重新读取当前生活 → 主模型重新裁决（不是直接发送旧台词）。"""
    h = harness
    story = await h.setup_story()

    # Seed a due delayed-reply intent directly (as persist_decision would).
    now = h.clock.now()
    from hdsi.types import iso

    await h.db.insert("interlude_intent", {
        "story_id": story.id,
        "participant_id": (await h.db.get("interlude_participant", {}))[0]["id"],
        "type": "delayed-reply",
        "summary": "The character decided to send a delayed reply.",
        "not_before": _iso_offset(h, seconds=-1),
        "status": "pending",
        "payload": {"content": "[旧台词绝不能直接发送]", "userInitiated": True},
        "created_at": iso(now), "updated_at": iso(now),
    })
    # The narrator is asked to RE-decide; it answers with a different reply.
    h.narrator.enqueue({
        "script": "她忙完了一段时间，情况已经变了，她决定现在简短回复。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "现在方便了，说吧"}},
    })
    await h.service.sweep()
    await asyncio.sleep(0.05)

    call = next((c for c in h.narrator.calls if c["phase"] == "intent-due"), None)
    assert call is not None, "due intent must open a new intent-due narrative turn"
    assert h.sender.texts() == ["现在方便了，说吧"], (
        f"delivery must come from the NEW decision: {h.sender.texts()}"
    )
    # The old intent completes.
    pending = await h.db.get(
        "interlude_intent", {"story_id": story.id, "status": "pending"})
    assert not [i for i in pending if i["type"] == "delayed-reply"]


# ------------------------------------------------------------------ helpers

def _participant_state(rows):
    payload = rows[0]["state"]
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if hasattr(payload, "unread_message_count"):
        return {
            "unread_message_count": payload.unread_message_count,
            "pending_reply_count": payload.pending_reply_count,
        }
    return payload


def _iso_offset(harness, **kwargs):
    from datetime import timedelta
    from hdsi.types import iso

    return iso(harness.clock.now() + timedelta(**kwargs))
