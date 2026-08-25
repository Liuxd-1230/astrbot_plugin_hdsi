"""Scenarios 10-18: auto advance, offline interval, rest window, follow-ups,
reminder, proactive candidate, agency rejection/recheck, willingness."""

from __future__ import annotations

import asyncio

import pytest

from tests.fakes import wait_for


pytestmark = pytest.mark.asyncio


def _auto_cfg(harness):
    return {
        "enabled": harness.config.runtime.auto_advance_enabled,
        "interval_minutes": harness.config.runtime.auto_advance_interval_minutes,
        "jitter_minutes": 0,
        "follow_up_minutes": [10, 20],
        "follow_up_jitter_minutes": 0,
        "rest_windows": [w.model_dump() for w in harness.config.runtime.rest_windows],
    }


async def _advance_due(harness):
    """Drive the scheduler deterministically after moving the clock."""
    await harness.service.sweep()


async def test_10_auto_advance_writes_life_not_user_message(harness):
    """自动推进补写角色生活；绝不伪造用户消息。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她收拾好房间，泡了一杯茶，翻开了那本没读完的书。",
        # advance phase cannot emit interaction at all (normalize strips it)
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "[不允许]"}},
    })
    h.clock.advance(3600)
    await h.service.advance_story(story, force=True)
    entries = await h.service.recent_entries(story.id, 20)
    kinds = {e.kind for e in entries}
    assert "user-message" not in kinds, "auto advance must never fake a user message"
    script_entries = [e for e in entries if e.kind == "script"]
    assert script_entries and "茶" in script_entries[-1].content
    assert not h.sender.sent, "advance-phase interaction must be stripped"


async def test_11_long_offline_interval_long_passage(harness):
    """长时间离线后一次性补写已发生的生活。"""
    from hdsi.prompt_builder import build_prompt_payload

    h = harness
    story = await h.setup_story()
    h.clock.advance(26 * 3600)
    request = await h.service._build_request(
        story, None, "advance",
        story.cursor_at, h.clock.now(), None, [], [],
    )
    payload = build_prompt_payload(request)
    assert payload["interval"]["elapsedSeconds"] >= 25 * 3600
    ctx = payload["interval"]["nowLocalContext"]
    assert ctx["period"] in ("morning", "afternoon", "evening", "night")
    assert ctx["daylightExpectation"]


async def test_12_rest_window_changes_cadence(harness):
    """休息窗口内使用更长推进间隔。"""
    from hdsi.scheduler import active_rest_window, automatic_interval_minutes

    h = harness
    story = await h.setup_story()
    cfg = _auto_cfg(h)
    # 23:00-07:00 default rest window; move virtual clock into it.
    story_local_night = _utc_time_for_local(h, hour=1)
    window = active_rest_window(cfg["rest_windows"], story.setting.timezone, story_local_night)
    assert window is not None and window["label"] == "night sleep"
    day_time = _utc_time_for_local(h, hour=14)
    assert active_rest_window(cfg["rest_windows"], story.setting.timezone, day_time) is None
    minutes = automatic_interval_minutes(story, story_local_night, cfg)
    assert 120 <= minutes <= 240, f"rest cadence expected, got {minutes}"


async def test_13_conversation_follow_up_after_turn(harness):
    """对话结束后安排 10/20 分钟短期补写，并按序消费。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她回完消息，继续手头的事。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "好的"}},
    })
    assert await h.service.receive(h.event(content="那就这样"))
    ok = await wait_for(lambda: len(h.sender.sent) >= 1)
    assert ok
    fresh = await h.service.get_story(story.id)
    followups = fresh.state.automation.conversation_follow_up_at
    assert len(followups) == 2, f"expected 10/20min follow-ups, got {followups}"
    participant_id = fresh.state.automation.conversation_follow_up_participant_id
    assert participant_id

    # First short pass fires when due.
    h.clock.advance(11 * 60)
    h.narrator.enqueue({"script": "十分钟后，她起身活动了一下。"})
    await h.service.sweep()
    calls = [c for c in h.narrator.calls if c["phase"] == "conversation-follow-up"]
    assert calls, "first follow-up should use conversation-follow-up phase"
    remaining = (await h.service.get_story(story.id)).state.automation.conversation_follow_up_at
    assert len(remaining) == 1, "consumed follow-up removed; second retained"


