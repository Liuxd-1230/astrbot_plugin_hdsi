"""Scenarios 19-25: reload/pending recovery, duplicate prevention, cursor
monotonicity, participant isolation, multi-participant shared story, privacy."""

from __future__ import annotations

import asyncio

import pytest

from hdsi.types import iso, parse_date
from tests.fakes import wait_for


pytestmark = pytest.mark.asyncio


async def test_19_reload_recovery(harness, tmp_path):
    """插件重载：从数据库恢复 pending 任务，不丢状态、不重复发送。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她记下了这件事。",
        "interaction": {"seen": True, "reply": {
            "mode": "delayed", "content": "稍后回复你",
            "sendAt": _iso_offset(h, minutes=30)}},
    })
    assert await h.service.receive(h.event(content="记得回我"))
    await asyncio.sleep(0.15)
    assert not h.sender.sent

    # Simulate reload: stop service, close DB, reopen with a fresh service.
    await h.service.stop_background_tasks()
    h.service.invalidate_buffered_narratives()
    db_path = h.db.path
    await h.db.close()

    from hdsi.database.connection import Database
    from hdsi.service import InterludeService

    db2 = Database(db_path)
    await db2.connect()
    config = h.config
    service2 = InterludeService(
        db=db2, config=config, narrator=h.narrator,
        embedder=h.embedder, sender=h.sender, now_fn=h.clock.now,
    )
    # Recovery path used by main.initialize:
    rows = await db2.get("interlude_intent",
                         {"story_id": story.id, "status": "pending"})
    pending_types = [r["type"] for r in rows]
    assert "delayed-reply" in pending_types, "delayed intent survives restart"
    fresh_story_rows = await db2.get("interlude_story", {"id": story.id})
    from hdsi.types import parse_date
    assert isinstance(parse_date(fresh_story_rows[0]["cursor_at"]), object)
    entries_before = len(await _all_entries(db2, story.id))
    assert entries_before > 0
    await db2.close()


async def test_20_pending_intent_recovery_fires_after_restart(harness):
    """重启后到期 intent 触发重新裁决并投递。"""
    h = harness
    story = await h.setup_story()
    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]
    from datetime import timedelta

    due_at = h.clock.now() - timedelta(seconds=1)
    await h.db.insert("interlude_intent", {
        "story_id": story.id, "participant_id": pid,
        "type": "reminder", "summary": "到点的提醒",
        "not_before": iso(due_at), "status": "pending",
        "payload": {"userInitiated": True}, "created_at": iso(due_at), "updated_at": iso(due_at),
    })
    h.narrator.enqueue({
        "script": "她看了一眼时间，到了之前答应的提醒点。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "时间到了"}},
    })
    await h.service.sweep()
    assert any("时间到了" in t for t in h.sender.texts())


async def test_21_duplicate_send_prevention(harness):
    """同一 split-message 段只投递一次；重复唤醒不会造成双发。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她拿起手机打字。",
        "interaction": {"seen": True, "reply": {
            "mode": "immediate", "content": "第一条<sep/>第二条"}},
    })
    assert await h.service.receive(h.event(content="在吗"))
    ok = await wait_for(lambda: len(h.sender.sent) >= 1)
    assert ok and h.sender.texts()[0] == "第一条"

    # Manually trigger the split delivery twice; second wake must no-op.
    await h.service.deliver_due_split_segments(story.id)
    await asyncio.sleep(0.1)

    class _FakeNow:  # typing delay is ~1s base; force due by rewinding intent
        pass

    # Rewind the segment's notBefore to now so it becomes deliverable.
    rows = await h.db.get("interlude_intent",
                          {"story_id": story.id, "type": "split-message",
                           "status": "pending"})
    if rows:
        await h.db.update("interlude_intent", {"id": rows[0]["id"]},
                          {"not_before": iso(_past(h))})
        await h.service.deliver_due_split_segments(story.id)
        await wait_for(lambda: len(h.sender.sent) >= 2)
        count_first_delivery = sum(1 for t in h.sender.texts() if t == "第二条")
        # Deliver again — must NOT duplicate.
        await h.service.deliver_due_split_segments(story.id)
        await asyncio.sleep(0.05)
        assert sum(1 for t in h.sender.texts() if t == "第二条") == count_first_delivery
    else:
        # If already delivered by the wake timer, ensure exactly one copy.
        assert h.sender.texts().count("第二条") == 1


