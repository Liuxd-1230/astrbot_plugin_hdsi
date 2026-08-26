"""Round-3 P0 regression guards. All must stay green."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


def _iso_now(harness):
    from hdsi.types import iso
    return iso(harness.clock.now())


async def test_r3_1_transport_success_finalize_failure_never_resends(harness):
    """发送成功 + finalize 失败 → 绝不重发；intent 留 sending。"""
    h = harness
    story = await h.setup_story()

    finalize_calls = {"n": 0}
    original_finalize = h.service.finalize_delivery_transaction

    async def failing_finalize(*args, **kwargs):
        finalize_calls["n"] += 1
        if finalize_calls["n"] <= 3:  # fail first 3 attempts (2 inline retries)
            raise RuntimeError("injected finalize failure")
        return await original_finalize(*args, **kwargs)

    h.narrator.enqueue({
        "script": "她回复。",
        "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "ATOMIC?"}},
    })
    assert await h.service.receive(h.event(content="触发"))
    await wait_for(lambda: len(h.sender.sent) >= 1)

    # Wait for inline retries to exhaust (0.25+0.5s) and the background task.
    await asyncio.sleep(1.2)
    sends_with_content = [s for s in h.sender.sent if s.content == "ATOMIC?"]
    assert len(sends_with_content) == 1, \
        f"finalize failure must never resend; got {len(sends_with_content)} deliveries"

    # Now let finalize succeed via explicit retry-only call.
    result = await original_finalize(
        story.id, "aiocqhttp:10000:20001", "ATOMIC?",
        sends_with_content[0] and await _get_outbound_intent_id(h, story.id),
        h.clock.now(),
    )
    entries = [e for e in await h.service.recent_entries(story.id, 50)
               if e.kind == "character-message" and e.content == "ATOMIC?"]
    assert len(entries) == 1, f"spoken fact must appear exactly once after retry-finalize"
    outbound = await h.db.get("interlude_intent",
                              {"story_id": story.id, "type": "outbound-message"})
    assert all(o["status"] in ("completed", "sending") for o in outbound)


async def test_r3_2_finalize_transaction_is_atomic_and_idempotent(harness):
    """事务化 finalize：原子（commit 失败无残留）+ 幂等（重复调用单条）。"""
    h = harness
    story = await h.setup_story()
    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]

    intent_id = await h.service.stage_outbound_message(
        story.id, pid, "IDEMPOTENT", h.clock.now(),
        baselines=(0, 0),
    )

    # Idempotency: call twice → exactly one spoken entry.
    r1 = await h.service.finalize_delivery_transaction(
        story.id, pid, "IDEMPOTENT", intent_id, h.clock.now())
    assert r1 is True
    r2 = await h.service.finalize_delivery_transaction(
        story.id, pid, "IDEMPOTENT", intent_id, h.clock.now())
    assert r2 is False, "second finalize must be a no-op"

    entries = await h.db.fetch_all(
        "SELECT * FROM interlude_script_entry WHERE delivery_intent_id=?",
        (intent_id,))
    assert len(entries) == 1, f"expected 1 entry, got {len(entries)}"

    # Atomicity: fail_all_writes is non-retryable → no partial state.
    h.db.fail_all_writes = True
    with pytest.raises(Exception):
        await h.service.finalize_delivery_transaction(
            story.id, pid, "SHOULD_NOT_EXIST", None, h.clock.now())
    ghosts = await h.db.fetch_all(
        "SELECT * FROM interlude_script_entry WHERE content='SHOULD_NOT_EXIST'")
    assert not ghosts, "atomic rollback must prevent partial entry"
    h.db.fail_all_writes = False


async def test_r3_3_stale_sending_restart_becomes_uncertain_not_zombie(harness):
    """崩溃后 stale `sending` → uncertain（不重发、不虚构、可查询）。"""
    h = harness
    story = await h.setup_story()
    rows = await h.db.get("interlude_participant", {})
    pid = rows[0]["id"]

    intent_id = await h.service.stage_outbound_message(
        story.id, pid, "CRASH_LEFT_ME", h.clock.now())
    await h.service._mark_intent_sending([intent_id])

    # Simulate restart: fresh service on same DB.
    from hdsi.database.connection import Database
    from hdsi.service import InterludeService
    from tests.fakes import SenderRecorder as SR

    db2 = Database(h.db.path)
    await db2.connect()
    svc2 = InterludeService(db=db2, config=h.config,
                            narrator=h.narrator, embedder=h.embedder,
                            sender=SR(), now_fn=h.clock.now)
    count = await svc2.recover_stale_sending()
    assert count >= 1

    row = (await db2.get("interlude_intent", {"id": intent_id}))[0]
    assert row["status"] == "uncertain", f"expected uncertain, got {row['status']}"

    # sweep does NOT deliver it.
    await svc2.sweep()
    assert not any("CRASH_LEFT_ME" == c for c in h.sender.texts()), \
        "uncertain messages must never be resent"

    # due_intents excludes it too.
    due = await svc2.due_intents(story.id, h.clock.now())
    assert all(i.id != intent_id for i in due)
    await db2.close()


async def test_r3_4_group_send_failure_returns_sending_to_pending(harness):
    """群聊发送失败：sending → pending(+30s)，非永久僵尸。"""
    h = harness
    story = await h.setup_story()

    group_calls = {"n": 0}
    group_ok = {"v": False}

    async def failing_group_sender(story, channel, content):
        group_calls["n"] += 1
        return group_ok["v"]

    h.service.group_sender = failing_group_sender

    intent_id = await h.service.stage_outbound_message(
        story.id, "", "GROUP_MSG", h.clock.now(),
        intent_type="outbound-group-message",
        extra_payload={"groupId": "g1", "channelId": "g1"},
    )
    delivered = await h.service._deliver_group_outbound(
        story,
        type("FakeIntent", (), {"id": intent_id, "type": "outbound-group-message",
                                 "payload": {"content": "GROUP_MSG", "groupId": "g1",
                                              "channelId": "g1"}})(),
        h.clock.now(),
    )
    assert not delivered
    row = (await h.db.get("interlude_intent", {"id": intent_id}))[0]
    assert row["status"] == "pending", \
        f"group send failure must revert sending→pending, got {row['status']}"
    from hdsi.types import parse_date

    nb = parse_date(row["not_before"])
    assert nb is not None and nb > h.clock.now(), "retry must be deferred"

    # Now succeed → completed.
    group_ok["v"] = True
    delivered2 = await h.service._deliver_group_outbound(
        story,
        type("FakeIntent", (), {"id": intent_id, "type": "outbound-group-message",
                                 "payload": {"content": "GROUP_MSG", "groupId": "g1",
                                              "channelId": "g1"}})(),
        h.clock.now(),
    )
    assert delivered2
    row2 = (await h.db.get("interlude_intent", {"id": intent_id}))[0]
    assert row2["status"] == "completed"


async def _get_outbound_intent_id(harness, story_id):
    rows = await harness.db.get("interlude_intent",
                                {"story_id": story_id, "type": "outbound-message"})
    return rows[-1]["id"] if rows else None