async def test_14_reminder_intent_roundtrip(harness):
    """提醒：用户回合创建 intent → 到期后经 intent-due 回合真实发送。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她答应了。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "好，我会记得叫你"}},
        "intents": [{
            "type": "reminder",
            "summary": "提醒用户三点半开会",
            "notBefore": _iso_offset(harness, minutes=15),
            "payload": {},
            "participantId": None,
        }],
    })
    assert await h.service.receive(h.event(content="提醒我三点半开会"))
    await asyncio.sleep(0.1)

    # Due turn re-evaluates with current life.
    h.narrator.enqueue({
        "script": "到了约定的时间，她拿起手机。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "三点半了，该开会啦"}},
    })
    h.clock.advance(16 * 60)
    await h.service.sweep()
    assert any("开会" in t for t in h.sender.texts()), h.sender.texts()


async def test_15_proactive_candidate_send_now(harness):
    """生活产生联系理由 → Agency 放行 → 立即主动联系。"""
    h = harness
    h.config.runtime.allow_proactive_messages = True
    story = await h.setup_story()
    participant_rows = await h.db.get("interlude_participant", {})
    pid = participant_rows[0]["id"]

    h.narrator.enqueue({
        "script": "她在旧书店看到了对方一直想找的那本书。",
        "agencyWindow": {
            "activityLoad": "free", "privacy": "private",
            "deviceAccess": "available", "validUntil": _iso_offset(h, hours=2),
            "basis": "她刚离开书店，在公交站等车。",
            "sourceEntryIds": [],
        },
        "proactiveContact": {
            "participantId": pid, "origin": "life-event",
            "motive": "看到对方找的书有现货", "disclosure": "ordinary",
            "sourceEntryIds": [], "willingness": 0.9,
            "outcome": "send-now",
        },
        "crossConversationActions": [{
            "participantId": pid, "mode": "immediate",
            "content": "我在书店看到你找的那本书了！", "willingness": 0.9,
            "reason": "life-event",
        }],
    })
    h.clock.advance(3 * 3600)
    messages = await h.service.advance_story(story, force=True)
    await h.service.send_outgoing_messages(story, messages)
    texts = h.sender.texts()
    assert any("书" in t for t in texts), f"proactive contact should deliver: {texts}"
    intents = await h.db.get("interlude_intent",
                             {"story_id": story.id, "status": "pending"})
    assert not [i for i in intents if i["type"] == "proactive-check"]


async def test_16_agency_rejection_matrix(harness):
    """容量矩阵：设备不可用/日程过载/隐私不足/意愿不足都必须拒绝立即发送。"""
    from hdsi.agency import evaluate_agency_capacity
    from hdsi.types import AgencyConfig, AgencyWindowState, ProactiveContactDraft, iso

    now = datetime_now()
    config = AgencyConfig()
    from datetime import timedelta

    future = iso(now + timedelta(hours=1))
    base_window = {
        "activity_load": "free", "privacy": "private", "device_access": "available",
        "valid_until": future, "basis": "b", "source_entry_ids": [1], "updated_at": iso(now),
    }
    candidate = ProactiveContactDraft(
        participant_id="p", origin="life-event", motive="m",
        disclosure="ordinary", source_entry_ids=[1], willingness=0.9,
        outcome="send-now",
    )
    cases = [
        ({"deviceAccess": "unavailable"}, "ordinary", "device-unavailable"),
        ({"deviceAccess": "limited"}, "ordinary", "device-limited"),
        ({"activityLoad": "overloaded"}, "ordinary", "schedule-overloaded"),
        ({"privacy": "public"}, "personal", "privacy-insufficient"),
        ({"activityLoad": "occupied"}, "relationship-follow-up", "schedule-occupied"),
    ]
    for override, disclosure, expected_reason in cases:
        mapped_override = {
            {"deviceAccess": "device_access", "activityLoad": "activity_load",
             "privacy": "privacy"}.get(key, key): value
            for key, value in override.items()
        }
        window = AgencyWindowState(**{**base_window, **mapped_override})
        cand = candidate.model_copy(update={"disclosure": disclosure})
        result = evaluate_agency_capacity(window, cand, now, config)
        assert not result.allowed, f"{override}/{disclosure} should be blocked"
        assert result.reason == expected_reason
    # promise bypasses occupied schedule
    window = AgencyWindowState(**base_window | {"activityLoad": "occupied"})
    promise = candidate.model_copy(update={"origin": "promise"})
    assert evaluate_agency_capacity(window, promise, now, config).allowed


async def test_17_agency_recheck_via_proactive_check(harness):
    """recheck-later 创建 proactive-check（无预写台词），到期重查时重新裁决。"""
    h = harness
    h.config.runtime.allow_proactive_messages = True
    story = await h.setup_story()
    participant_rows = await h.db.get("interlude_participant", {})
    pid = participant_rows[0]["id"]

    h.narrator.enqueue({
        "script": "她想分享一件事，但这会儿对方大概在工作。",
        "agencyWindow": {
            "activityLoad": "free", "privacy": "public",
            "deviceAccess": "available", "validUntil": _iso_offset(h, hours=3),
            "basis": "她在咖啡馆，周围有人。",
            "sourceEntryIds": [],
        },
        "proactiveContact": {
            "participantId": pid, "origin": "relationship-follow-up",
            "motive": "想分享今天遇到的事", "disclosure": "ordinary",
            "sourceEntryIds": [], "willingness": 0.8,
            "outcome": "recheck-later", "notBefore": _iso_offset(h, minutes=30),
            "expiresAt": _iso_offset(h, hours=4),
        },
    })
    h.clock.advance(3 * 3600)
    await h.service.advance_story(story, force=True)
    checks = await h.db.get("interlude_intent",
                            {"story_id": story.id, "type": "proactive-check",
                             "status": "pending"})
    assert len(checks) == 1, "recheck-later should create a proactive-check"
    payload = checks[0]["payload"]
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    assert "content" not in payload or not payload.get("content"), \
        "proactive-check must never store a pre-written message"
    assert not any("想分享" in t for t in h.sender.texts())

    # Due recheck: agency now allows, model sends via interaction.reply.
    h.narrator.enqueue({
        "script": "一小时后她回到家里，环境安静私密。",
        "agencyWindow": {
            "activityLoad": "free", "privacy": "private",
            "deviceAccess": "available", "validUntil": _iso_offset(h, hours=2),
            "basis": "她到家了。", "sourceEntryIds": [],
        },
        "proactiveContact": {
            "participantId": pid, "origin": "relationship-follow-up",
            "motive": "想分享今天遇到的事", "disclosure": "ordinary",
            "sourceEntryIds": [], "willingness": 0.85, "outcome": "send-now",
        },
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "今天遇到了一件有意思的事"}},
    })
    h.clock.advance(31 * 60)
    await h.service.sweep()
    assert any("有意思的事" in t for t in h.sender.texts()), h.sender.texts()


async def test_18_willingness_threshold_blocks_contact(harness):
    """willingness 低于阈值：不发送，自然放下。"""
    h = harness
    h.config.runtime.allow_proactive_messages = True
    h.config.runtime.proactive_willingness_threshold = 0.65
    story = await h.setup_story()
    participant_rows = await h.db.get("interlude_participant", {})
    pid = participant_rows[0]["id"]
    h.narrator.enqueue({
        "script": "她有点想起对方，但并不强烈。",
        "agencyWindow": {
            "activityLoad": "free", "privacy": "private",
            "deviceAccess": "available", "validUntil": _iso_offset(h, hours=2),
            "basis": "在家休息。", "sourceEntryIds": [],
        },
        "proactiveContact": {
            "participantId": pid, "origin": "relationship-follow-up",
            "motive": "随便聊聊", "disclosure": "ordinary",
            "sourceEntryIds": [], "willingness": 0.4,
            "outcome": "send-now",
        },
        "crossConversationActions": [{
            "participantId": pid, "mode": "immediate",
            "content": "[低意愿不应发送]", "willingness": 0.4,
        }],
    })
    h.clock.advance(2 * 3600)
    await h.service.advance_story(story, force=True)
    assert not h.sender.sent, f"low willingness must block delivery: {h.sender.texts()}"


# ------------------------------------------------------------------ helpers

def datetime_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _iso_offset(harness, **kwargs):
    from datetime import timedelta
    from hdsi.types import iso

    return iso(harness.clock.now() + timedelta(**kwargs))


def _utc_time_for_local(harness, hour: int):
    """Return a UTC instant whose Asia/Shanghai local time has the given hour."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    base = harness.clock.now().astimezone(ZoneInfo("Asia/Shanghai")).replace(hour=hour, minute=30)
    return base.astimezone(timezone.utc)
