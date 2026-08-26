"""Round-2 P0 regression guards (audit #2). All must stay green."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


def _iso_now(harness):
    from hdsi.types import iso
    return iso(harness.clock.now())


async def _char_entries(harness, story_id):
    return [e for e in await harness.service.recent_entries(story_id, 80)
            if e.kind == "character-message"]


class BlockingSender:
    """Sender whose first call blocks until released."""

    def __init__(self, inner):
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_first = True

    async def __call__(self, story, participant, content):
        if self.block_first and not self.started.is_set():
            self.started.set()
            await self.release.wait()
        return await self.inner(story, participant, content)


async def test_p0_a_split_segment_single_spoken_fact(harness):
    """P0-A: ONE<sep/>TWO → 平台两条、数据库恰好两条 spoken fact。"""
    h = harness
    story = await h.setup_story()
    h.narrator.enqueue({
        "script": "她打字。",
        "interaction": {"seen": True, "reply": {
            "mode": "immediate", "content": "ONE<sep/>TWO"}},
    })
    assert await h.service.receive(h.event(content="说两句"))
    ok = await wait_for(lambda: len(h.sender.texts()) >= 1)
    assert ok, "first bubble must deliver immediately"

    # Typing simulation is REAL asyncio.sleep against a VIRTUAL clock; drive
    # deterministically by advancing sim time past the segment delay.
    h.clock.advance(30)
    await h.service.deliver_due_split_segments(story.id)
    ok = await wait_for(lambda: len(h.sender.texts()) >= 2)
    assert ok

    async def all_transport_completed() -> bool:
        rows = await h.db.get(
            "interlude_intent",
            {"story_id": story.id,
             "type": {"$in": ["outbound-message", "split-message"]}})
        return bool(rows) and all(o["status"] == "completed" for o in rows)

    await wait_for_async(all_transport_completed)
    entries = await _char_entries(h, story.id)
    contents = [e.content for e in entries]
    assert contents.count("TWO") == 1, f"duplicate spoken fact: {contents}"
    assert contents.count("ONE") == 1
    assert len(contents) == 2


async def test_p0_a2_recovered_outbound_single_spoken_fact(harness):
    """重启恢复路径：pending outbound 投递一次只写一条 spoken fact。"""
    h = harness
    story = await h.setup_story()
    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]
    from datetime import timedelta

    from datetime import timedelta
    from hdsi.types import iso as _iso

    past = h.clock.now() - timedelta(seconds=1)
    past_iso = _iso(past)
    await h.db.insert("interlude_intent", {
        "story_id": story.id, "participant_id": pid,
        "type": "outbound-message", "summary": "staged before restart",
        "not_before": past_iso,
        "status": "pending",
        "payload": {"content": "RECOVER_ME", "visibleMessage": True,
                    "snapshotUnread": 0, "snapshotPending": 0},
        "created_at": past_iso, "updated_at": past_iso,
    })
    h.narrator.enqueue({
        "script": "她补发那条没送出的消息。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "RECOVER_ME"}},
    })
    await h.service.sweep()
    await wait_for(lambda: "RECOVER_ME" in h.sender.texts())
    await asyncio.sleep(0.15)
    assert h.sender.texts().count("RECOVER_ME") == 1
    entries = [e.content for e in await _char_entries(h, story.id)]
    assert entries.count("RECOVER_ME") == 1, f"duplicate spoken fact: {entries}"


async def test_p0_b_insert_returning_id_commit_failure_atomic(harness):
    """P0-B: COMMIT 失败后重试不得产生第二行。"""
    h = harness
    story = await h.setup_story()
    h.db.fail_next_commit = 1
    entry = await h.service.append_entry(story.id, {
        "kind": "admin-note", "actor": "system",
        "content": "ONCE", "occurred_at": _iso_now(h),
    }, h.clock.now())
    rows = await h.db.get("interlude_script_entry", {"story_id": story.id})
    matches = [r for r in rows if r["content"] == "ONCE"]
    assert len(matches) == 1, f"commit-failure retry duplicated the row: {len(matches)}"
    assert matches[0]["id"] == entry.id, \
        f"returned id {entry.id} != persisted row id {matches[0]['id']}"


async def test_p0_c_private_memory_never_promotes_to_global_continuity(harness):
    """P0-C: A 私有 memory 不进入 advance prompt，也就无法被提升为 Global。"""
    from hdsi.prompt_builder import build_prompt_payload

    h = harness
    ev_a = h.event(sender_id="20001")
    story = await h.setup_story(ev_a)
    pa = await h.service.ensure_participant(story, ev_a)
    secret = "PRIVATE_MEMORY_SECRET_66291"

    from hdsi.types import MemoryDraft
    await h.service.append_memory(story.id, MemoryDraft(
        category="fact", content=secret, importance=0.9), h.clock.now(), pa.id)

    fresh = await h.service.get_story(story.id)
    request = await h.service._build_request(
        fresh, None, "advance", fresh.cursor_at, h.clock.now(),
        None, [], [],
    )
    payload = build_prompt_payload(request)
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    assert secret not in blob, (
        "advance 上下文读到了参与者私有 memory，可被提升为 Global Continuity"
    )

    # 对照组：B 的正常回合能看到自己的（此处为空）与全局，但同样看不到 A 的。
    ev_b = h.event(sender_id="20002")
    pb = await h.service.ensure_participant(story, ev_b)
    request_b = await h.service._build_request(
        fresh, pb, "user-message", fresh.cursor_at, h.clock.now(),
        "hi", [], [],
    )
    blob_b = json.dumps(build_prompt_payload(request_b), ensure_ascii=False, default=str)
    assert secret not in blob_b


async def test_p0_d_new_message_during_transport_preserves_new_user_state(harness):
    """P0-D: 传输期间到达的新用户消息不会被旧回复的状态覆盖。"""
    h = harness
    story = await h.setup_story()

    blocker = BlockingSender(h.sender)
    original_sender = h.service.sender
    h.service.sender = blocker

    h.narrator.enqueue({
        "script": "她立刻回复。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "OLD_REPLY"}},
    })
    t1 = h.clock.now()
    assert await h.service.receive(h.event(content="第一条"))
    await wait_for(blocker.started.is_set)

    # Transport is in flight. A new message arrives NOW.
    # NOTE: deliberately NO narrator response is queued for this second
    # message — its turn may run later and would otherwise legitimately
    # clear unread again, masking the invariant under test.
    h.clock.advance(60)
    t2 = h.clock.now()
    assert await h.service.receive(h.event(content="第二条！"))

    blocker.release.set()
    ok = await wait_for(lambda: h.sender.texts().count("OLD_REPLY") == 1)
    assert ok, "transport should complete after release"
    for _ in range(40):
        entries = await _char_entries(h, story.id)
        if any(e.content == "OLD_REPLY" for e in entries):
            break
        await asyncio.sleep(0.05)

    entries = [e.content for e in await _char_entries(h, story.id)]
    assert entries.count("OLD_REPLY") == 1, f"spoken fact wrong: {entries}"

    # No contradictory 'interrupted' system entry for OLD_REPLY.
    cancelled_rows = await h.db.fetch_all(
        "SELECT content FROM interlude_script_entry WHERE story_id=? AND kind='intent-cancelled'",
        (story.id,),
    )
    assert all("OLD_REPLY" not in (r["content"] or "") for r in cancelled_rows), \
        "sending 状态的气泡不应被标记为被打断"

    state_raw = (await h.db.get("interlude_participant", {}))[0]["state"]
    if isinstance(state_raw, str):
        state_raw = json.loads(state_raw)
    last_user = getattr(state_raw, "last_user_message_at", None) or (
        state_raw.get("last_user_message_at") if isinstance(state_raw, dict) else None)
    unread = getattr(state_raw, "unread_message_count", None) if not isinstance(state_raw, dict) \
        else state_raw.get("unread_message_count")
    assert last_user is not None
    from hdsi.types import parse_date

    assert parse_date(last_user) >= t2 - __import__("datetime").timedelta(seconds=1), \
        f"新消息时间戳被旧回复回滚: {last_user}"
    # P0-D core invariant: msg2 survives accounting. Baseline(subtract the
    # answered msg1) must leave exactly ONE outstanding item for msg2 rather
    # than a blanket zero-wipe of both counters.
    pending = getattr(state_raw, "pending_reply_count", None) if not isinstance(state_raw, dict) \
        else state_raw.get("pending_reply_count")
    assert int(pending or 0) >= 1, \
        f"传输期间到达的新消息待回数被旧回复清零: pending={pending}"
    assert int(unread or 0) <= 1, f"unexpected unread growth: {unread}"
    await asyncio.sleep(0.25)  # let stray retries observe a live db before teardown

    h.service.sender = original_sender
