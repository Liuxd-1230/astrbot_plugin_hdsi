"""Scenarios 32-40: fallbacks, malformed output, timeout/retry, serialization,
scheduler dedup, sqlite resilience, cross-story concurrency."""

from __future__ import annotations

import asyncio

import pytest

from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


async def test_32_embedding_disabled_fallback(harness):
    """Embedding 关闭时事实检索退化为规则排序，不报错。"""
    h = harness
    story = await h.setup_story()
    from hdsi.types import FactDraft, ScriptEntryDraft

    e = await h.service.append_entry(story.id, ScriptEntryDraft(
        kind="script", actor="narrator", content="背景", occurred_at=iso_now(h),
    ), h.clock.now())
    for i in range(3):
        await h.service.persist_fact(story.id, FactDraft(
            scope="world", content=f"世界事实{i}", source_entry_ids=[e.id],
            importance=0.5 + i * 0.1, confidence=0.8,
        ), [e], h.clock.now())
    facts = await h.service.facts(story.id, 10)
    assert len(facts) == 3
    assert facts[0].importance >= facts[-1].importance, "rule ranking applies"


async def test_33_browser_disabled_fallback(harness):
    """浏览器未启用：browser-research 意图记录 blocked 观察，绝不阻塞叙事。"""
    h = harness
    h.config.browser.enabled = False
    story = await h.setup_story()
    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]
    from datetime import timedelta
    from hdsi.types import iso

    intent = {
        "story_id": story.id, "participant_id": pid,
        "type": "browser-research", "summary": "查资料",
        "not_before": iso(h.clock.now() - timedelta(seconds=1)),
        "status": "pending",
        "payload": {"mode": "search", "query": "weather", "purpose": "查天气"},
        "created_at": iso(h.clock.now()), "updated_at": iso(h.clock.now()),
    }
    await h.db.insert("interlude_intent", intent)
    h.narrator.default = {"script": "她翻了下手机里的资料，没查到。"}
    await h.service.sweep()
    observations = await h.db.get("interlude_web_observation", {"story_id": story.id})
    assert observations and observations[0]["status"] in ("blocked", "failed")
    intents = await h.db.get("interlude_intent",
                             {"story_id": story.id, "type": "browser-research"})
    assert all(i["status"] == "completed" for i in intents)


async def test_34_malformed_llm_output_no_state_pollution(harness):
    """模型输出坏 JSON：状态不受污染，安排 narrative-retry。"""
    class BadNarrator:
        calls = 0

        async def decide_raw(self, request, **kwargs):
            BadNarrator.calls += 1
            raise ValueError("Narrative provider returned invalid JSON")

        async def compact_raw(self, **kwargs):
            return {}

        async def analyze_alter(self, *a, **k):
            return {"description": ""}

    h = harness
    story = await h.setup_story()
    original = h.service.narrator
    h.service.narrator = BadNarrator()
    assert await h.service.receive(h.event(content="你好"))
    ok = await wait_for_async(lambda: _has_retry_intent(h, story.id), timeout=2.0)
    assert ok, "failed user turn must persist a narrative-retry intent"
    entries = await h.service.recent_entries(story.id, 20)
    assert not any(e.kind == "script" for e in entries), "no script may be written on failure"
    assert not any(e.kind == "character-message" for e in entries)
    fresh = await h.service.get_story(story.id)
    assert fresh.cursor_at == story.cursor_at, "cursor must not advance on failure"
    h.service.narrator = original


async def test_35_provider_timeout_schedules_retry(harness):
    """Provider 超时进入持久化重试；恢复后重试回合成功投递。"""
    h = harness
    story = await h.setup_story()

    class TimeoutThenOk:
        phase = 0

        async def decide_raw(self, request, **kwargs):
            TimeoutThenOk.phase += 1
            if TimeoutThenOk.phase == 1:
                raise TimeoutError("simulated provider timeout")
            return {"script": "她终于收到了。",
                    "interaction": {"seen": True,
                                    "reply": {"mode": "immediate", "content": "刚才信号不好"}}}, []

        async def compact_raw(self, **kwargs):
            return {}

        async def analyze_alter(self, *a, **k):
            return {"description": ""}

    narrator = TimeoutThenOk()
    original = h.service.narrator
    h.service.narrator = narrator
    assert await h.service.receive(h.event(content="在吗"))
    ok = await wait_for_async(lambda: _has_retry_intent(h, story.id))
    assert ok
    # Retry turn becomes due.
    h.clock.advance(120)
    await h.service.sweep()
    assert any("信号不好" in t for t in h.sender.texts()), h.sender.texts()
    h.service.narrator = original


