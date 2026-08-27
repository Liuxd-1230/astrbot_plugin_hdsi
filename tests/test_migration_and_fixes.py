"""Tests for migration async fixes, story_id rewrites, JSON row preparation, and cross-platform fallbacks."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from hdsi.database.connection import Database, _prepare_row
from hdsi.migration import convert_koishi_row, import_koishi_database, rewrite_story_id
from hdsi.service import IncomingEvent, account_enabled, normalize_account_id, normalize_group_id


def test_rewrite_story_id():
    assert rewrite_story_id("onebot:123456:789012") == "character:onebot:123456"
    assert rewrite_story_id("character:onebot:123456") == "character:onebot:123456"
    assert rewrite_story_id("custom_id") == "custom_id"


def test_convert_koishi_row_child_tables():
    entry_row = {
        "id": 1,
        "storyId": "onebot:123456:789012",
        "participantId": "onebot:123456:789012",
        "kind": "script",
        "actor": "narrator",
        "content": "测试内容",
        "occurredAt": "2026-08-27T00:00:00Z",
        "metadata": '{"key": "val"}',
        "createdAt": "2026-08-27T00:00:00Z",
    }
    converted = convert_koishi_row("interlude_script_entry", entry_row)
    assert converted is not None
    assert converted["story_id"] == "character:onebot:123456"
    assert converted["metadata"] == {"key": "val"}


def test_prepare_row_json_defaults():
    story_row = _prepare_row("interlude_story", {"id": "test", "setting": None, "state": None})
    assert story_row["setting"] == "{}"
    assert story_row["state"] == "{}"

    fact_row = _prepare_row("interlude_fact", {"id": 1, "source_entry_ids": None, "embedding": None})
    assert fact_row["source_entry_ids"] == "[]"
    assert fact_row["embedding"] is None


def test_account_enabled_logic():
    assert account_enabled([], "123456") is True
    assert account_enabled(None, "webchat") is True

    from hdsi.config import AccessRule
    rules = [AccessRule(id="webchat:10000", enabled=True)]
    assert account_enabled(rules, "10000") is True
    assert account_enabled(rules, "webchat:10000") is True
    assert account_enabled(rules, "20000") is False


@pytest.mark.asyncio
async def test_import_koishi_database_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "koishi.db"
        dst_path = Path(tmp) / "target.db"

        src_conn = sqlite3.connect(src_path)
        src_conn.execute("""
            CREATE TABLE interlude_story (
                id TEXT PRIMARY KEY, platform TEXT, selfId TEXT, userId TEXT,
                channelId TEXT, status TEXT, setting TEXT, state TEXT,
                cursorAt TEXT, createdAt TEXT, updatedAt TEXT
            )
        """)
        src_conn.execute("""
            CREATE TABLE interlude_script_entry (
                id INTEGER PRIMARY KEY, storyId TEXT, participantId TEXT,
                kind TEXT, actor TEXT, content TEXT, occurredAt TEXT,
                metadata TEXT, createdAt TEXT
            )
        """)
        src_conn.execute("""
            INSERT INTO interlude_story VALUES (
                'onebot:10001:20002', 'onebot', '10001', '20002', '', 'active',
                '{}', '{}', '2026-08-27T00:00:00Z', '2026-08-27T00:00:00Z', '2026-08-27T00:00:00Z'
            )
        """)
        src_conn.execute("""
            INSERT INTO interlude_script_entry VALUES (
                1, 'onebot:10001:20002', 'onebot:10001:20002', 'script', 'narrator',
                '测试剧本', '2026-08-27T00:00:00Z', '{}', '2026-08-27T00:00:00Z'
            )
        """)
        src_conn.commit()
        src_conn.close()

        target_db = Database(dst_path)
        await target_db.connect()
        try:
            counts = await import_koishi_database(src_path, target_db)
            assert counts.get("interlude_story") == 1
            assert counts.get("interlude_script_entry") == 1

            stories = await target_db.get("interlude_story", {})
            assert len(stories) == 1
            assert stories[0]["id"] == "character:onebot:10001"

            entries = await target_db.get("interlude_script_entry", {})
            assert len(entries) == 1
            assert entries[0]["story_id"] == "character:onebot:10001"
            assert entries[0]["content"] == "测试剧本"
        finally:
            await target_db.close()


@pytest.mark.asyncio
async def test_find_story_for_event_single_active_fallback(harness):
    h = harness
    story = await h.setup_story()
    assert story.platform_id == "aiocqhttp"

    event_other = IncomingEvent(
        platform_id="webchat", self_id="webchat", sender_id="user99",
        sender_name="User99", umo="webchat:FriendMessage:user99",
        message_type="FriendMessage", content="你好",
    )
    found = await h.service.find_story_for_event(event_other)
    assert found is not None
    assert found.id == story.id


@pytest.mark.asyncio
async def test_sync_story_setting_from_defaults(harness):
    h = harness
    story = await h.setup_story()
    assert story.setting.character.name == "Unnamed character"

    # User modifies story_defaults in config to "千"
    h.config.story_defaults.character_name = "千"
    h.config.story_defaults.world = "新世界设定"
    await h.service.sync_story_setting_from_defaults()

    updated = await h.service.get_story(story.id)
    assert updated.setting.character.name == "千"
    assert updated.setting.world == "新世界设定"


@pytest.mark.asyncio
async def test_create_story_updates_name_if_existing(harness):
    h = harness
    story = await h.setup_story()
    assert story.setting.character.name == "Unnamed character"

    # User runs hdsi init 千
    event = h.event(content="hdsi init 千")
    re_init = await h.service.create_story(event, name="千")
    assert re_init.id == story.id
    assert re_init.setting.character.name == "千"

    refetched = await h.service.get_story(story.id)
    assert refetched.setting.character.name == "千"

