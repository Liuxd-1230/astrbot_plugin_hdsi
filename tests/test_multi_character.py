"""Tests for HDSI Multi-Character Management and Strict Isolation.

Covers:
1. Character Registry bootstrap / migration compatibility
2. Character CRUD (create, update, clone, delete/archive, set_default, export, import)
3. Canon isolation between characters
4. Participants isolation between characters
5. Facts & Memory isolation between characters
6. Intent isolation between characters
7. Conversation Binding routing (exact match, wildcard match, unbound fallback to default)
8. WebUI character-scoped APIs and endpoints
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from hdsi.config import HdsiConfig
from hdsi.database.connection import Database
from hdsi.service import IncomingEvent, InterludeService
from hdsi.types import (
    CharacterRecord,
    CharacterStatus,
    ConversationBinding,
    InterludeStory,
    NarrativeFact,
    NarrativeIntent,
    NarrativeMemory,
    ScriptEntryDraft,
    StorySetting,
    empty_story_state,
    iso,
)
from tests.fakes import SenderRecorder, ScriptedNarrator, SilentEmbedder, VirtualClock


@pytest_asyncio.fixture
async def multi_harness():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_multi.db"
        db = Database(db_path)
        await db.connect()
        clock = VirtualClock()
        config = HdsiConfig()
        service = InterludeService(
            db=db,
            config=config,
            narrator=ScriptedNarrator(),
            embedder=SilentEmbedder(),
            sender=SenderRecorder(),
            now_fn=clock.now,
        )
        yield service, db, clock
        await service.stop_background_tasks()
        await db.close()


@pytest.mark.asyncio
async def test_character_registry_bootstrap_from_existing_story(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    # Pre-populate a legacy single-story without character registry
    legacy_story_id = "character:webchat:webchat"
    setting = service.initial_story_setting("千")
    setting.world = "世界之树下的旧书店"
    story = InterludeStory(
        id=legacy_story_id,
        platform_id="webchat",
        self_id="webchat",
        status="active",
        setting=setting,
        state=empty_story_state(),
        cursor_at=now,
        created_at=now,
        updated_at=now,
    )
    await db.insert("interlude_story", story.model_dump(mode="json"))

    # Verify no characters exist yet
    chars_before = await db.get("interlude_character", {})
    assert len(chars_before) == 0

    # Ensure registry
    chars = await service.ensure_character_registry()
    assert len(chars) == 1
    assert chars[0].id == "default"
    assert chars[0].name == "千"
    assert chars[0].story_id == legacy_story_id
    assert chars[0].is_default is True
    assert chars[0].status == CharacterStatus.ACTIVE

    # Check default character resolution
    default_char = await service.get_default_character()
    assert default_char is not None
    assert default_char.name == "千"
    assert default_char.story_id == legacy_story_id


@pytest.mark.asyncio
async def test_character_crud_operations(multi_harness):
    service, db, clock = multi_harness

    # 1. Create Character A
    char_a = await service.create_character_record(
        name="千",
        description="喜爱安静阅读的书店店员",
        is_default=True,
        canon={"world": "平静的日常小镇", "timezone": "Asia/Shanghai"},
    )
    assert char_a.name == "千"
    assert char_a.is_default is True
    assert char_a.story_id == f"story:{char_a.id}"

    # 2. Create Character B
    char_b = await service.create_character_record(
        name="爱丽丝",
        description="充满好奇心的见习魔女",
        avatar="https://example.com/alice.png",
        is_default=False,
        canon={"world": "魔法与齿轮都市", "timezone": "Europe/London"},
    )
    assert char_b.name == "爱丽丝"
    assert char_b.is_default is False
    assert char_b.avatar == "https://example.com/alice.png"

    # 3. List characters
    chars = await service.list_characters(include_archived=True)
    assert len(chars) == 2

    # 4. Update Character B
    updated_b = await service.update_character_record(
        character_id=char_b.id,
        name="爱丽丝·玛格特洛依德",
        description="人偶使魔女",
        canon={"location": "魔法之森"},
    )
    assert updated_b.name == "爱丽丝·玛格特洛依德"
    assert updated_b.description == "人偶使魔女"
    story_b = await service.get_story(char_b.story_id)
    assert story_b.setting.character.name == "爱丽丝·玛格特洛依德"
    assert story_b.setting.location == "魔法之森"

    # 5. Set default character to B
    ok = await service.set_default_character(char_b.id)
    assert ok is True
    def_char = await service.get_default_character()
    assert def_char.id == char_b.id

    char_a_reloaded = await service.get_character(char_a.id)
    assert char_a_reloaded.is_default is False

    # 6. Clone Character B
    cloned_b = await service.clone_character_record(char_b.id, new_name="爱丽丝 (平行世界)")
    assert cloned_b.name == "爱丽丝 (平行世界)"
    assert cloned_b.id != char_b.id
    assert cloned_b.story_id != char_b.story_id
    cloned_story = await service.get_story(cloned_b.story_id)
    assert cloned_story.setting.character.name == "爱丽丝 (平行世界)"
    assert cloned_story.setting.location == "魔法之森"

    # 7. Export and Import
    exported = await service.export_character_config(char_a.id)
    assert exported["character"]["name"] == "千"
    assert exported["canon"]["world"] == "平静的日常小镇"

    imported = await service.import_character_config(exported, name_override="千 (导入版)")
    assert imported.name == "千 (导入版)"
    assert imported.id != char_a.id
    imported_story = await service.get_story(imported.story_id)
    assert imported_story.setting.world == "平静的日常小镇"

    # 8. Archive & Delete
    await service.delete_or_archive_character(imported.id, purge_data=False)
    archived = await service.get_character(imported.id)
    assert archived.status == CharacterStatus.ARCHIVED

    await service.delete_or_archive_character(cloned_b.id, purge_data=True)
    deleted = await service.get_character(cloned_b.id)
    assert deleted is None
    story_check = await db.get("interlude_story", {"id": cloned_b.story_id})
    assert len(story_check) == 0


@pytest.mark.asyncio
async def test_multi_character_canon_isolation(multi_harness):
    service, db, clock = multi_harness

    char_a = await service.create_character_record(
        name="千",
        canon={"world": "世界A", "relationship": "关系A", "style": "风格A", "timezone": "Asia/Shanghai"},
    )
    char_b = await service.create_character_record(
        name="爱丽丝",
        canon={"world": "世界B", "relationship": "关系B", "style": "风格B", "timezone": "Europe/London"},
    )

    story_a = await service.get_story(char_a.story_id)
    story_b = await service.get_story(char_b.story_id)

    # Verify initial settings are strictly distinct
    assert story_a.setting.character.name == "千"
    assert story_a.setting.world == "世界A"
    assert story_a.setting.timezone == "Asia/Shanghai"

    assert story_b.setting.character.name == "爱丽丝"
    assert story_b.setting.world == "世界B"
    assert story_b.setting.timezone == "Europe/London"

    # Modify Character A
    await service.update_character_record(char_a.id, name="千 (改)", canon={"world": "世界A (演化)"})

    # Character B must be completely unaffected
    story_b_refreshed = await service.get_story(char_b.story_id)
    assert story_b_refreshed.setting.character.name == "爱丽丝"
    assert story_b_refreshed.setting.world == "世界B"


@pytest.mark.asyncio
async def test_multi_character_participants_isolation(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    char_a = await service.create_character_record(name="千")
    char_b = await service.create_character_record(name="爱丽丝")

    story_a = await service.get_story(char_a.story_id)
    story_b = await service.get_story(char_b.story_id)

    # Add participant 1 to Story A
    ev_a = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20001",
        sender_name="Alice_Friend", umo="aiocqhttp:FriendMessage:20001",
        message_type="FriendMessage", content="Hi Qian",
    )
    part_a = await service.ensure_participant(story_a, ev_a, now)

    # Add participant 2 to Story B
    ev_b = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20002",
        sender_name="Bob_Friend", umo="aiocqhttp:FriendMessage:20002",
        message_type="FriendMessage", content="Hi Alice",
    )
    part_b = await service.ensure_participant(story_b, ev_b, now)

    parts_a = await service.participants(story_a.id)
    parts_b = await service.participants(story_b.id)

    assert len(parts_a) == 1
    assert parts_a[0].id == part_a.id
    assert parts_a[0].display_name == "Alice_Friend"

    assert len(parts_b) == 1
    assert parts_b[0].id == part_b.id
    assert parts_b[0].display_name == "Bob_Friend"


@pytest.mark.asyncio
async def test_multi_character_memory_and_facts_isolation(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    char_a = await service.create_character_record(name="千")
    char_b = await service.create_character_record(name="爱丽丝")

    # Insert Fact and Memory for Character A
    await db.insert("interlude_fact", {
        "story_id": char_a.story_id,
        "participant_id": "",
        "scope": "character",
        "content": "千喜欢黑咖啡",
        "importance": 0.8,
        "confidence": 0.9,
        "unresolved": 0,
        "last_seen_at": iso(now),
        "created_at": iso(now),
        "updated_at": iso(now),
    })
    await db.insert("interlude_memory", {
        "story_id": char_a.story_id,
        "participant_id": "",
        "category": "habit",
        "content": "千每天早上八点开店",
        "importance": 0.7,
        "status": "active",
        "created_at": iso(now),
        "updated_at": iso(now),
    })

    # Insert Fact and Memory for Character B
    await db.insert("interlude_fact", {
        "story_id": char_b.story_id,
        "participant_id": "",
        "scope": "world",
        "content": "魔法之森常年起雾",
        "importance": 0.9,
        "confidence": 0.95,
        "unresolved": 0,
        "last_seen_at": iso(now),
        "created_at": iso(now),
        "updated_at": iso(now),
    })

    # Query for Character A
    facts_a = await db.get("interlude_fact", {"story_id": char_a.story_id})
    mems_a = await db.get("interlude_memory", {"story_id": char_a.story_id})
    assert len(facts_a) == 1
    assert facts_a[0]["content"] == "千喜欢黑咖啡"
    assert len(mems_a) == 1
    assert mems_a[0]["content"] == "千每天早上八点开店"

    # Query for Character B
    facts_b = await db.get("interlude_fact", {"story_id": char_b.story_id})
    mems_b = await db.get("interlude_memory", {"story_id": char_b.story_id})
    assert len(facts_b) == 1
    assert facts_b[0]["content"] == "魔法之森常年起雾"
    assert len(mems_b) == 0  # Character B has 0 memories


@pytest.mark.asyncio
async def test_multi_character_intents_isolation(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    char_a = await service.create_character_record(name="千")
    char_b = await service.create_character_record(name="爱丽丝")

    await db.insert("interlude_intent", {
        "story_id": char_a.story_id,
        "participant_id": "p1",
        "type": "follow-up",
        "summary": "询问千的新书进度",
        "not_before": iso(now),
        "status": "pending",
        "payload": "{}",
        "created_at": iso(now),
        "updated_at": iso(now),
    })

    intents_a = await db.get("interlude_intent", {"story_id": char_a.story_id})
    intents_b = await db.get("interlude_intent", {"story_id": char_b.story_id})

    assert len(intents_a) == 1
    assert intents_a[0]["summary"] == "询问千的新书进度"
    assert len(intents_b) == 0


@pytest.mark.asyncio
async def test_conversation_binding_and_message_routing(multi_harness):
    service, db, clock = multi_harness

    # Create two characters: Qian (default) and Alice
    char_qian = await service.create_character_record(name="千", is_default=True)
    char_alice = await service.create_character_record(name="爱丽丝", is_default=False)

    # 1. Unbound message should route to Default Character (Qian)
    ev_unbound = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20001",
        group_id="", message_type="FriendMessage", content="Hello",
    )
    story1 = await service.find_story_for_event(ev_unbound)
    assert story1 is not None
    assert story1.id == char_qian.story_id
    assert story1.setting.character.name == "千"

    # 2. Bind QQ Group 88888 to Alice
    binding = await service.set_conversation_binding(
        platform_id="aiocqhttp", self_id="10000", conversation_id="88888", character_id=char_alice.id
    )
    assert binding.character_id == char_alice.id

    # Group message from 88888 should route to Alice
    ev_group_alice = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20002",
        group_id="88888", message_type="GroupMessage", content="Hi Group Alice",
    )
    story2 = await service.find_story_for_event(ev_group_alice)
    assert story2 is not None
    assert story2.id == char_alice.story_id
    assert story2.setting.character.name == "爱丽丝"

    # Unbound Group 99999 should route to Default (Qian)
    ev_group_other = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20002",
        group_id="99999", message_type="GroupMessage", content="Hi Group Other",
    )
    story3 = await service.find_story_for_event(ev_group_other)
    assert story3 is not None
    assert story3.id == char_qian.story_id

    # 3. Wildcard binding: bind all WebChat conversations to Alice
    await service.set_conversation_binding(
        platform_id="webchat", self_id="webchat", conversation_id="*", character_id=char_alice.id
    )
    ev_webchat = IncomingEvent(
        platform_id="webchat", self_id="webchat", sender_id="random_user_1",
        group_id="", message_type="FriendMessage", content="Webchat message",
    )
    story4 = await service.find_story_for_event(ev_webchat)
    assert story4 is not None
    assert story4.id == char_alice.story_id

    # 4. Unbind group 88888 -> should fall back to default (Qian)
    deleted = await service.delete_conversation_binding("aiocqhttp", "10000", "88888")
    assert deleted is True
    story5 = await service.find_story_for_event(ev_group_alice)
    assert story5 is not None
    assert story5.id == char_qian.story_id


@pytest.mark.asyncio
async def test_webui_apis_character_scoping(multi_harness):
    service, db, clock = multi_harness

    char_qian = await service.create_character_record(name="千", is_default=True)
    char_alice = await service.create_character_record(name="爱丽丝", is_default=False)

    # Insert a script entry for Qian and Alice
    await service.append_entry(char_qian.story_id, ScriptEntryDraft(
        kind="life", actor="character", content="千在书店整理新到的诗集。",
        occurred_at=iso(clock.now()), metadata={},
    ), clock.now())

    await service.append_entry(char_alice.story_id, ScriptEntryDraft(
        kind="life", actor="character", content="爱丽丝正在调制魔法药水。",
        occurred_at=iso(clock.now()), metadata={},
    ), clock.now())

    # Check latest_active_story scoping
    story_q = await service.latest_active_story(char_qian.id)
    story_a = await service.latest_active_story(char_alice.id)
    assert story_q.setting.character.name == "千"
    assert story_a.setting.character.name == "爱丽丝"

    # Check script entries scoping
    script_q = await service.recent_entries(char_qian.story_id, 10)
    script_a = await service.recent_entries(char_alice.story_id, 10)
    assert any("诗集" in e.content for e in script_q)
    assert not any("诗集" in e.content for e in script_a)
    assert any("魔法药水" in e.content for e in script_a)
    assert not any("魔法药水" in e.content for e in script_q)


@pytest.mark.asyncio
async def test_participant_lifecycle_management(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    char = await service.create_character_record(name="千", is_default=True)
    story = await service.get_story(char.story_id)

    ev = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20001",
        sender_name="Alice", message_type="FriendMessage", content="Hello",
    )
    part = await service.ensure_participant(story, ev, now)
    assert part.display_name == "Alice"
    assert part.status == "active"

    # Update participant
    await db.update("interlude_participant", {"id": part.id}, {
        "display_name": "Alice Cooper",
        "relationship": "多年好友",
        "status": "paused",
        "updated_at": iso(now),
    })
    reloaded = await service.find_participant_for_event(ev, story)
    assert reloaded.display_name == "Alice Cooper"
    assert reloaded.relationship == "多年好友"
    assert reloaded.status == "paused"

    # Reset participant state
    from hdsi.types import empty_participant_state
    await db.update("interlude_participant", {"id": part.id}, {
        "state": empty_participant_state().model_dump(mode="json"),
        "updated_at": iso(now),
    })
    reset_part = await service.find_participant_for_event(ev, story)
    assert reset_part.state.unread_message_count == 0

    # Delete participant
    await db.execute("DELETE FROM interlude_participant WHERE id=?", (part.id,))
    deleted = await db.get("interlude_participant", {"id": part.id})
    assert len(deleted) == 0


@pytest.mark.asyncio
async def test_saving_character_b_canon_never_mutates_global_defaults(multi_harness):
    service, db, clock = multi_harness

    # Initial global story_defaults in config
    service.config.story_defaults.character_name = "千"
    service.config.story_defaults.world = "世界之树下的旧书店"

    # Create Character Alice
    char_alice = await service.create_character_record(
        name="爱丽丝",
        canon={"world": "魔界", "relationship": "魔女与使魔", "timezone": "UTC"},
    )

    # Alice story has its own setting
    story_alice = await service.get_story(char_alice.story_id)
    assert story_alice.setting.character.name == "爱丽丝"
    assert story_alice.setting.world == "魔界"

    # Global config's story_defaults MUST remain unchanged
    assert service.config.story_defaults.character_name == "千"
    assert service.config.story_defaults.world == "世界之树下的旧书店"

    # Mutate Alice's setting directly
    story_alice.setting.character.name = "爱丽丝·二世"
    story_alice.setting.world = "魔界地下城"
    await db.update("interlude_story", {"id": story_alice.id}, {
        "setting": story_alice.setting.model_dump(mode="json"),
    })

    # Global defaults are still "千"
    assert service.config.story_defaults.character_name == "千"
    assert service.config.story_defaults.world == "世界之树下的旧书店"


@pytest.mark.asyncio
async def test_bound_paused_character_never_falls_back_to_default(multi_harness):
    """Authoritative termination: if an explicit binding matches a character whose story
    is paused/inactive, find_story_for_event MUST return None instead of falling back to default character.
    """
    service, db, clock = multi_harness

    char_qian = await service.create_character_record(name="千", is_default=True)
    char_alice = await service.create_character_record(name="爱丽丝", is_default=False)

    # Bind QQ Group 12345 to Alice
    await service.set_conversation_binding(
        platform_id="aiocqhttp", self_id="10000", conversation_id="12345",
        character_id=char_alice.id, conversation_type="group",
    )

    # When Alice is active, group 12345 routes to Alice
    ev = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="999",
        group_id="12345", message_type="GroupMessage", content="Hello Alice",
    )
    s = await service.find_story_for_event(ev)
    assert s is not None
    assert s.id == char_alice.story_id

    # Archive / Pause Alice
    await service.delete_or_archive_character(char_alice.id, purge_data=False)
    char_alice_reloaded = await service.get_character(char_alice.id)
    assert char_alice_reloaded.status == CharacterStatus.ARCHIVED

    # Re-bind for test to simulate explicit binding to an archived/paused character
    await service.set_conversation_binding(
        platform_id="aiocqhttp", self_id="10000", conversation_id="12345",
        character_id=char_alice.id, conversation_type="group",
    )

    # Routing MUST terminate and return None — ABSOLUTELY NO fallback to Qian!
    s_paused = await service.find_story_for_event(ev)
    assert s_paused is None, "Authoritative binding MUST terminate on unavailable target, not fallback to Qian!"


@pytest.mark.asyncio
async def test_friend_and_group_same_numeric_id_route_independently(multi_harness):
    """ConversationBinding distinguishes conversation_type ('friend' vs 'group')."""
    service, db, clock = multi_harness

    char_qian = await service.create_character_record(name="千", is_default=True)
    char_alice = await service.create_character_record(name="爱丽丝", is_default=False)
    char_bob = await service.create_character_record(name="鲍勃", is_default=False)

    # Bind Friend 55555 to Alice
    await service.set_conversation_binding(
        platform_id="aiocqhttp", self_id="10000", conversation_id="55555",
        character_id=char_alice.id, conversation_type="friend",
    )

    # Bind Group 55555 to Bob
    await service.set_conversation_binding(
        platform_id="aiocqhttp", self_id="10000", conversation_id="55555",
        character_id=char_bob.id, conversation_type="group",
    )

    # Friend message with ID 55555 -> routes to Alice
    ev_friend = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="55555",
        group_id="", message_type="FriendMessage", content="Private msg",
    )
    s_friend = await service.find_story_for_event(ev_friend)
    assert s_friend is not None
    assert s_friend.id == char_alice.story_id

    # Group message with ID 55555 -> routes to Bob
    ev_group = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="999",
        group_id="55555", message_type="GroupMessage", content="Group msg",
    )
    s_group = await service.find_story_for_event(ev_group)
    assert s_group is not None
    assert s_group.id == char_bob.story_id


@pytest.mark.asyncio
async def test_participant_purge_removes_all_private_scoped_data(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    char = await service.create_character_record(name="千", is_default=True)
    story = await service.get_story(char.story_id)

    ev = IncomingEvent(
        platform_id="aiocqhttp", self_id="10000", sender_id="20001",
        sender_name="Alice", message_type="FriendMessage", content="Hello",
    )
    part = await service.ensure_participant(story, ev, now)

    # Add memory and fact for this participant
    await db.insert("interlude_memory", {
        "story_id": story.id,
        "participant_id": part.id,
        "category": "fact",
        "content": "Alice loves tea",
        "importance": 0.8,
        "status": "active",
        "created_at": iso(now),
        "updated_at": iso(now),
    })
    await db.insert("interlude_fact", {
        "story_id": story.id,
        "participant_id": part.id,
        "scope": "relationship",
        "content": "Alice is a close friend",
        "importance": 0.8,
        "confidence": 0.9,
        "unresolved": 0,
        "last_seen_at": iso(now),
        "created_at": iso(now),
        "updated_at": iso(now),
    })
    await db.insert("interlude_script_entry", {
        "story_id": story.id,
        "participant_id": part.id,
        "kind": "user-message",
        "actor": "user",
        "content": "Hello",
        "occurred_at": iso(now),
        "metadata": "{}",
        "created_at": iso(now),
    })

    # Verify rows exist
    assert len(await db.get("interlude_memory", {"participant_id": part.id})) == 1
    assert len(await db.get("interlude_fact", {"participant_id": part.id})) == 1
    assert len(await db.get("interlude_script_entry", {"participant_id": part.id})) >= 1

    # Cascade purge
    stmts = [
        ("DELETE FROM interlude_script_entry WHERE participant_id=?", (part.id,)),
        ("DELETE FROM interlude_memory WHERE participant_id=?", (part.id,)),
        ("DELETE FROM interlude_fact WHERE participant_id=?", (part.id,)),
        ("DELETE FROM interlude_intent WHERE participant_id=?", (part.id,)),
        ("DELETE FROM interlude_participant WHERE id=?", (part.id,)),
    ]
    await db.execute_many(stmts)

    # Verify all participant-scoped rows are gone
    assert len(await db.get("interlude_memory", {"participant_id": part.id})) == 0
    assert len(await db.get("interlude_fact", {"participant_id": part.id})) == 0
    assert len(await db.get("interlude_script_entry", {"participant_id": part.id})) == 0
    assert len(await db.get("interlude_participant", {"id": part.id})) == 0


@pytest.mark.asyncio
async def test_active_stories_does_not_starve_registered_characters(multi_harness):
    service, db, clock = multi_harness
    now = clock.now()

    # Create an orphan active story not in character registry
    orphan_story = InterludeStory(
        id="orphan:story:999",
        platform_id="orphan",
        self_id="orphan",
        status="active",
        setting=service.initial_story_setting("Orphan"),
        state=empty_story_state(),
        cursor_at=now,
        created_at=now,
        updated_at=now,
    )
    await db.insert("interlude_story", orphan_story.model_dump(mode="json"))

    # Create a registered active character
    char = await service.create_character_record(name="千", is_default=True)

    # service.active_stories() should pick the registered character's story and not be crowded out
    active = await service.active_stories()
    active_ids = {s.id for s in active}
    assert char.story_id in active_ids
    assert orphan_story.id not in active_ids


@pytest.mark.asyncio
async def test_backup_restore_roundtrip_preserves_every_domain_table(multi_harness):
    from hdsi.database.migrations import TABLES

    service, db, clock = multi_harness
    now = clock.now()

    char = await service.create_character_record(name="千", is_default=True)
    await service.set_conversation_binding("qq", "100", "888", char.id, "group")

    mem_id = await db.insert_returning_id("interlude_memory", {
        "story_id": char.story_id,
        "participant_id": "",
        "category": "fact",
        "content": "千的咖啡杯是白色的",
        "importance": 0.8,
        "status": "active",
        "created_at": iso(now),
        "updated_at": iso(now),
    })
    assert isinstance(mem_id, int)

    fact_id = await db.insert_returning_id("interlude_fact", {
        "story_id": char.story_id,
        "participant_id": "",
        "scope": "world",
        "content": "小镇有一个旧火车站",
        "importance": 0.9,
        "confidence": 0.95,
        "unresolved": 0,
        "last_seen_at": iso(now),
        "created_at": iso(now),
        "updated_at": iso(now),
    })
    assert isinstance(fact_id, int)

    # Dump all tables
    dump_tables = {}
    for tbl in TABLES:
        rows = await db.get(tbl, {})
        dump_tables[tbl] = rows

    assert len(dump_tables["interlude_character"]) == 1
    assert len(dump_tables["interlude_conversation_binding"]) == 1
    assert len(dump_tables["interlude_memory"]) == 1
    assert len(dump_tables["interlude_fact"]) == 1

    # Wipe tables
    clear_stmts = [(f"DELETE FROM {tbl}", ()) for tbl in reversed(TABLES)]
    await db.execute_many(clear_stmts)

    for tbl in TABLES:
        assert len(await db.get(tbl, {})) == 0

    # Restore tables
    for tbl in TABLES:
        for r in dump_tables[tbl]:
            await db.insert(tbl, r)

    # Verify exact restore
    restored_chars = await db.get("interlude_character", {})
    assert len(restored_chars) == 1
    assert restored_chars[0]["name"] == "千"

    restored_bindings = await db.get("interlude_conversation_binding", {})
    assert len(restored_bindings) == 1
    assert restored_bindings[0]["conversation_type"] == "group"

    restored_mems = await db.get("interlude_memory", {})
    assert len(restored_mems) == 1
    assert restored_mems[0]["content"] == "千的咖啡杯是白色的"

    restored_facts = await db.get("interlude_fact", {})
    assert len(restored_facts) == 1
    assert restored_facts[0]["content"] == "小镇有一个旧火车站"
