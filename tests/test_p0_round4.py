"""Round-4 P0 regression guards: group outbox two-phase + split-at-stage."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import wait_for, wait_for_async


pytestmark = pytest.mark.asyncio


def _iso_now(harness):
    from hdsi.types import iso
    return iso(harness.clock.now())


class GroupSenderRecorder:
    """Records group sends; supports partial failure."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []  # (channel_id, content)
        self.fail_next = 0
        self.fail_content_match: str | None = None

    async def __call__(self, story, channel_id: str, content: str) -> bool:
        if self.fail_next > 0:
            self.fail_next -= 1
            return False
        if self.fail_content_match and self.fail_content_match in content:
            self.fail_content_match = None
            return False
        self.sent.append((channel_id, content))
        return True

    def texts(self):
        return [c for _, c in self.sent]


async def _stage_group(harness, story_id, content, **kw):
    return await harness.service.stage_outbound_message(
        story_id, "", content, harness.clock.now(),
        intent_type="outbound-group-message",
        extra_payload={"groupId": kw.get("group_id", "g1"),
                       "channelId": kw.get("channel_id", "g1")},
    )


async def _make_intent(harness, story_id, intent_id):
    rows = await harness.db.get("interlude_intent", {"id": intent_id})
    from hdsi.types import NarrativeIntent
    return NarrativeIntent.model_validate(rows[0])


async def test_r4_1_group_transport_success_finalize_failure_never_resends(harness):
    """群聊发送成功 + finalize 失败 → 绝不重发。"""
    h = harness
    story = await h.setup_story()

    sender = GroupSenderRecorder()
    h.service.group_sender = sender

    finalize_calls = {"n": 0}
    original = h.service.finalize_group_delivery_transaction

    async def failing_finalize(*args, **kwargs):
        finalize_calls["n"] += 1
        if finalize_calls["n"] <= 3:
            raise RuntimeError("injected group finalize failure")
        return await original(*args, **kwargs)

    h.service.finalize_group_delivery_transaction = failing_finalize

    intent_id = await _stage_group(h, story.id, "GROUP_ONCE")
    intent = await _make_intent(h, story.id, intent_id)
    await h.service._deliver_group_outbound(story, intent, h.clock.now())

    # Transport succeeded exactly once.
    assert sender.texts() == ["GROUP_ONCE"]

    # Wait for inline retries to exhaust.
    await asyncio.sleep(1.0)
    assert sender.texts().count("GROUP_ONCE") == 1, \
        f"finalize failure must never resend; got {len(sender.texts())}"

    # Manual retry-finalize succeeds → spoken fact appears once.
    h.service.finalize_group_delivery_transaction = original
    result = await original(
        story.id, "g1", "g1", "GROUP_ONCE", intent_id, h.clock.now())
    assert result is True

    entries = await h.db.fetch_all(
        "SELECT * FROM interlude_script_entry "
        "WHERE kind='character-group-message' AND content='GROUP_ONCE'")
    assert len(entries) == 1


async def test_r4_2_group_finalize_atomic_and_idempotent(harness):
    """事务化群聊 finalize：原子 + 幂等。"""
    h = harness
    story = await h.setup_story()

    intent_id = await _stage_group(h, story.id, "GROUP_IDEM")

    r1 = await h.service.finalize_group_delivery_transaction(
        story.id, "g1", "g1", "GROUP_IDEM", intent_id, h.clock.now())
    assert r1 is True

    r2 = await h.service.finalize_group_delivery_transaction(
        story.id, "g1", "g1", "GROUP_IDEM", intent_id, h.clock.now())
    assert r2 is False, "second finalize must be a no-op"

    entries = await h.db.fetch_all(
        "SELECT * FROM interlude_script_entry WHERE delivery_intent_id=?",
        (intent_id,))
    assert len(entries) == 1

    # Atomicity: non-retryable failure → no partial state.
    h.db.fail_all_writes = True
    with pytest.raises(Exception):
        await h.service.finalize_group_delivery_transaction(
            story.id, "g1", "g1", "SHOULD_NOT_EXIST", None, h.clock.now())
    ghosts = await h.db.fetch_all(
        "SELECT * FROM interlude_script_entry WHERE content='SHOULD_NOT_EXIST'")
    assert not ghosts, "atomic rollback must prevent partial entry"
    h.db.fail_all_writes = False


async def test_r4_3_group_split_partial_transport_never_duplicates(harness):
    """ONE<sep/>TWO 拆为两个 intent，每个恰好一次平台发送；部分失败不重复。"""
    h = harness
    story = await h.setup_story()
    sender = GroupSenderRecorder()
    h.service.group_sender = sender

    # Stage two segments as separate intents (as split-at-stage would).
    id_one = await _stage_group(h, story.id, "ONE", group_id="g1")
    id_two = await _stage_group(h, story.id, "TWO", group_id="g1")

    # Deliver ONE successfully.
    intent1 = await _make_intent(h, story.id, id_one)
    ok1 = await h.service._deliver_group_outbound(story, intent1, h.clock.now())
    assert ok1
    assert sender.texts() == ["ONE"]

    # TWO's transport partially fails then retries — ONE is never re-sent.
    sender.fail_content_match = "TWO"
    intent2 = await _make_intent(h, story.id, id_two)
    ok2 = await h.service._deliver_group_outbound(story, intent2, h.clock.now())
    assert not ok2
    assert sender.texts() == ["ONE"], f"ONE duplicated: {sender.texts()}"

    # Retry TWO only.
    ok3 = await h.service._deliver_group_outbound(story, intent2, h.clock.now())
    assert ok3
    assert sender.texts() == ["ONE", "TWO"], \
        f"expected ['ONE','TWO'], got {sender.texts()}"

    # DB: exactly one spoken fact per segment.
    for content in ("ONE", "TWO"):
        entries = await h.db.fetch_all(
            "SELECT * FROM interlude_script_entry "
            "WHERE kind='character-group-message' AND content=?", (content,))
        assert len(entries) == 1, f"{content} appeared {len(entries)} times"


async def test_r4_4_v1_to_v2_migration_preserves_data(tmp_path):
    """v1 SQLite（无 delivery_intent_id 列）→ connect → 自动升级 → 数据不丢。"""
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "v1.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("CREATE TABLE hdsi_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO hdsi_meta VALUES ('schema_version', '1')")
    conn.execute("""CREATE TABLE interlude_script_entry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_id TEXT NOT NULL,
        participant_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'life',
        actor TEXT NOT NULL DEFAULT 'character',
        content TEXT NOT NULL DEFAULT '',
        occurred_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )""")
    conn.execute(
        "INSERT INTO interlude_script_entry "
        "(story_id, participant_id, kind, actor, content, occurred_at, metadata, created_at) "
        "VALUES ('s1', '', 'script', 'narrator', '旧数据', '2026-01-01T00:00:00Z', '{}', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Open with our Database → auto-migrates to v2.
    from hdsi.database.connection import Database

    db = Database(db_path)
    await db.connect()
    try:
        cols = {r["name"] for r in await db.fetch_all(
            "PRAGMA table_info(interlude_script_entry)")}
        assert "delivery_intent_id" in cols

        idx = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='uq_entry_delivery_intent'")
        assert idx is not None

        old_rows = await db.get("interlude_script_entry", {"story_id": "s1"})
        assert len(old_rows) == 1
        assert old_rows[0]["content"] == "旧数据"
        assert old_rows[0]["delivery_intent_id"] is None

        ver = await db.fetch_one("SELECT value FROM hdsi_meta WHERE key='schema_version'")
        assert int(ver["value"]) >= 2
    finally:
        await db.close()
