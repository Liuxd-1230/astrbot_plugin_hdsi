"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

from hdsi.config import AccessRule, HdsiConfig
from hdsi.database.connection import Database
from hdsi.service import IncomingEvent, InterludeService
from tests.fakes import (
    SenderRecorder,
    ScriptedNarrator,
    SilentEmbedder,
    VirtualClock,
)


def make_config(**runtime_overrides) -> HdsiConfig:
    config = HdsiConfig()
    config.platform_gate.bot_accounts.append(AccessRule(id="10000"))
    config.platform_gate.user_accounts.append(AccessRule(id="20001"))
    config.platform_gate.user_accounts.append(AccessRule(id="20002"))
    config.runtime.user_message_debounce_seconds = runtime_overrides.pop(
        "debounce", 0.02
    )
    for key, value in runtime_overrides.items():
        setattr(config.runtime, key, value)
    return config


class Harness:
    """Bundles service + fakes for one test story world."""

    def __init__(self, db_path: str, config: HdsiConfig | None = None):
        self.config = config or make_config()
        self.db = Database(db_path)
        self.clock = VirtualClock()
        self.sender = SenderRecorder()
        self.narrator = ScriptedNarrator()
        self.service: InterludeService | None = None
        self.embedder = SilentEmbedder()

    async def start(self) -> "Harness":
        await self.db.connect()
        self.service = InterludeService(
            db=self.db, config=self.config, narrator=self.narrator,
            embedder=self.embedder, sender=self.sender, now_fn=self.clock.now,
        )
        return self

    async def stop(self) -> None:
        if self.service:
            await self.service.stop_background_tasks()
            await self.service.invalidate_buffered_narratives()
        await self.db.close()

    def event(self, sender_id="20001", content="hello", **kwargs) -> IncomingEvent:
        kwargs.setdefault("platform_id", "aiocqhttp")
        return IncomingEvent(
            platform_id=kwargs.pop("platform_id"),
            self_id=str(kwargs.pop("self_id", "10000")),
            sender_id=sender_id,
            sender_name=kwargs.pop("sender_name", f"user-{sender_id}"),
            umo=kwargs.pop("umo", None) or f"{kwargs.get('platform_id', 'aiocqhttp')}:FriendMessage:{sender_id}",
            message_type=kwargs.pop("message_type", "FriendMessage"),
            content=content, **kwargs,
        )

    async def setup_story(self, event: IncomingEvent | None = None):
        event = event or self.event()
        return await self.service.create_story(event)


async def _make_harness(config=None):
    tmp = tempfile.mkdtemp(prefix="hdsi-test-")
    harness = Harness(os.path.join(tmp, "test.db"), config)
    await harness.start()
    return harness


@pytest_asyncio.fixture
async def harness():
    h = await _make_harness()
    yield h
    await h.stop()


@pytest_asyncio.fixture
async def quiet_harness():
    config = make_config(auto_advance_enabled=False)
    h = await _make_harness(config)
    yield h
    await h.stop()


def drain_async_tasks():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
