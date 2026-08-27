"""End-to-end smoke: story creation, debounced user turn, immediate reply."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdsi.config import HdsiConfig
from hdsi.database.connection import Database
from hdsi.service import IncomingEvent, InterludeService
from tests.fakes import (
    SenderRecorder,
    ScriptedNarrator,
    SilentEmbedder,
    VirtualClock,
    wait_for,
)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        await db.connect()
        clock = VirtualClock()
        config = HdsiConfig()
        config.runtime.user_message_debounce_seconds = 0.05
        config.runtime.auto_advance_enabled = True
        config.runtime.auto_create = False
        sender = SenderRecorder()
        narrator = ScriptedNarrator()

        service = InterludeService(
            db=db, config=config, narrator=narrator, embedder=SilentEmbedder(),
            sender=sender, now_fn=clock.now,
        )

        event = IncomingEvent(
            platform_id="aiocqhttp", self_id="10000", sender_id="20001",
            sender_name="Alice", umo="aiocqhttp:FriendMessage:20001",
            message_type="FriendMessage", content="你好，在吗？",
        )
        # Gate is closed by default (empty allowlist) — auto-enroll for test.
        service.config.platform_gate.bot_accounts.append(
            __import__("hdsi.config", fromlist=["AccessRule"]).AccessRule(id="10000")
        )
        service.config.platform_gate.user_accounts.append(
            __import__("hdsi.config", fromlist=["AccessRule"]).AccessRule(id="20001")
        )

        story = await service.create_story(event)
        assert story.id == "character:aiocqhttp:10000"
        print("story created:", story.id)

        narrator.enqueue({
            "script": "她正在窗边整理书架，手机亮了起来。",
            "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "在的，怎么了？"}},
        })
        assert await service.receive(event)
        ok = await wait_for(lambda: len(sender.sent) >= 1)
        assert ok, f"no delivery; sent={sender.sent}"
        assert sender.texts()[0] == "在的，怎么了？"
        print("immediate reply delivered:", sender.texts())

        entries = await service.recent_entries(story.id, 10)
        kinds = [(e.kind, e.actor) for e in entries]
        assert ("user-message", "user") in kinds
        assert ("character-message", "character") in kinds
        print("script trace:", kinds)

        fresh = await service.get_story(story.id)
        assert fresh.cursor_at > clock.now() - timedelta(seconds=5)
        participant = await service.find_participant_for_event(event, fresh)
        assert participant is not None
        assert participant.state.unread_message_count == 0
        assert participant.state.last_character_message_at is not None
        print("participant state updated")

        await service.stop_background_tasks()
        service.invalidate_buffered_narratives()
        await db.close()
    print("SMOKE OK")


asyncio.run(main())