async def test_36_plugin_reload_recovery():
    """重载（新 service 实例 + 同一 DB）后故事与参与者完整保留。"""
    import os
    import tempfile

    from hdsi.database.connection import Database
    from hdsi.service import InterludeService
    from tests.conftest import Harness, make_config
    from tests.fakes import SilentEmbedder, SenderRecorder, ScriptedNarrator, VirtualClock

    tmp = tempfile.mkdtemp(prefix="hdsi-reload-")
    db_path = os.path.join(tmp, "t.db")
    clock = VirtualClock()
    sender = SenderRecorder()
    h = Harness(db_path, make_config())
    h.db = Database(db_path)
    h.db._conn = None
    # use one shared connection set for both lifecycles
    loop = asyncio.get_event_loop()

    async def run() -> None:
        await h.start()
        story = await h.service.create_story(h.event(content="init"))
        h.narrator.enqueue({"script": "第一段生活。",
                            "interaction": {"seen": True, "reply": {"mode": "none"}}})
        await h.service.receive(h.event(content="第一次对话"))
        ok = False
        for _ in range(50):
            entries = await h.service.recent_entries(story.id, 20)
            if any(e.kind == "script" for e in entries):
                ok = True
                break
            await asyncio.sleep(0.05)
        assert ok, "first lifecycle should write a script entry"
        await h.stop()

        # Reload.
        db2 = Database(db_path)
        await db2.connect()
        service2 = InterludeService(
            db=db2, config=make_config(), narrator=ScriptedNarrator(),
            embedder=SilentEmbedder(), sender=sender, now_fn=clock.now,
        )
        fresh_story_rows = await db2.get("interlude_story", {"id": story.id})
        assert fresh_story_rows
        participants = await service2.participants(story.id, include_paused=True)
        assert len(participants) == 1
        entries = await db2.get("interlude_script_entry", {"story_id": story.id})
        kinds = {e["kind"] for e in entries}
        assert "user-message" in kinds
        await db2.close()

    await run()


async def test_37_scheduler_duplicate_prevention(harness):
    """sweep 重入保护 + 到期意图只处理一次。"""
    h = harness
    story = await h.setup_story()
    from datetime import timedelta
    from hdsi.types import iso

    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]
    due_at = h.clock.now() - timedelta(seconds=1)
    await h.db.insert("interlude_intent", {
        "story_id": story.id, "participant_id": pid,
        "type": "reminder", "summary": "一次性提醒",
        "not_before": iso(due_at), "status": "pending",
        "payload": {"userInitiated": True},
        "created_at": iso(due_at), "updated_at": iso(due_at),
    })
    h.narrator.enqueue({
        "script": "时间到了。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "提醒你"}},
    })
    await asyncio.gather(h.service.sweep(), h.service.sweep(), h.service.sweep())
    await asyncio.sleep(0.05)
    texts = [t for t in h.sender.texts() if "提醒" in t]
    assert len(texts) <= 1, f"duplicate delivery: {h.sender.texts()}"
    pending = await h.db.get("interlude_intent",
                             {"story_id": story.id, "status": "pending",
                              "type": "reminder"})
    assert not pending


