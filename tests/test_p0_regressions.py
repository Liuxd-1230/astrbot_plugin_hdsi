"""P0 regression guards from the independent audit. All must stay green."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import wait_for


pytestmark = pytest.mark.asyncio


def _iso_now(harness):
    from hdsi.types import iso
    return iso(harness.clock.now())


async def _participant_state(harness):
    rows = await harness.db.get("interlude_participant", {})
    payload = rows[0]["state"]
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "last_character_message_at"):
        return {
            "last_character_message_at": payload.last_character_message_at,
            "unread_message_count": payload.unread_message_count,
        }
    return payload


async def test_p0_1_failed_send_never_becomes_spoken_fact(quiet_harness):
    """P0-1: 投递失败 → 无 character-message 条目、无 lastCharacterMessageAt、staged intent 取消。"""
    h = quiet_harness
    story = await h.setup_story()
    h.sender.fail_participants.add("aiocqhttp:10000:20001")
    h.narrator.enqueue({
        "script": "她想回复。",
        "interaction": {"seen": True, "reply": {"mode": "immediate",
                                                "content": "THIS_WAS_NOT_DELIVERED"}},
    })
    assert await h.service.receive(h.event(content="你好"))
    for _ in range(40):
        entries = [e for e in await h.service.recent_entries(story.id, 50)
                   if e.kind == "character-message"]
        outbound = await h.db.get("interlude_intent",
                                  {"story_id": story.id, "type": "outbound-message"})
        if not h.sender.sent and entries == [] and outbound:
            break
        await asyncio.sleep(0.05)

    entries = [e for e in await h.service.recent_entries(story.id, 50)
               if e.kind == "character-message"]
    assert not entries, f"failed send wrote a spoken entry: {[e.content for e in entries]}"

    state = await _participant_state(h)
    assert not state.get("last_character_message_at"), \
        "lastCharacterMessageAt must stay unset on delivery failure"

    outbound = await h.db.get("interlude_intent",
                              {"story_id": story.id, "type": "outbound-message"})
    assert outbound, "delivery must have been staged before transport"
    # Original semantics: a FAILED send is kept for retry (+30s), never
    # completed and never written as spoken fact.
    statuses = {o["status"] for o in outbound}
    assert "completed" not in statuses
    assert statuses <= {"pending", "cancelled"}, \
        f"unexpected staged status after failure: {statuses}"
    retriable = [o for o in outbound if o["status"] == "pending"]
    if retriable:
        from hdsi.types import parse_date

        nb = parse_date(retriable[0]["not_before"])
        assert nb is not None and nb > h.clock.now(), "retry must be deferred"


async def test_p0_1b_successful_send_finalizes_outbox(harness):
    """成功投递 → character-message 条目 + participant 记录 + intent completed。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她想回复。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "收到啦"}},
    })
    assert await h.service.receive(h.event(content="在吗"))
    ok = await wait_for(lambda: len(h.sender.sent) >= 1)
    assert ok
    entries = [e for e in await h.service.recent_entries(story.id, 50)
               if e.kind == "character-message"]
    assert entries and entries[-1].content == "收到啦"
    outbound = await h.db.get("interlude_intent",
                              {"story_id": story.id, "type": "outbound-message"})
    assert outbound and all(o["status"] == "completed" for o in outbound)
    state = await _participant_state(h)
    assert state.get("last_character_message_at")


async def test_p0_2_execute_many_is_atomic(harness):
    """P0-2: 批量写中途失败必须整体回滚；后续无关提交不会诈尸。"""
    db = harness.db
    await db.execute("CREATE TABLE IF NOT EXISTS p0_t (k TEXT PRIMARY KEY, v TEXT)")
    statements = [
        ("INSERT INTO p0_t(k, v) VALUES('x', '1')", ()),
        ("THIS IS NOT SQL", ()),
    ]
    with pytest.raises(Exception):
        await db.execute_many(statements)

    await db.execute("INSERT INTO p0_t(k, v) VALUES('y', 'ok')", ())
    rows = {r["k"]: r["v"] for r in await db.fetch_all("SELECT k, v FROM p0_t")}
    assert "y" in rows
    assert "x" not in rows, f"ghost partial commit leaked: {rows}"


async def test_p0_3_generated_ids_belong_to_own_story(harness):
    """P0-3: 并发 append_entry 返回的 id 必须对应自己那条内容。"""
    from hdsi.config import AccessRule

    h = harness
    ev_a = h.event(sender_id="20001")
    ev_b = h.event(sender_id="20002", self_id="99999", platform_id="telegram",
                   umo="telegram:FriendMessage:20002")
    h.config.platform_gate.bot_accounts.append(AccessRule(id="99999"))
    h.config.platform_gate.user_accounts.append(AccessRule(id="20002"))
    story_a = await h.setup_story(ev_a)
    story_b = await h.service.create_story(ev_b)

    async def append_many(story_id, tag, n):
        out = []
        for i in range(n):
            entry = await h.service.append_entry(story_id, {
                "kind": "admin-note", "actor": "system",
                "content": f"{tag}-{i}", "occurred_at": _iso_now(h),
            }, h.clock.now())
            out.append((entry.id, entry.content))
        return out

    results = await asyncio.gather(
        append_many(story_a.id, "A", 12),
        append_many(story_b.id, "B", 12),
    )
    for ids, story_id in ((results[0], story_a.id), (results[1], story_b.id)):
        for entry_id, content in ids:
            rows = await h.db.get("interlude_script_entry", {"id": entry_id})
            assert rows and rows[0]["content"] == content, \
                f"id {entry_id} does not map to own content"
            assert rows[0]["story_id"] == story_id