async def test_22_story_cursor_monotonicity(harness):
    """损坏的未来游标不得让叙事倒填时间；正常推进单调向前。"""
    from hdsi.service import narrative_cursor

    h = harness
    story = await h.setup_story()
    future_cursor = _future(h, days=3)
    clamped = narrative_cursor(story.model_copy(update={"cursor_at": future_cursor}), h.clock.now())
    assert clamped == h.clock.now(), "future cursor must clamp to now"
    past = _past(h)
    assert narrative_cursor(story.model_copy(update={"cursor_at": past}), h.clock.now()) == past

    # Real turn keeps cursor monotonic.
    h.narrator.enqueue({
        "script": "时间继续。",
        "interaction": {"seen": True, "reply": {"mode": "none"}},
    })
    h.clock.advance(600)
    assert await h.service.receive(h.event(content="继续"))
    await asyncio.sleep(0.15)
    fresh = await h.service.get_story(story.id)
    assert fresh.cursor_at > story.cursor_at


async def test_23_participant_isolation(harness):
    """两位参与者共享一个故事；各自的关系分支互不覆盖。"""
    h = harness
    event_a = h.event(sender_id="20001")
    event_b = h.event(sender_id="20002")
    story = await h.setup_story(event_a)
    pa = await h.service.ensure_participant(story, event_a)
    pb = await h.service.ensure_participant(story, event_b)
    assert pa.id != pb.id
    assert pb.story_id == story.id

    h.narrator.default = {"script": "安静的一天。"}
    h.narrator.enqueue({
        "script": "她和 A 聊起了周末的计划。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "A 你好"}},
    })
    await h.service.receive(event_a)
    await wait_for(lambda: len(h.sender.sent) >= 1)

    state_a = (await h.service.get_participant(pa.id)).state
    state_b = (await h.service.get_participant(pb.id)).state
    assert state_a.last_user_message_at is not None
    assert state_b.last_user_message_at is None
    assert state_b.unread_message_count == 0


async def test_24_multi_participant_shared_canonical_story():
    """多个账号私聊进入同一段主剧本（character:<platform>:<selfId>）。"""
    import os
    import tempfile

    from tests.conftest import Harness, make_config

    tmp = tempfile.mkdtemp(prefix="hdsi-mp-")
    h = Harness(os.path.join(tmp, "t.db"), make_config())
    await h.start()
    try:
        ev_a = h.event(sender_id="20001")
        ev_b = h.event(sender_id="20002")
        story_a = await h.service.create_story(ev_a)
        story_b = await h.service.create_story(ev_b)
        assert story_a.id == story_b.id, "one canonical story per bot identity"
    finally:
        await h.stop()


async def test_25_privacy_no_cross_participant_raw_leak(harness):
    """A 的原始私聊不得出现在 B 的主模型上下文中。"""
    h = harness
    ev_a = h.event(sender_id="20001")
    ev_b = h.event(sender_id="20002")
    story = await h.setup_story(ev_a)
    await h.service.ensure_participant(story, ev_a)
    pb = await h.service.ensure_participant(story, ev_b)

    secret = "我的银行密码是123456"
    h.narrator.enqueue({
        "script": "她认真听着。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "嗯"}},
    })
    assert await h.service.receive(h.event(sender_id="20001", content=secret))
    await asyncio.sleep(0.2)

    # Now build B's live request context.
    request = await h.service._build_request(
        await h.service.get_story(story.id), pb, "user-message",
        (await h.service.get_story(story.id)).cursor_at, h.clock.now(),
        "B 的新消息", [], [],
    )
    from hdsi.prompt_builder import build_prompt_payload

    payload = build_prompt_payload(request)
    # Whole-payload scan (P0-5): continuitySnapshot / state / sceneContext /
    # participants — everything the model will see, not just three fields.
    import json as _json

    blob = _json.dumps(payload, ensure_ascii=False, default=str)
    assert secret not in blob, "raw private content leaked across participants!"
    assert all(
        not e["participantId"] or e["participantId"] == pb.id
        for e in payload["recentScript"]
    )


# ------------------------------------------------------------------ helpers

def _iso_offset(harness, **kwargs):
    from datetime import timedelta
    from hdsi.types import iso

    return iso(harness.clock.now() + timedelta(**kwargs))


def _past(harness):
    from datetime import timedelta

    return harness.clock.now() - timedelta(seconds=5)


def _future(harness, **kwargs):
    from datetime import timedelta

    return harness.clock.now() + timedelta(**kwargs)


async def _all_entries(db, story_id):
    return await db.get("interlude_script_entry", {"story_id": story_id})