async def test_38_sqlite_write_failure_fallback(harness):
    """SQLite 写入瞬时失败自动重试；持续失败时管理操作降级为逻辑清除。"""
    h = harness
    story = await h.setup_story()

    h.db.fail_transient_writes = 2
    entry = await h.service.append_entry(story.id, {
        "kind": "admin-note", "actor": "system", "content": "写入重试",
        "occurred_at": iso_now(h),
    }, h.clock.now())
    assert entry.id > 0, "transient failures retried transparently"
    rows = await h.db.get("interlude_script_entry", {"id": entry.id})
    assert rows, "entry persisted despite transient failures"

    # Persistent failure → purge must raise CLEANLY (no half-applied ghost
    # state), leaving the database consistent and readable.
    h.db.fail_all_writes = True
    with pytest.raises(Exception):
        await h.service.purge_all_story_data(story.id)
    rows_mid = await h.db.get("interlude_story", {"id": story.id})
    assert rows_mid, "story row still readable after failed purge"

    # Once writes recover, the same purge completes the logical redaction.
    h.db.fail_all_writes = False
    await h.service.purge_all_story_data(story.id)
    entries = await h.db.get("interlude_script_entry", {"story_id": story.id})
    assert all(e["kind"] == "redacted" for e in entries)


async def test_39_same_story_serialization(harness):
    """同一故事的写库操作串行：并发 receive 不产生交叉写入。"""
    h = harness
    story = await h.setup_story()
    order: list[str] = []
    original_append = h.service.append_entry

    async def traced_append(story_id, draft, now, participant_id=""):
        marker = getattr(draft, "content", None) or (draft.get("content") if isinstance(draft, dict) else "")
        order.append(f"start:{marker[:6]}")
        result = await original_append(story_id, draft, now, participant_id)
        order.append(f"end:{marker[:6]}")
        return result

    h.service.append_entry = traced_append
    h.narrator.default = {"script": "安静。"}
    events = [h.event(sender_id=str(20000 + i), content=f"消息{i}") for i in range(1)]
    tasks = [asyncio.create_task(h.service.receive(e)) for e in events * 3]
    await asyncio.gather(*tasks)
    await asyncio.sleep(0.3)
    # start/end must interleave without nesting
    stack_depth = 0
    for item in order:
        stack_depth += 1 if item.startswith("start:") else -1
        assert stack_depth <= 1, f"concurrent entry writes detected: {order}"


async def test_40_cross_story_concurrency():
    """不同 bot 身份的故事互不阻塞、互不串扰。"""
    import os
    import tempfile

    from tests.conftest import Harness, make_config
    from tests.fakes import wait_for, wait_for_async

    tmp = tempfile.mkdtemp(prefix="hdsi-x-")
    h = Harness(os.path.join(tmp, "t.db"), make_config())
    await h.start()
    try:
        ev_a = h.event(sender_id="20001")
        ev_b = h.event(sender_id="20002", self_id="99999",
                       platform_id="telegram",
                       umo="telegram:FriendMessage:20002")
        h.config.platform_gate.bot_accounts.append(_rule("99999"))
        h.config.platform_gate.user_accounts.append(_rule_tg("20002"))
        story_a = await h.service.create_story(ev_a)
        story_b = await h.service.create_story(ev_b)
        assert story_a.id != story_b.id

        h.narrator.enqueue({
            "script": "A 的生活。",
            "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "给A"}},
        })
        h.narrator.enqueue({
            "script": "B 的生活。",
            "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "给B"}},
        })
        r1 = await h.service.receive(ev_a)
        r2 = await h.service.receive(ev_b)
        assert r1 and r2
        await wait_for(lambda: len(h.sender.sent) >= 2, timeout=3)
        texts = sorted(h.sender.texts())
        assert texts == ["给A", "给B"]
    finally:
        await h.stop()


# ------------------------------------------------------------------ helpers

def _rule(id_):
    from hdsi.config import AccessRule

    return AccessRule(id=id_)


def _rule_tg(id_):
    from hdsi.config import AccessRule

    return AccessRule(id=id_)


def iso_now(harness):
    from hdsi.types import iso

    return iso(harness.clock.now())


async def _has_retry_intent(harness, story_id) -> bool:
    rows = await harness.db.get(
        "interlude_intent",
        {"story_id": story_id, "status": "pending", "type": "narrative-retry"},
    )
    return bool(rows)


def fresh_story_status(rows):
    state_raw = rows[0].get("state")
    return rows[0].get("status")