async def test_p0_4_unscoped_canonical_never_archives_other_identity(harness):
    """P0-4: 无作用域调用不归档其他身份；同身份多活动行仍归档。"""
    from hdsi.config import AccessRule

    h = harness
    ev_a = h.event(sender_id="20001")
    ev_b = h.event(sender_id="20002", self_id="99999", platform_id="telegram",
                   umo="telegram:FriendMessage:20002")
    h.config.platform_gate.bot_accounts.append(AccessRule(id="99999"))
    h.config.platform_gate.user_accounts.append(AccessRule(id="20002"))
    story_a = await h.setup_story(ev_a)
    story_b = await h.service.create_story(ev_b)

    picked = await h.service.get_canonical_story()
    assert picked is not None
    status = {}
    for sid in (story_a.id, story_b.id):
        row = (await h.db.get("interlude_story", {"id": sid}))[0]
        status[sid] = row["status"]
    assert status[story_a.id] == "active" and status[story_b.id] == "active"

    duplicate = dict((await h.db.get("interlude_story", {"id": story_a.id}))[0])
    duplicate["setting"] = dict(duplicate["setting"]) if hasattr(duplicate["setting"], "keys") else duplicate["setting"]
    if hasattr(duplicate["setting"], "model_dump"):
        duplicate["setting"] = duplicate["setting"].model_dump()
    if hasattr(duplicate["state"], "model_dump"):
        duplicate["state"] = duplicate["state"].model_dump()
    duplicate.pop("id")
    await h.db.insert("interlude_story", {**duplicate,
                                          "id": "character:aiocqhttp:10000-dup",
                                          "status": "active"})
    await h.service.get_canonical_story(
        preferred_id=story_a.id, platform_id="aiocqhttp", self_id="10000",
    )
    statuses = {}
    for sid in (story_a.id, "character:aiocqhttp:10000-dup", story_b.id):
        row = (await h.db.get("interlude_story", {"id": sid}))[0]
        statuses[sid] = row["status"]
    assert statuses[story_a.id] == "active"
    assert statuses["character:aiocqhttp:10000-dup"] == "archived"
    assert statuses[story_b.id] == "active"


async def test_p0_5_continuity_privacy_full_payload(harness):
    """P0-5: A 的私有 continuity 绝不出现在 B 的完整 prompt payload。"""
    from hdsi.prompt_builder import build_prompt_payload
    from hdsi.types import ContinuitySnapshot

    h = harness
    ev_a = h.event(sender_id="20001")
    ev_b = h.event(sender_id="20002")
    story = await h.setup_story(ev_a)
    pa = await h.service.ensure_participant(story, ev_a)
    pb = await h.service.ensure_participant(story, ev_b)
    secret = "CONTINUITY_SECRET_93721"

    # Simulate an A-scoped continuity refresh that captured the secret.
    fresh = await h.service.get_story(story.id)
    state = fresh.state.model_copy(update={
        "participant_continuity": {pa.id: ContinuitySnapshot(
            current=f"A刚告诉她{secret}",
            recent=[secret],
            salient=[secret],
        )},
        # A malicious/buggy global write attempt must also not leak:
        "continuity_snapshot": None,
    })
    await h.db.update("interlude_story", {"id": story.id},
                      {"state": state.model_dump(mode="json")})

    now = h.clock.now()
    request = await h.service._build_request(
        await h.service.get_story(story.id), pb, "user-message",
        (await h.service.get_story(story.id)).cursor_at, now,
        "B 的新消息", [], [],
    )
    payload = build_prompt_payload(request)
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    assert secret not in blob, (
        "A 的私有 continuity 泄漏进 B 的完整 prompt：\n"
        + blob[:800]
    )

    # B 自己的 refresh 会写入自己的分支，且 B 能看到自己的内容。
    own_secret = "B_OWN_NOTE_1234"
    state2 = (await h.service.get_story(story.id)).state.model_copy(update={
        "participant_continuity": {
            pa.id: ContinuitySnapshot(current=f"A刚告诉她{secret}"),
            pb.id: ContinuitySnapshot(recent=[own_secret]),
        },
    })
    await h.db.update("interlude_story", {"id": story.id},
                      {"state": state2.model_dump(mode="json")})
    request_b = await h.service._build_request(
        await h.service.get_story(story.id), pb, "user-message",
        now, h.clock.now(), "再聊一句", [], [],
    )
    payload_b = build_prompt_payload(request_b)
    blob_b = json.dumps(payload_b, ensure_ascii=False, default=str)
    assert own_secret in blob_b, "B 应能看到自己的 continuity"
    assert secret not in blob_b


async def test_p0_5b_advance_refresh_writes_global_only(harness):
    """advance 阶段的 continuity 刷新只写全局，不写任何参与者分支。"""
    h = harness
    story = await h.setup_story()
    raw = {
        "script": "一天过去了。",
        "continuity": {"current": "她在准备下周的实验。",
                        "next": ["整理数据"], "recent": [], "salient": []},
    }
    await h.service.persist_decision(
        story, None, raw, story.cursor_at, h.clock.now(),
        permit_messages=False, phase="advance",
    )
    fresh = await h.service.get_story(story.id)
    assert fresh.state.continuity_snapshot is not None
    assert fresh.state.continuity_snapshot.current.startswith("她在准备")
    assert not fresh.state.participant_continuity
