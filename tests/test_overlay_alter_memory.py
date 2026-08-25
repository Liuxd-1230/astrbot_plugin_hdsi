"""Scenarios 26-31: overlay evidence threshold & clear, alter accumulation &
decay, compaction preservation, fact provenance."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


def _patch_draft(target="character", path="personality.calm", value="她变得更沉稳。",
                 evidence="多次表现出耐心。", confidence=0.9, impact="minor",
                 source_ids=None):
    from hdsi.types import StatePatchDraft

    return StatePatchDraft(
        target=target, path=path, proposed_value=value, evidence=evidence,
        confidence=confidence, impact=impact,
        source_entry_ids=source_ids or [],
    )


async def _seed_compaction_entries(harness, story_id, days_spread=3, turns=4):
    """Create real script entries across distinct days for evidence."""
    from hdsi.types import ScriptEntryDraft

    entries = []
    for index in range(turns * days_spread):
        at = harness.clock.now() - __import__("datetime").timedelta(days=index % days_spread,
                                                                    hours=index)
        entry = await harness.service.append_entry(story_id, ScriptEntryDraft(
            kind="script", actor="narrator",
            content=f"第{index}回合：她安静地处理着事务。",
            occurred_at=iso(at),
        ), harness.clock.now())
        entries.append(entry)
        harness.clock.advance(3600)
    return entries


async def test_26_overlay_evidence_threshold(harness):
    """普通 overlay 变化需要：置信度达标 + 独立回合数 + 跨日期证据；否则只累计。"""
    from datetime import timedelta
    from hdsi.types import iso

    h = harness
    story = await h.setup_story()
    memory = h.config.memory

    # Single-turn low-evidence proposal must NOT apply.
    entry = await h.service.append_entry(story.id, {
        "kind": "script", "actor": "narrator", "content": "一次单独的表现。",
        "occurred_at": iso(h.clock.now()),
    }, h.clock.now())
    await h.service.persist_state_patch(
        story, _patch_draft(confidence=0.95, source_ids=[entry.id]), [entry], h.clock.now())
    fresh = await h.service.get_story(story.id)
    assert not fresh.state.setting_overlay.character_profile, \
        "single-turn evidence must not rewrite personality"

    # Enough turns across enough days + high confidence → applies.
    entries = await _seed_compaction_entries(h, story.id, days_spread=3, turns=3)
    await h.service.persist_state_patch(
        await h.service.get_story(story.id),
        _patch_draft(confidence=0.95, source_ids=[e.id for e in entries]),
        entries, h.clock.now(),
    )
    fresh = await h.service.get_story(story.id)
    assert fresh.state.setting_overlay.character_profile and "沉稳" in \
        fresh.state.setting_overlay.character_profile


async def test_27_overlay_clear_and_rollback(harness):
    """clear overlay 只清对应演化层，保留 Canon、剧本与记忆；提案同时失效。"""
    from hdsi.types import iso

    h = harness
    story = await h.setup_story()

    state = (await h.service.get_story(story.id)).state
    overlay = state.setting_overlay.model_copy(update={
        "character_profile": "长期积累的性格变化", "world": "世界缓慢变化",
    })
    await h.db.update("interlude_story", {"id": story.id}, {
        "state": state.model_copy(update={"setting_overlay": overlay}).model_dump(mode="json"),
    })
    entry = await h.service.append_entry(story.id, {
        "kind": "script", "actor": "narrator", "content": "背景条目。",
        "occurred_at": iso(h.clock.now()),
    }, h.clock.now())
    await h.db.insert("interlude_state_patch", {
        "story_id": story.id, "participant_id": "", "target": "character",
        "path": "p", "proposed_value": "v", "evidence": "e", "confidence": 0.99,
        "impact": "minor", "status": "applied", "source_entry_ids": [entry.id],
        "created_at": iso(h.clock.now()), "applied_at": iso(h.clock.now()),
    })

    result = await h.service.clear_setting_overlay(story, "character")
    fresh = await h.service.get_story(story.id)
    assert not fresh.state.setting_overlay.character_profile
    assert fresh.state.setting_overlay.world == "世界缓慢变化"
    patches = await h.db.get("interlude_state_patch", {"story_id": story.id})
    assert all(p["status"] == "cleared" for p in patches if p["target"] == "character")
    # script untouched
    entries = await h.service.recent_entries(story.id, 10)
    assert any(e.kind == "script" for e in entries)


async def test_28_alter_accumulation_triggers_analysis(harness):
    """Alter 正向累计达到阈值 → 后台分析生成 offset 并清零累计。"""
    import asyncio

    from hdsi.alter import advance_alter_system
    from hdsi.types import AlterSystemState, EmotionalOffset, iso

    class FastNarrator:
        async def decide_raw(self, request, **kwargs):
            return {"script": "x"}, []

        async def compact_raw(self, **kwargs):
            return {}

        async def analyze_alter(self, payload, **kwargs):
            return {"description": "剧情整体转向严肃谨慎。"}

    h = harness
    h.config.alter_system.enabled = True
    h.config.alter_system.base_threshold = 5
    h.config.alter_system.density_factor = 0
    story = await h.setup_story()
    h.service.narrator = FastNarrator()

    cfg = h.service._alter_config_dict()
    now = h.clock.now()
    result = advance_alter_system(None, 3, "user-message", now, cfg)
    result2 = advance_alter_system(result.state, 3, "advance", now, cfg)
    state = result2.state
    assert abs(state.alter_value) >= result2.threshold, "threshold should be reached"

    # Run analysis through the service path.
    fresh_rows = await h.db.get("interlude_story", {"id": story.id})
    fresh = type(story).model_validate(fresh_rows[0])
    fresh.state.alter_system = state
    await h.db.update("interlude_story", {"id": story.id},
                      {"state": fresh.state.model_dump(mode="json")})
    await h.service.analyze_alter_system(story.id, "user-message")
    final = await h.service.get_story(story.id)
    alter = final.state.alter_system
    assert alter.emotional_offset is not None
    assert alter.emotional_offset.direction == "serious"
    assert alter.alter_value == 0
    assert alter.alter_weight == 1.0


async def test_29_alter_decay_and_expiry(harness):
    """反向变化按 oppositeDecay 衰减权重，低于 minWeight 清除 offset。"""
    from hdsi.alter import advance_alter_system
    from hdsi.types import EmotionalOffset, iso as _iso

    h = harness
    cfg = h.service._alter_config_dict()
    now = h.clock.now()
    state = advance_alter_system(None, -5, "user-message", now, cfg).state
    offset_state = state.model_copy(deep=True)
    from hdsi.types import EmotionalOffset

    offset_state.emotional_offset = EmotionalOffset(
        direction="relaxed", description="轻松氛围", intensity=1.0,
        generated_at=_iso(now),
    )
    offset_state.alter_weight = 1.0
    offset_state.last_trigger_direction = -1
    # Opposite direction (+) decays weight.
    turned = advance_alter_system(offset_state, +5, "user-message", now, cfg)
    assert turned.state.alter_weight < 1.0
    # Drive below minWeight repeatedly to clear.
    current = turned.state
    for _ in range(12):
        current = advance_alter_system(current, +5, "user-message", now, cfg).state
    assert current.emotional_offset is None
    assert current.alter_weight == 0


async def test_30_compaction_preserves_causality_and_updates_scene(harness):
    """压缩推进场景 lastEntryId 检查点，摘要落库且原始条目保留。"""
    from hdsi.prompt_builder import compaction_prompt
    from hdsi.types import CompactionDecision, ScriptEntryDraft, iso

    h = harness
    story = await h.setup_story()
    entries = []
    for i in range(3):
        entries.append(await h.service.append_entry(story.id, ScriptEntryDraft(
            kind="script", actor="narrator", content=f"事件{i}：因为下雨所以取消了远行。",
            occurred_at=iso(h.clock.now()),
        ), h.clock.now()))
        h.clock.advance(600)

    scene = await h.service.active_scene(story.id)
    decision = CompactionDecision.model_validate({
        "scene": {"hook": "雨天的计划变动", "summary": "因下雨取消远行，改为在家读书。"},
        "facts": [{"scope": "event", "content": "远行因下雨取消",
                   "source_entry_ids": [entries[0].id], "importance": 0.7, "confidence": 0.8}],
    })
    await h.service.persist_compaction(story, scene, decision, entries, h.clock.now())
    fresh_scene = await h.service.active_scene(story.id)
    assert fresh_scene.summary and "远行" in fresh_scene.summary
    assert fresh_scene.last_entry_id == entries[-1].id
    # raw entries preserved (auditability / source of truth)
    still_there = await h.service.recent_entries(story.id, 10)
    assert len([e for e in still_there if e.kind == "script"]) >= 3
    facts = await h.db.get("interlude_fact", {"story_id": story.id})
    assert any("取消" in f["content"] for f in facts)


async def test_31_fact_provenance_sources_and_dedup(harness):
    """事实保留来源条目（可审计），重复内容合并而非重复入库。"""
    from hdsi.types import FactDraft, ScriptEntryDraft, iso

    h = harness
    story = await h.setup_story()
    e1 = await h.service.append_entry(story.id, ScriptEntryDraft(
        kind="script", actor="narrator", content="她说她下周要去北京出差。",
        occurred_at=iso(h.clock.now()),
    ), h.clock.now())
    draft = FactDraft(scope="promise", content="用户下周去北京出差。",
                      source_entry_ids=[e1.id], importance=0.8, confidence=0.9,
                      unresolved=True)
    await h.service.persist_fact(story.id, draft, [e1], h.clock.now())
    await h.service.persist_fact(story.id, draft.model_copy(), [e1], h.clock.now())
    facts = await h.db.get("interlude_fact", {"story_id": story.id})
    assert len(facts) == 1, f"duplicate fact merged expected, got {len(facts)}"
    row = facts[0]
    sources = row["source_entry_ids"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    assert e1.id in sources, "fact provenance must keep its source entry id"
    assert bool(row["unresolved"]), "promise defaults to unresolved"


# ------------------------------------------------------------------ helpers

def iso(dt):
    from hdsi.types import iso as _iso

    return _iso(dt)


def _iso(value):
    from hdsi.types import iso as _i

    return _i(value)
