"""HDSI core service. Port of HDS-Interlude src/service.ts (0.1.3-beta1).

Semantic invariants preserved from the original:
- ScriptEntry is the source of truth; every visible message is traceable.
- Per-story serial queues; one narrator per shared story at a time.
- Debounced user bursts become ONE writing turn.
- A new message invalidates an uncommitted generation; once the first bubble
  is committed, later <sep/> segments become cancellable split-message
  intents whose unsent text re-enters the next prompt as interrupted drafts.
- Due delayed replies NEVER send pre-written text: the intent triggers a new
  narrative turn over current life; the model decides again.
- Agency proactive contacts require grounded sources, capacity, willingness.
- Alter analysis never blocks the visible reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

from . import agency as agency_mod
from . import alter as alter_mod
from .concurrency import BrowserSlots, SerialQueues, is_transient_db_error
from .database.connection import Database
from .logging import format_layered_log, LayeredLogInput, phase_label
from .time import calendar_day_key, format_log_time, local_clock_minutes
from .types import (
    DEFAULT_AGENCY_CONFIG,
    AgencyConfig,
    AgencyWindowState,
    BrowserIntentDraft,
    ContinuitySnapshot,
    FactScope,
    IntentStatus,
    IntentUpdateDraft,
    InterludeArc,
    InterludeParticipant,
    InterludeScene,
    InterludeStory,
    MemoryDraft,
    NarrativeDecision,
    NarrativeFact,
    NarrativeIntent,
    NarrativeMemory,
    NarrativePhase,
    NarrativeRequest,
    OutgoingMessageDraft,
    OverlaySnapshot,
    ParticipantState,
    ProactiveContactDraft,
    SceneContext,
    ScriptEntry,
    ScriptEntryDraft,
    StatePatchProposal,
    StatePatchTarget,
    StorySetting,
    StoryState,
    WebObservation,
    clip,
    clamp_number,
    empty_participant_state,
    empty_story_setting,
    empty_story_state,
    iso,
    normalize_participant_state,
    normalize_story_state,
    parse_date,
)
from .config import HdsiConfig

logger = logging.getLogger("hdsi.service")

SECOND = 1.0
MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0

# Intent types that represent already-decided transport work: they are
# delivered directly (send → finalize) and never open a new narrative turn.
TRANSPORT_INTENT_TYPES = ("split-message", "outbound-message",
                         "outbound-group-message")


# ------------------------------------------------------------------ events

@dataclass
class IncomingEvent:
    """Normalized platform event handed in by the AstrBot adapter."""

    platform_id: str
    self_id: str
    sender_id: str
    sender_name: str
    umo: str
    message_type: str  # FriendMessage | GroupMessage
    group_id: str = ""
    content: str = ""
    image_sources: list[str] = field(default_factory=list)
    is_mention: bool = False
    is_admin: bool = False
    message_id: str = ""


SenderFn = Callable[[InterludeStory, InterludeParticipant, str], Awaitable[bool]]
GroupSenderFn = Callable[[InterludeStory, str, str], Awaitable[bool]]


def default_now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ service

class InterludeService:
    def __init__(
        self,
        db: Database,
        config: HdsiConfig,
        narrator: Any,
        embedder: Any,
        sender: SenderFn,
        group_sender: Optional[GroupSenderFn] = None,
        now_fn: Callable[[], datetime] = default_now,
        browser_fetch: Optional[Callable[..., Awaitable[tuple[str, str]]]] = None,
        image_loader: Optional[Callable[[str], Awaitable[tuple[str, str]]]] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.narrator = narrator
        self.embedder = embedder
        self.sender = sender
        self.group_sender = group_sender
        self.now_fn = now_fn
        # Web pages and images are DIFFERENT adapters (P1): the web fetcher
        # returns (title, visible_text) for HTML pages; the image loader
        # returns (mime_type, data_uri) for picture sources.
        self.browser_fetch = browser_fetch
        self.image_loader = image_loader

        self.queues = SerialQueues()
        self.write_queue_lock = asyncio.Lock()

        self.buffered_turns: dict[str, BufferedNarrativeTurn] = {}
        self.buffered_group_turns: dict[str, BufferedGroupTurn] = {}
        self.due_wake_tasks: dict[str, asyncio.Task] = {}
        self.due_wake_at: dict[str, float] = {}
        self.interrupted_typing_participants: set[str] = set()
        self.narrating_stories: set[str] = set()
        self.fact_backfills: set[str] = set()
        self.scheduled_compactions: set[str] = set()
        self.scheduled_alter_analyses: set[str] = set()
        self.browser_slots = BrowserSlots(max(1, config.browser.max_concurrent_pages))

        # Real-time backoff used when a flush finds the story busy; hosts with
        # virtual clocks (simulations/tests) may shrink it.
        self.story_busy_retry_delay_seconds = 0.25
        self.database_resetting = False
        self.sweep_running = False
        self.compaction_sweep_running = False
        self._background_task: Optional[asyncio.Task] = None
        self._memory_task: Optional[asyncio.Task] = None

    def now(self) -> datetime:
        return self.now_fn()

    # ------------------------------------------------------------ logging

    def emit_log(self, level: str, output: str) -> None:
        log = logger.error if level == "error" else logger.warning if level == "warn" else logger.info if level == "info" else logger.debug
        log(output)

    def write_report(
        self,
        level: str,
        protagonist: str,
        phase: Optional[str],
        message: str,
        args: tuple[Any, ...] = (),
        standalone: bool = False,
    ) -> None:
        rank = {"silent": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}
        verbosity_rank = {"summary": 1, "standard": 2, "diagnostic": 3}
        cfg = self.config.logging
        if rank.get(cfg.level, 3) < rank.get(level, 3):
            return
        output = (
            format_layered_log(LayeredLogInput(
                level=level, phase=phase, protagonist=protagonist, message=message, args=args,
                colors=cfg.colors, color_theme=cfg.color_theme, kaomoji=cfg.kaomoji,
                standalone=standalone,
            ))
            if cfg.format == "layered"
            else f"[{phase_label(phase)}] {message % args if args else message}"
        )
        self.emit_log(level, output)

    def report_operation(
        self,
        verbosity: str,
        level: str,
        story: InterludeStory,
        phase: Optional[str],
        message: str,
        *args: Any,
    ) -> None:
        rank = {"summary": 1, "standard": 2, "diagnostic": 3}
        if rank.get(self.config.logging.verbosity, 2) < rank.get(verbosity, 2):
            return
        self.write_report(level, story.setting.character.name, phase, message, args)

    def report_standalone(
        self,
        level: str,
        message: str,
        *args: Any,
        verbosity: str = "standard",
    ) -> None:
        rank = {"summary": 1, "standard": 2, "diagnostic": 3}
        if rank.get(self.config.logging.verbosity, 2) < rank.get(verbosity, 2):
            return
        self.write_report(level, "HDSI", None, message, args, standalone=True)

    # ------------------------------------------------------------ access gate

    def can_handle_event(self, event: IncomingEvent) -> bool:
        gate = self.config.platform_gate
        self_id = normalize_account_id(event.self_id)
        user_id = normalize_account_id(event.sender_id)
        if gate.ignore_self_messages and self_id and self_id == user_id:
            return False
        if not account_enabled(gate.bot_accounts, self_id):
            return False
        return account_enabled(gate.user_accounts, user_id)

    def can_manage_event(self, event: IncomingEvent) -> bool:
        if not self.can_handle_event(event):
            return False
        managers = [m.strip() for m in self.config.shared_story.manager_ids if m.strip()]
        if not managers:
            # Least-privilege default (P1): an empty manager list means ONLY
            # platform administrators may run destructive commands; being in
            # the story whitelist alone never grants story-root powers.
            return bool(getattr(event, "is_admin", False))
        return any(normalize_account_id(m) == normalize_account_id(event.sender_id) for m in managers)

    def can_handle_participant(self, participant: InterludeParticipant) -> bool:
        gate = self.config.platform_gate
        if not account_enabled(
            gate.bot_accounts, normalize_account_id(participant.self_id)
        ):
            return False
        return account_enabled(
            gate.user_accounts, normalize_account_id(_user_of_session_key(participant.session_key))
        )

    def can_handle_story(self, story: InterludeStory) -> bool:
        return account_enabled(
            self.config.platform_gate.bot_accounts, normalize_account_id(story.self_id)
        )

    def group_rule(self, group_id: str):
        normalized = normalize_group_id(group_id)
        for rule in self.config.platform_gate.group_chats:
            if rule.enabled and normalize_group_id(rule.id) == normalized:
                return rule
        return None

    # ------------------------------------------------------------ stories

    async def get_canonical_story(
        self,
        preferred_id: str | None = None,
        platform_id: str | None = None,
        self_id: str | None = None,
    ) -> Optional[InterludeStory]:
        """One canonical story per bot identity.

        The archiving guard ONLY runs when an identity scope
        (platform_id + self_id) is provided: it then keeps one active story
        for that identity and archives that identity's other active rows.
        An unscoped call is non-destructive — it returns the most recently
        updated active story without ever touching other identities' stories
        (P0-4).
        """
        scoped = bool(platform_id and self_id)
        query: dict[str, Any] = {"status": "active"}
        if platform_id:
            query["platform_id"] = platform_id
            if self_id:
                query["self_id"] = self_id
        rows = await self.db.get(
            "interlude_story", query,
            order_by="updated_at", descending=True,
        )
        stories = [InterludeStory.model_validate(r) for r in rows]
        if not stories:
            return None
        canonical = None
        if preferred_id:
            canonical = next((s for s in stories if s.id == preferred_id), None)
        if canonical is None:
            canonical = next((s for s in stories if s.id.startswith("character:")), None)
        if canonical is None:
            canonical = stories[0]
        now = self.now()
        for story in stories:
            if story.id == canonical.id:
                continue
            if not scoped:
                # Never archive outside an explicit identity scope.
                break
            await self.update_row("interlude_story", {"id": story.id}, {
                "status": "archived", "updated_at": iso(now),
            })
            self.report_standalone(
                "warn",
                "主剧本归档完成 原因=同一身份检测到多个活动故事 保留=%s 已归档=%s 身份=%s/%s",
                canonical.id, story.id, canonical.platform_id, canonical.self_id,
            )
        return canonical

    async def latest_active_story(self) -> Optional[InterludeStory]:
        """Non-destructive view helper: most recent active story overall."""
        rows = await self.db.get(
            "interlude_story", {"status": "active"},
            order_by="updated_at", descending=True, limit=1,
        )
        return InterludeStory.model_validate(rows[0]) if rows else None

    def story_id_for(self, event: IncomingEvent) -> str:
        return f"character:{event.platform_id}:{event.self_id}"

    async def find_story_for_event(self, event: IncomingEvent) -> Optional[InterludeStory]:
        preferred = self.story_id_for(event)
        existing = await self.get_canonical_story(
            preferred, platform_id=event.platform_id, self_id=event.self_id,
        )
        if existing is not None:
            return existing
        rows = await self.db.get("interlude_story", {"id": preferred})
        if rows:
            return InterludeStory.model_validate(rows[0])
        return None

    async def active_stories(self) -> list[InterludeStory]:
        """All active stories across bot identities (bounded by config)."""
        rows = await self.db.get(
            "interlude_story", {"status": "active"},
            order_by="updated_at", descending=True,
            limit=max(1, self.config.runtime.max_stories_per_sweep),
        )

        # Enforce the per-identity single-story invariant.
        seen_identity: set[tuple[str, str]] = set()
        out: list[InterludeStory] = []
        now = self.now()
        for story in [InterludeStory.model_validate(r) for r in rows]:
            key = (story.platform_id, story.self_id)
            if key in seen_identity:
                await self.update_row("interlude_story", {"id": story.id}, {
                    "status": "archived", "updated_at": iso(now),
                })
                continue
            seen_identity.add(key)
            out.append(story)
        return out

    async def get_story(self, story_id: str) -> InterludeStory:
        rows = await self.db.get("interlude_story", {"id": story_id})
        if not rows:
            raise LookupError(f"Interlude story not found: {story_id}")
        return InterludeStory.model_validate(rows[0])

    async def update_row(self, table: str, query: dict[str, Any], data: dict[str, Any]) -> None:
        await self.db.update(table, query, data)

    async def create_story(self, event: IncomingEvent, name: str | None = None) -> InterludeStory:
        existing = await self.find_story_for_event(event)
        if existing is not None:
            if event.message_type == "FriendMessage":
                await self.ensure_participant(existing, event)
            return existing
        now = self.now()
        setting = self.initial_story_setting(name)
        story = InterludeStory(
            id=self.story_id_for(event),
            platform_id=event.platform_id,
            self_id=event.self_id,
            status="active",
            setting=setting,
            state=empty_story_state(),
            cursor_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            await self.db.insert("interlude_story", story.model_dump(mode="json"))
        except Exception as error:
            raced_rows = await self.db.get("interlude_story", {"id": story.id})
            if not raced_rows:
                raise
            raced = InterludeStory.model_validate(raced_rows[0])
            await self.ensure_continuity(raced, now)
            if event.message_type == "FriendMessage":
                await self.ensure_participant(raced, event, now)
            return raced
        await self.ensure_continuity(story, now)
        if event.message_type == "FriendMessage":
            await self.ensure_participant(story, event, now)
        await self.append_entry(story.id, ScriptEntryDraft(
            kind="setup", actor="system",
            content=f"The story begins with {setting.character.name}.",
            occurred_at=iso(now), metadata={},
        ), now)
        await self.schedule_next_automatic_advance(story.id, now)
        return story

    def initial_story_setting(self, name: str | None = None) -> StorySetting:
        setting = empty_story_setting()
        defaults = self.config.story_defaults
        setting.character.name = (name or "").strip() or defaults.character_name or setting.character.name
        setting.character.profile = defaults.character_profile
        setting.user_profile = defaults.user_profile
        setting.relationship = defaults.relationship
        setting.world = defaults.world
        setting.supporting_cast = defaults.supporting_cast
        setting.location = defaults.location
        setting.style = defaults.style or setting.style
        setting.timezone = defaults.timezone or setting.timezone
        return setting

    async def ensure_participant(
        self,
        story: InterludeStory,
        event: IncomingEvent,
        now: datetime | None = None,
        known_existing: Optional[InterludeParticipant] = None,
    ) -> InterludeParticipant:
        from .types import utcnow

        now = now or self.now()
        account = self.user_rule(event.sender_id)
        existing = known_existing or await self.find_participant_for_event(event, story)
        defaults = self.config.story_defaults
        person_id = (account.person_id.strip() if account else "") or (existing.person_id if existing else "") or event.sender_id
        display_name = (account.label.strip() if account else "") or (
            existing.display_name if existing else ""
        ) or event.sender_name or event.sender_id
        profile = (account.profile.strip() if account else "") or (
            existing.profile if existing else ""
        ) or defaults.user_profile
        relationship = (account.relationship.strip() if account else "") or (
            existing.relationship if existing else ""
        ) or defaults.relationship
        if existing is not None:
            changed = (
                existing.story_id != story.id
                or existing.umo != event.umo
                or existing.person_id != person_id
                or existing.display_name != display_name
                or existing.profile != profile
                or existing.relationship != relationship
            )
            if changed:
                await self.db.update("interlude_participant", {"id": existing.id}, {
                    "story_id": story.id, "umo": event.umo, "person_id": person_id,
                    "display_name": display_name, "profile": profile,
                    "relationship": relationship, "updated_at": iso(now),
                })
                self.report_operation("diagnostic", "debug", story, "user-message",
                                      "参与者资料已同步 参与者=%s", existing.id)
            return existing.model_copy(update={
                "story_id": story.id, "umo": event.umo, "person_id": person_id,
                "display_name": display_name, "profile": profile,
                "relationship": relationship,
                "updated_at": now if changed else existing.updated_at,
            })

        base_id = f"{event.platform_id}:{event.self_id}:{event.sender_id}"
        globally_existing_rows = await self.db.get("interlude_participant", {"id": base_id})
        participant_id = base_id
        if globally_existing_rows and globally_existing_rows[0].get("story_id") != story.id:
            participant_id = f"{base_id}:{story.id}"[:255]
        participant = InterludeParticipant(
            id=participant_id,
            story_id=story.id,
            platform_id=event.platform_id,
            self_id=event.self_id,
            session_key=event.sender_id,
            umo=event.umo,
            message_type=event.message_type,
            person_id=person_id,
            display_name=display_name,
            profile=profile,
            relationship=relationship,
            state=empty_participant_state(),
            status="active",
            created_at=now,
            updated_at=now,
        )
        try:
            await self.db.insert("interlude_participant", participant.model_dump(mode="json"))
        except Exception:
            raced = await self.find_participant_for_event(event, story)
            if raced is None:
                raise
            return raced
        await self.append_entry(story.id, ScriptEntryDraft(
            kind="participant-joined", actor="system",
            content=f"{participant.display_name} entered the character's relationship network.",
            occurred_at=iso(now), metadata={"personId": participant.person_id},
        ), now, participant.id)
        return participant

    def user_rule(self, user_id: str):
        normalized = normalize_account_id(user_id)
        for rule in self.config.platform_gate.user_accounts:
            if rule.enabled and normalize_account_id(rule.id) == normalized:
                return rule
        return None

    async def find_participant_for_event(
        self, event: IncomingEvent, story: InterludeStory | None = None
    ) -> Optional[InterludeParticipant]:
        resolved = story or await self.find_story_for_event(event)
        if resolved is None:
            return None
        rows = await self.db.get("interlude_participant", {"story_id": resolved.id})
        participants = [InterludeParticipant.model_validate(r) for r in rows]

        def same_endpoint(p: InterludeParticipant) -> bool:
            return (
                p.platform_id == event.platform_id
                and normalize_account_id(p.self_id) == normalize_account_id(event.self_id)
                and p.session_key == event.sender_id
            )

        for participant in participants:
            if same_endpoint(participant):
                return participant
        # Fallback: match by UMO when session_key differs across reloads.
        for participant in participants:
            if participant.umo == event.umo:
                return participant
        return None

    async def get_participant(self, participant_id: str) -> Optional[InterludeParticipant]:
        rows = await self.db.get("interlude_participant", {"id": participant_id})
        return InterludeParticipant.model_validate(rows[0]) if rows else None

    async def participants(self, story_id: str, include_paused: bool = False) -> list[InterludeParticipant]:
        rows = await self.db.get("interlude_participant", {"story_id": story_id})
        out = [InterludeParticipant.model_validate(r) for r in rows]
        out = [p for p in out if include_paused or p.status == "active"]
        out.sort(key=lambda p: p.updated_at, reverse=True)
        return out

    async def recent_entries(self, story_id: str, limit: int = 30) -> list[ScriptEntry]:
        bounded = max(1, min(limit, 200))
        rows = await self.db.get(
            "interlude_script_entry", {"story_id": story_id},
            limit=bounded, order_by="occurred_at", descending=True,
        )
        entries = [ScriptEntry.model_validate(r) for r in rows]
        entries.reverse()
        return entries

    async def memories(
        self, story_id: str, limit: int = 20, participant_id: str | None = None
    ) -> list[NarrativeMemory]:
        bounded = max(1, min(limit * 4, 500))
        rows = await self.db.get(
            "interlude_memory", {"story_id": story_id, "status": "active"},
            limit=bounded, order_by="importance", descending=True,
        )
        items = [NarrativeMemory.model_validate(r) for r in rows]
        items = [
            m for m in items
            if participant_id is None or not m.participant_id or m.participant_id == participant_id
        ]
        items.sort(key=lambda m: (-m.importance, -m.updated_at.timestamp()))
        return items[:limit]

    async def facts(
        self,
        story_id: str,
        limit: int = 20,
        query: str = "",
        participant_id: str | None = None,
    ) -> list[NarrativeFact]:
        memory = self.config.memory
        candidate_limit = max(20, min(limit * 5, memory.max_facts_per_story, 300))
        rows = await self.db.get(
            "interlude_fact", {"story_id": story_id, "status": "active"},
            limit=candidate_limit, order_by="importance", descending=True,
        )
        items = [NarrativeFact.model_validate(r) for r in rows]
        query_embedding: list[float] = []
        if query.strip() and self.config.models.embedding_live_query:
            query_embedding = await self.embed_text(query)
        scored = []
        for fact in items:
            if participant_id is not None and fact.participant_id and fact.participant_id != participant_id:
                continue
            scored.append((fact_score(fact, memory, query_embedding), fact))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].updated_at.timestamp(), -pair[1].id))
        return [fact for _, fact in scored[:limit]]

    async def web_observations(self, story_id: str, participant_id: str | None = None) -> list[WebObservation]:
        if not self.config.browser.enabled:
            return []
        limit = max(1, min(self.config.browser.max_observations_in_prompt, 20))
        rows = await self.db.get(
            "interlude_web_observation", {"story_id": story_id},
            limit=max(limit * 4, 20), order_by="accessed_at", descending=True,
        )
        observations = [WebObservation.model_validate(r) for r in rows]
        observations = [o for o in observations if o.status == "success"]
        observations = [
            o for o in observations
            if self.config.shared_story.share_participant_details
            or not o.participant_id
            or o.participant_id == (participant_id or "")
        ]
        observations = observations[:limit]
        observations.reverse()
        return observations

    async def active_scene(self, story_id: str) -> Optional[InterludeScene]:
        rows = await self.db.get(
            "interlude_scene", {"story_id": story_id, "status": "active"},
            limit=1, order_by="updated_at", descending=True,
        )
        return InterludeScene.model_validate(rows[0]) if rows else None

    async def active_arc(self, story_id: str) -> Optional[InterludeArc]:
        rows = await self.db.get(
            "interlude_arc", {"story_id": story_id, "status": "active"},
            limit=1, order_by="updated_at", descending=True,
        )
        return InterludeArc.model_validate(rows[0]) if rows else None

    async def overlay_snapshots_for_prompt(
        self, story_id: str, participant_id: str | None = None, background: bool = False
    ) -> list[OverlaySnapshot]:
        if not self.config.memory.overlay_compression_enabled:
            return []
        rows = await self.db.get(
            "interlude_overlay_snapshot", {"story_id": story_id, "status": "active"},
            order_by="period_end", descending=True,
        )
        snapshots = [OverlaySnapshot.model_validate(r) for r in rows]
        visible = [
            s for s in snapshots
            if not s.participant_id
            or (
                self.config.shared_story.share_participant_details
                if background
                else s.participant_id == (participant_id or "")
            )
        ]
        result: list[OverlaySnapshot] = []
        for target in ("character", "world", "relationship"):
            matches = [s for s in visible if s.target.value == target]
            monthly = next((s for s in matches if s.tier == "monthly"), None)
            if monthly:
                result.append(monthly)
            result.extend([s for s in matches if s.tier == "weekly"][:4])
        return result

    # ------------------------------------------------------------ entry append

    async def append_entry(
        self,
        story_id: str,
        draft: ScriptEntryDraft | dict[str, Any],
        now: datetime,
        participant_id: str = "",
    ) -> ScriptEntry:
        if isinstance(draft, dict):
            draft = ScriptEntryDraft.model_validate(draft)
        occurred_at = parse_date(draft.occurred_at) or now
        row = {
            "story_id": story_id,
            "participant_id": participant_id,
            "kind": clip(draft.kind, 32) or "life",
            "actor": clip(draft.actor, 32) if draft.actor else "character",
            "content": clip(draft.content, 12_000),
            "occurred_at": iso(occurred_at),
            "metadata": draft.metadata if isinstance(draft.metadata, dict) else {},
            "created_at": iso(now),
        }
        # INSERT + rowid are one write-queue task: concurrent stories can
        # never observe each other's generated ids.
        entry_id = await self.db.insert_returning_id("interlude_script_entry", row)
        created = await self.db.get("interlude_script_entry", {"id": entry_id})
        return ScriptEntry.model_validate(created[0])

    async def append_memory(
        self, story_id: str, memory: MemoryDraft, now: datetime, participant_id: str = ""
    ) -> None:
        await self.db.insert("interlude_memory", {
            "story_id": story_id,
            "participant_id": participant_id,
            "category": clip(memory.category, 32) or "fact",
            "content": clip(memory.content, 4_000),
            "importance": clamp_number(memory.importance, 0.5, 0, 1),
            "status": "active",
            "source_entry_id": None,
            "created_at": iso(now),
            "updated_at": iso(now),
        })

    async def append_intent(
        self, story_id: str, intent_data: dict[str, Any], now: datetime, participant_id: str = ""
    ) -> None:
        memory = self.config.memory
        not_before = parse_date(intent_data.get("not_before") or intent_data.get("notBefore"))
        payload = intent_data.get("payload") if isinstance(intent_data.get("payload"), dict) else {}
        active_consequence = intent_data.get("type") == "active-consequence" and payload.get("lifecycle") == "active"
        if active_consequence and not memory.active_consequences_enabled:
            return
        requested_expires_at = consequence_expires_at(payload) if active_consequence else None
        max_lifetime = max(1, memory.active_consequence_max_days) * DAY
        expires_at: datetime | None = None
        if requested_expires_at is not None and requested_expires_at > now:
            expires_at = min(requested_expires_at, now + timedelta(seconds=max_lifetime))
        if (
            not_before is None
            or (not active_consequence and not_before <= now)
            or (active_consequence and expires_at is None)
        ):
            return
        normalized_payload = payload
        if active_consequence:
            strength = payload.get("strength")
            normalized_payload = {
                **payload,
                "strength": clamp_number(strength, memory.active_consequence_default_strength, 0, 1),
                "expiresAt": iso(expires_at),
            }
        await self.db.insert("interlude_intent", {
            "story_id": story_id,
            "participant_id": intent_data.get("participant_id") or participant_id,
            "type": clip(intent_data.get("type"), 32) or "follow-up",
            "summary": clip(intent_data.get("summary"), 4_000),
            "not_before": iso(not_before),
            "status": "pending",
            "payload": normalized_payload,
            "created_at": iso(now),
            "updated_at": iso(now),
        })

    # ------------------------------------------------------------ continuity

    async def ensure_continuity(self, story: InterludeStory, now: datetime) -> None:
        arc = await self.active_arc(story.id)
        if arc is None:
            await self.db.insert("interlude_arc", {
                "story_id": story.id, "status": "active", "title": "Beginning",
                "summary": "", "scene_count": 0,
                "created_at": iso(now), "updated_at": iso(now),
            })
            arc = await self.active_arc(story.id)
        scene = await self.active_scene(story.id)
        if scene is None:
            await self.db.insert("interlude_scene", {
                "story_id": story.id, "status": "active",
                "started_at": iso(now), "ended_at": None,
                "hook": "", "summary": "", "entry_count": 0, "last_entry_id": None,
                "created_at": iso(now), "updated_at": iso(now),
            })
            scene = await self.active_scene(story.id)
            if arc is not None:
                await self.db.update("interlude_arc", {"id": arc.id}, {
                    "scene_count": arc.scene_count + 1, "updated_at": iso(now),
                })
        if arc and scene and (
            story.state.active_arc_id != arc.id or story.state.active_scene_id != scene.id
        ):
            state = story.state.model_copy(update={"active_arc_id": arc.id, "active_scene_id": scene.id})
            await self.db.update("interlude_story", {"id": story.id}, {
                "state": state.model_dump(mode="json"), "updated_at": iso(now),
            })

    # ------------------------------------------------------------ incoming DM

    async def receive(self, event: IncomingEvent) -> bool:
        if self.database_resetting:
            return False
        if not self.can_handle_event(event):
            return False
        story = await self.find_story_for_event(event)
        if story is None and self.config.runtime.auto_create:
            story = await self.create_story(event)
        if story is None or story.status != "active":
            self.report_standalone(
                "diagnostic", "私聊未处理：故事不存在或已暂停 平台=%s 用户=%s",
                event.platform_id, event.sender_id, verbosity="diagnostic",
            )
            return False
        participant = await self.find_participant_for_event(event, story)
        if participant is not None:
            participant = await self.ensure_participant(story, event, self.now(), participant)
        elif self.config.shared_story.auto_enroll_participants:
            participant = await self.ensure_participant(story, event)
        if participant is None or participant.status != "active":
            return False

        # Synchronously mark interruption before waiting for the story queue;
        # this lets this arrival invalidate an about-to-persist request and
        # stop a due split segment before transport starts.
        self.signal_incoming_interruption(story, participant)
        self.report_operation("summary", "info", story, "user-message",
                              "收到参与者私聊消息 参与者=%s", participant.id)

        accepted = await self.queues.run(story.id, lambda: self._record_incoming(story, participant, event))
        if accepted is None:
            return False
        recorded_story, recorded_participant, recorded_at, superseded = accepted
        self.buffer_user_narrative(
            recorded_story, recorded_participant, event, recorded_at, superseded,
        )
        self.report_operation("standard", "info", recorded_story, "user-message",
                              "用户回合已入队 参与者=%s 已取消旧计划=%d",
                              recorded_participant.id, len(superseded))
        return True

    async def _record_incoming(self, story: InterludeStory, participant: InterludeParticipant, event: IncomingEvent):
        current_rows = await self.db.get("interlude_story", {"id": story.id})
        if not current_rows:
            return None
        current = InterludeStory.model_validate(current_rows[0])
        current_participant = await self.get_participant(participant.id)
        if current_participant is None or current_participant.status != "active":
            return None
        now = self.now()
        incoming = await self.record_incoming_message(current_participant, now)
        superseded = await self.cancel_pending_outgoing_messages(
            current.id, incoming.id, now,
            self.config.runtime.cancel_delayed_replies_on_user_message,
        )
        visual_content = event.content
        await self.append_entry(current.id, ScriptEntryDraft(
            kind="user-message", actor="user", content=visual_content,
            occurred_at=iso(now),
            metadata={
                "platform": event.platform_id,
                "messageId": event.message_id,
                "personId": incoming.person_id,
            },
        ), now, incoming.id)
        await self.pause_automatic_advance_after_user_message(current.id, now)
        return current, incoming, now, superseded

    async def record_incoming_message(self, participant: InterludeParticipant, now: datetime) -> InterludeParticipant:
        current = normalize_participant_state(participant.state)
        state = current.model_copy(update={
            "unread_message_count": current.unread_message_count + 1,
            "pending_reply_count": current.pending_reply_count + 1,
            "last_user_message_at": iso(now),
        })
        await self.db.update("interlude_participant", {"id": participant.id}, {
            "state": state.model_dump(mode="json"), "updated_at": iso(now),
        })
        return participant.model_copy(update={"state": state, "updated_at": now})

    async def mark_participant_seen(self, participant: InterludeParticipant, now: datetime) -> None:
        current = normalize_participant_state(participant.state)
        state = current.model_copy(update={"unread_message_count": 0})
        await self.db.update("interlude_participant", {"id": participant.id}, {
            "state": state.model_dump(mode="json"), "updated_at": iso(now),
        })

    async def record_character_message(self, participant: InterludeParticipant, now: datetime) -> None:
        current = normalize_participant_state(participant.state)
        state = current.model_copy(update={
            "unread_message_count": 0,
            "pending_reply_count": 0,
            "last_character_message_at": iso(now),
        })
        await self.db.update("interlude_participant", {"id": participant.id}, {
            "state": state.model_dump(mode="json"), "updated_at": iso(now),
        })


# ------------------------------------------------------------------ buffers

@dataclass
class BufferedUserMessage:
    content: str
    occurred_at: datetime
    superseded_intents: list[NarrativeIntent]
    image_sources: list[str]


@dataclass
class BufferedNarrativeTurn:
    story_id: str
    participant_id: str
    messages: list[BufferedUserMessage] = field(default_factory=list)
    latest_event: Optional[IncomingEvent] = None
    timer_task: Optional[asyncio.Task] = None
    next_revision: int = 0
    in_flight_request_id: Optional[int] = None
    first_message_committed_request_id: Optional[int] = None
    obsolete_request_ids: set[int] = field(default_factory=set)


@dataclass
class BufferedGroupTurn:
    story_id: str
    group_id: str
    channel_id: str
    messages: list[Any] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None
    revision: int = 0
    latest_event: Optional[IncomingEvent] = None
    rule: Any = None


# ------------------------------------------------------------------ misc helpers

def normalize_account_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    for _ in range(3):
        stripped = normalized
        for prefix in ("private:", "user:", "onebot:", "napcat:", "qq:"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
        if stripped == normalized:
            break
        normalized = stripped
    return normalized


def _user_of_session_key(session_key: str) -> str:
    return session_key


def normalize_group_id(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in ("group:", "guild:"):
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def account_enabled(rules: Iterable, account_id: str) -> bool:
    normalized = normalize_account_id(account_id)
    if not normalized:
        return False
    for rule in rules:
        if rule.enabled and normalize_account_id(rule.id) == normalized:
            return True
    return False


def consequence_expires_at(payload: Any) -> Optional[datetime]:
    if not isinstance(payload, dict):
        return None
    return parse_date(payload.get("expiresAt", payload.get("expires_at")))


def consequence_strength(payload: Any, fallback: float = 0.55) -> float:
    value = payload.get("strength") if isinstance(payload, dict) else None
    return clamp_number(value, fallback, 0, 1)


def is_active_consequence(intent: NarrativeIntent) -> bool:
    return intent.type == "active-consequence" and intent.payload.get("lifecycle") == "active"


def narrative_cursor(story: InterludeStory, now: datetime) -> datetime:
    cursor = story.cursor_at
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    return now if cursor > now else cursor


def fact_score(fact: NarrativeFact, memory, query_embedding: list[float]) -> float:
    from math import exp, sqrt

    age_days = max(0.0, (datetime.now(timezone.utc) - fact.last_seen_at).total_seconds() / DAY)
    recency = exp(-age_days / 30)
    similarity = cosine_similarity(query_embedding, fact.embedding)
    semantic = 0.0 if similarity is None else max(0.0, similarity)
    return (
        fact.importance * memory.fact_importance_weight
        + fact.confidence * memory.fact_confidence_weight
        + recency * memory.fact_recency_weight
        + semantic * memory.semantic_weight
        + (1.0 if fact.unresolved else 0.0) * memory.unresolved_weight
    )


def cosine_similarity(left: list[float], right: list[float]) -> Optional[float]:
    if not left or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_mag = sum(a * a for a in left)
    right_mag = sum(b * b for b in right)
    if not left_mag or not right_mag:
        return None
    return dot / ((left_mag * right_mag) ** 0.5)


def signed_number(value: float) -> str:
    if value > 0:
        return f"+{value:g}"
    if NumberIsInteger(value):
        return str(int(value))
    return f"{value:.2f}"


def NumberIsInteger(value: float) -> bool:
    return abs(value - round(value)) < 1e-9


# ------------------------------------------------------------------ user turn flow

def should_supersede_narrative_request(
    in_flight_request_id: Optional[int],
    first_message_committed_request_id: Optional[int],
    obsolete_request_ids: set[int],
) -> bool:
    return bool(
        in_flight_request_id is not None
        and first_message_committed_request_id != in_flight_request_id
        and in_flight_request_id not in obsolete_request_ids
    )


def _cancel_timer(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done():
        task.cancel()


def format_buffered_user_messages(messages: list[BufferedUserMessage]) -> str:
    if len(messages) == 1:
        return messages[0].content
    parts = []
    for index, message in enumerate(messages):
        time_text = iso(message.occurred_at)
        parts.append(f"[连续消息 {index + 1}，收到时间 {time_text}]\n{message.content}")
    return "\n\n".join(parts)


def _invalidate_turn(turn: BufferedNarrativeTurn) -> None:
    _cancel_timer(turn.timer_task)
    turn.timer_task = None
    if turn.in_flight_request_id is not None:
        turn.obsolete_request_ids.add(turn.in_flight_request_id)


def _service_invalidate_buffered(self: "InterludeService", story_id: Optional[str] = None) -> None:
    """Synchronously invalidate buffered turns and wake timers.

    Deliberately a plain function (no awaits): administrators calling
    clear/purge must invalidate in the SAME event-loop step, and every
    call site runs it un-awaited on purpose.
    """
    for key in list(self.buffered_turns):
        turn = self.buffered_turns[key]
        if story_id and turn.story_id != story_id:
            continue
        _invalidate_turn(turn)
        self.buffered_turns.pop(key, None)
    for key in list(self.buffered_group_turns):
        turn = self.buffered_group_turns[key]
        if story_id and turn.story_id != story_id:
            continue
        _cancel_timer(turn.timer_task)
        self.buffered_group_turns.pop(key, None)
    for key in list(self.due_wake_tasks):
        if story_id and key != story_id:
            continue
        task = self.due_wake_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        self.due_wake_at.pop(key, None)


InterludeService.invalidate_buffered_narratives = _service_invalidate_buffered  # type: ignore[attr-defined]


def _signal_incoming_interruption(self: "InterludeService", story: InterludeStory, participant: InterludeParticipant) -> None:
    self.interrupted_typing_participants.add(participant.id)
    turn = self.buffered_turns.get(participant.id)
    if turn is None:
        return
    if not should_supersede_narrative_request(
        turn.in_flight_request_id,
        turn.first_message_committed_request_id,
        turn.obsolete_request_ids,
    ):
        return
    turn.obsolete_request_ids.add(turn.in_flight_request_id)  # type: ignore[arg-type]
    self.report_operation(
        "standard", "info", story, "user-message",
        "新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d",
        participant.id, turn.in_flight_request_id,
    )


InterludeService.signal_incoming_interruption = _signal_incoming_interruption  # type: ignore[attr-defined]


def _has_pending_narrative(self: "InterludeService", story_id: str) -> bool:
    if story_id in self.narrating_stories:
        return True
    for turn in self.buffered_turns.values():
        if turn.story_id == story_id and (turn.messages or turn.timer_task or turn.in_flight_request_id):
            return True
    for turn in self.buffered_group_turns.values():
        if turn.story_id == story_id and (turn.messages or turn.timer_task):
            return True
    return False


InterludeService.has_pending_narrative = _has_pending_narrative  # type: ignore[attr-defined]


def _buffer_user_narrative(
    self: "InterludeService",
    story: InterludeStory,
    participant: InterludeParticipant,
    event: IncomingEvent,
    now: datetime,
    superseded_intents: list[NarrativeIntent],
) -> None:
    key = participant.id
    existing = self.buffered_turns.get(key)
    turn = existing or BufferedNarrativeTurn(story_id=story.id, participant_id=participant.id)
    if should_supersede_narrative_request(
        turn.in_flight_request_id,
        turn.first_message_committed_request_id,
        turn.obsolete_request_ids,
    ):
        turn.obsolete_request_ids.add(turn.in_flight_request_id)  # type: ignore[arg-type]
        self.report_operation(
            "standard", "info", story, "user-message",
            "新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d",
            participant.id, turn.in_flight_request_id,
        )
    turn.messages.append(BufferedUserMessage(
        content=event.content,
        occurred_at=now,
        superseded_intents=superseded_intents,
        image_sources=list(event.image_sources),
    ))
    turn.latest_event = event
    _cancel_timer(turn.timer_task)
    revision = turn.next_revision = turn.next_revision + 1
    delay = max(0.0, float(self.config.runtime.user_message_debounce_seconds))
    loop = asyncio.get_running_loop()

    async def fire() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.flush_buffered_narrative(key, revision)
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001
            logger.exception("合并写作任务失败：%s", error)

    turn.timer_task = loop.create_task(fire())
    self.buffered_turns[key] = turn
    self.report_operation("diagnostic", "debug", story, "user-message",
                          "短时消息合并 参与者=%s 待处理=%d 等待=%dms",
                          participant.id, len(turn.messages), delay * 1000)


InterludeService.buffer_user_narrative = _buffer_user_narrative  # type: ignore[attr-defined]


async def _flush_buffered_narrative(self: "InterludeService", key: str, revision: int) -> None:
    if self.database_resetting:
        return
    turn = self.buffered_turns.get(key)
    if turn is None or turn.next_revision != revision:
        return
    if story_busy := (turn.story_id in self.narrating_stories):
        loop = asyncio.get_running_loop()
        turn.timer_task = loop.create_task(self._retry_flush_later(key, revision))
        return
    self.narrating_stories.add(turn.story_id)
    turn.timer_task = None
    batch = turn.messages
    turn.messages = []
    if not batch:
        self.narrating_stories.discard(turn.story_id)
        return
    request_id = revision
    turn.in_flight_request_id = request_id
    try:
        snapshot = await self.queues.run(turn.story_id, lambda: self._snapshot_user_turn(turn))
        if snapshot is None:
            return
        story, participant, from_time, snapshot_now, due = snapshot
        user_message = format_buffered_user_messages(batch)
        image_sources: list[str] = []
        for message in batch:
            for source in message.image_sources:
                if source not in image_sources:
                    image_sources.append(source)
        image_sources = image_sources[:3]
        images = await self.load_native_images(image_sources)
        if turn.next_revision != revision:
            # A newer message arrived while images were loading; recombine.
            turn.messages = batch + turn.messages
            return
        superseded = [intent for message in batch for intent in message.superseded_intents]
        outcome = await self.try_decide(
            story, participant, NarrativePhase.USER_MESSAGE, from_time, snapshot_now,
            user_message=user_message, due_intents=due, superseded_intents=superseded,
            images=images,
        )
        decision = outcome["decision_raw"]
        succeeded = outcome["succeeded"]
        effective_now = outcome["effective_now"]
        immediate_observations = outcome["immediate_observations"]

        result = await self.queues.run(turn.story_id, lambda: self._persist_user_turn(
            turn, request_id, batch, decision, succeeded, effective_now,
            from_time, snapshot_now, due, immediate_observations,
        ))
        if result["obsolete"]:
            if result["requeue"]:
                turn.messages = batch + turn.messages
            self.report_operation("standard", "info", story, "user-message",
                                  "已丢弃过期主模型结果 参与者=%s 请求=%d",
                                  participant.id, request_id)
            return
        messages: list[OutgoingMessageDraft] = result["messages"]
        if self.can_handle_participant(participant):
            # First-reply commit boundary = the moment transport begins.
            # A sent bubble can never be retracted by later input; anything
            # still staged will be cancelled instead of delivered.
            if any(m.delivery_intent_id for m in messages):
                turn.first_message_committed_request_id = request_id
            await self.send_outgoing_messages(story, messages, current=participant)
        else:
            await self.cancel_undelivered_messages(story, messages, self.now())
        self.schedule_compaction(turn.story_id)
    except Exception as error:  # noqa: BLE001
        self.report_standalone("warn", "合并写作任务失败：参与者=%s 错误=%s", turn.participant_id, error)
    finally:
        if turn.in_flight_request_id == request_id:
            turn.in_flight_request_id = None
            turn.first_message_committed_request_id = None
            self.narrating_stories.discard(turn.story_id)
        turn.obsolete_request_ids.discard(request_id)
        if not turn.messages and turn.timer_task is None and turn.in_flight_request_id is None:
            self.buffered_turns.pop(key, None)


async def _retry_flush_later(self: "InterludeService", key: str, revision: int,
                             delay: float | None = None) -> None:
    try:
        wait = self.story_busy_retry_delay_seconds if delay is None else delay
        if wait > 0:
            await asyncio.sleep(wait)
        else:
            await asyncio.sleep(0)
        await self.flush_buffered_narrative(key, revision)
    except asyncio.CancelledError:
        pass


InterludeService._retry_flush_later = _retry_flush_later  # type: ignore[attr-defined]
InterludeService.flush_buffered_narrative = _flush_buffered_narrative  # type: ignore[attr-defined]


async def _snapshot_user_turn(self: "InterludeService", turn: BufferedNarrativeTurn):
    rows = await self.db.get("interlude_story", {"id": turn.story_id})
    if not rows:
        return None
    story = InterludeStory.model_validate(rows[0])
    participant = await self.get_participant(turn.participant_id)
    if participant is None or participant.status != "active" or story.status != "active":
        return None
    now = self.now()
    due = await self.due_intents(story.id, now)
    due = [
        intent for intent in due
        if not intent.participant_id or intent.participant_id == participant.id
    ]
    return story, participant, narrative_cursor(story, now), now, due


InterludeService._snapshot_user_turn = _snapshot_user_turn  # type: ignore[attr-defined]


async def _persist_user_turn(
    self: "InterludeService",
    turn: BufferedNarrativeTurn,
    request_id: int,
    batch: list[BufferedUserMessage],
    decision: NarrativeDecision,
    succeeded: bool,
    effective_now: datetime,
    from_time: datetime,
    snapshot_now: datetime,
    due: list[NarrativeIntent],
    immediate_observations: list[WebObservation],
) -> dict[str, Any]:
    empty: list[OutgoingMessageDraft] = []
    if self.database_resetting:
        return {"obsolete": True, "requeue": False, "messages": empty}
    if request_id in turn.obsolete_request_ids:
        return {"obsolete": True, "requeue": True, "messages": empty}
    rows = await self.db.get("interlude_story", {"id": turn.story_id})
    if not rows:
        return {"obsolete": True, "requeue": False, "messages": empty}
    current = InterludeStory.model_validate(rows[0])
    current_participant = await self.get_participant(turn.participant_id)
    if (
        current_participant is None
        or current_participant.status != "active"
        or current.status != "active"
    ):
        return {"obsolete": True, "requeue": False, "messages": empty}
    now = self.now()
    # Persist successful/failed immediate web observations only after this
    # request survives debounce invalidation.
    for observation in immediate_observations:
        await self.persist_collected_web_observation(observation)
    raw_interaction = decision.get("interaction") if isinstance(decision, dict) else None
    raw_reply = raw_interaction.get("reply") if isinstance(raw_interaction, dict) else {}
    messages = await self.persist_decision(
        current, current_participant, decision, from_time, effective_now,
        permit_messages=True, phase=NarrativePhase.USER_MESSAGE.value,
    )
    # P0-D: flip staged rows to `sending` while still holding the story
    # queue. After this point a concurrent incoming message can no longer
    # cancel them — transport begins the moment the queue is released.
    await self._mark_intent_sending(
        [m.delivery_intent_id for m in messages if m.delivery_intent_id]
    )
    if succeeded:
        await self.db.update("interlude_story", {"id": current.id}, {
            "cursor_at": iso(effective_now), "updated_at": iso(now),
        })
        if due:
            await self.db.execute_many([
                ("UPDATE interlude_intent SET status='completed', updated_at=? WHERE id=?",
                 (iso(now), intent.id)) for intent in due
            ])
    else:
        await self.schedule_narrative_retry(current.id, current_participant.id, now)
    if succeeded:
        await self.schedule_conversation_follow_ups_after_turn(
            current.id, effective_now, raw_interaction, current_participant.id,
        )
    self.report_operation("diagnostic", "debug", current, "user-message",
                          "写作回合统计 参与者=%s 合并消息=%d 成功=%s 可见消息=%d",
                          current_participant.id, len(batch), succeeded, len(messages))
    return {"obsolete": False, "requeue": False, "messages": messages}


InterludeService._persist_user_turn = _persist_user_turn  # type: ignore[attr-defined]


# ------------------------------------------------------------------ decide

async def _load_native_images(self: "InterludeService", sources: list[str]) -> list[Any]:
    """Convert stored image sources into data URIs via the injected loader."""
    from .types import NarrativeImage

    if not self.config.models.vision_enabled or not sources or self.image_loader is None:
        return []
    images: list[NarrativeImage] = []
    for index, source in enumerate(sources[:3]):
        try:
            result = await self.image_loader(source)
            if result is None:
                continue
            mime_type, data_uri = result
            images.append(NarrativeImage(
                id=f"turn-image-{index + 1}", mime_type=mime_type, data_uri=data_uri,
            ))
        except Exception as error:  # noqa: BLE001
            self.report_standalone("warn", "图片读取失败，已继续处理文字消息 错误=%s", error)
    return images


InterludeService.load_native_images = _load_native_images  # type: ignore[attr-defined]


def _should_refresh_continuity(self: "InterludeService", story: InterludeStory, phase: str) -> bool:
    state = normalize_story_state(story.state)
    if phase == "advance" and state.continuity_snapshot is None:
        return True
    count = max(0, int(state.narrative_update_count or 0))
    return (count + 1) % 15 == 0


InterludeService._should_refresh_continuity = _should_refresh_continuity  # type: ignore[attr-defined]


def _emotional_offset_for_prompt(self: "InterludeService", story: InterludeStory):
    return alter_mod.emotional_offset_for_prompt(story.state.alter_system, self._alter_config_dict())


def _alter_config_dict(self: "InterludeService") -> dict[str, Any]:
    cfg = self.config.alter_system
    return {
        "enabled": cfg.enabled,
        "base_threshold": cfg.base_threshold,
        "density_factor": cfg.density_factor,
        "same_direction_boost": cfg.same_direction_boost,
        "opposite_decay": cfg.opposite_decay,
        "min_weight": cfg.min_weight,
        "max_intensity": cfg.max_intensity,
    }


InterludeService._alter_config_dict = _alter_config_dict  # type: ignore[attr-defined]
InterludeService._emotional_offset_for_prompt = _emotional_offset_for_prompt  # type: ignore[attr-defined]


async def _decide(
    self: "InterludeService",
    story: InterludeStory,
    participant: Optional[InterludeParticipant],
    phase: str,
    from_time: datetime,
    now: datetime,
    user_message: Optional[str],
    due_intents: list[NarrativeIntent],
    superseded_intents: Optional[list[NarrativeIntent]] = None,
    group_context=None,
    images=None,
) -> NarrativeRequest:
    """Build the one and only main-model context for this turn."""
    from .types import GroupContext as GC

    superseded_intents = superseded_intents or []
    images = images or []
    await self.expire_active_consequences(story.id, now)

    # P0-C: explicit privacy scopes. Unattended life turns are GLOBAL_ONLY —
    # they must never read any branch's private memories/facts/consequences,
    # because anything the model reads here can legally be promoted into the
    # GLOBAL continuity and then reaches every other participant.
    #   global_only   -> advance
    #   participant   -> user-message / intent-due / conversation-follow-up
    # (ALL_PRIVATE exists only for explicit admin tooling; no narrative path
    #  uses it.)
    global_only = phase == "advance"
    share_details_flag = self.config.shared_story.share_participant_details
    memory_scope = "" if (global_only and not share_details_flag) else (
        participant.id if participant else None
    )
    fact_scope = "" if (global_only and not share_details_flag) else (
        participant.id if participant else None
    )

    fact_query = create_fact_query(participant, user_message, due_intents, superseded_intents)

    recent_entries_task = self.recent_entries(story.id, self.config.runtime.context_entry_limit)
    memories_task = self.memories(story.id, self.config.runtime.memory_limit, memory_scope)
    scene_task = self.active_scene(story.id)
    arc_task = self.active_arc(story.id)
    facts_task = self.facts(story.id, self.config.runtime.memory_limit, fact_query, fact_scope)
    participants_task = self.participants(story.id)
    web_task = self.web_observations(story.id, participant.id if participant else None)
    share_details_flag = self.config.shared_story.share_participant_details
    consequences_task = self.active_consequences(
        story.id, now,
        None if (global_only or share_details_flag)
        else (participant.id if participant else None),
    )
    overlay_task = self.overlay_snapshots_for_prompt(
        story.id, participant.id if participant else None, phase == "advance",
    )
    (
        recent_entries, memories, scene, arc, facts, all_participants,
        web_context, active_consequences, overlay_snapshots,
    ) = await asyncio.gather(
        recent_entries_task, memories_task, scene_task, arc_task, facts_task,
        participants_task, web_task, consequences_task, overlay_task,
    )
    share_details = share_details_flag

    # Defense-in-depth post-filter for the GLOBAL_ONLY scope: even if a
    # repository query later changes, private rows can never reach an
    # unattended prompt.
    if global_only and not share_details:
        memories = [m for m in memories if not m.participant_id]
        facts = [f for f in facts if not f.participant_id]
        active_consequences = [c for c in active_consequences if not c.participant_id]

    visible_entries = (
        recent_entries if share_details
        else [
            e for e in recent_entries
            if (group_context is not None or e.kind not in ("group-message", "character-group-message"))
            and (not e.participant_id or e.participant_id == (participant.id if participant else None))
        ]
    )
    if phase == "advance":
        turn_entries = [
            e for e in visible_entries
            if e.kind not in ("user-message", "character-message", "group-message", "character-group-message")
        ]
    else:
        turn_entries = visible_entries
    prompt_entries = [e for e in turn_entries if e.content.strip()]

    other_participants = [
        p for p in all_participants
        if p.id != (participant.id if participant else "") and self.can_handle_participant(p)
    ]
    def relevance(p: InterludeParticipant) -> float:
        st = normalize_participant_state(p.state)
        pending = st.pending_reply_count * 2 + st.unread_message_count
        last = parse_date(st.last_user_message_at)
        ts = last.timestamp() if last else p.updated_at.timestamp()
        return pending * 1_000_000_000 + ts
    other_participants.sort(key=relevance, reverse=True)
    other_participants = other_participants[: self.config.shared_story.participant_context_limit]

    agency_enabled = bool(
        self.config.agency.enabled
        and self.config.runtime.allow_proactive_messages
        and (
            phase == "advance"
            or (phase == "intent-due" and any(i.type == "proactive-check" for i in due_intents))
        )
    )
    advance_can_contact = phase == "advance" and self.config.runtime.allow_proactive_messages
    if share_details:
        visible_due_intents = list(due_intents)
    elif global_only:
        visible_due_intents = [i for i in due_intents if not i.participant_id]
    else:
        visible_due_intents = [
            i for i in due_intents
            if not i.participant_id or i.participant_id == (participant.id if participant else None)
        ]
    visible_consequences = (
        active_consequences if (phase == "advance" or share_details)
        else [i for i in active_consequences if not i.participant_id or i.participant_id == (participant.id if participant else None)]
    )
    extra_web = getattr(self, "_extra_web_context", [])
    merged_web = sorted(
        [o for o in [*web_context, *extra_web] if o.status != "deleted"],
        key=lambda o: o.accessed_at,
    )[-max(1, self.config.browser.max_observations_in_prompt):]

    refresh_continuity = self._should_refresh_continuity(story, phase)
    request = NarrativeRequest(
        phase=NarrativePhase(phase),
        refresh_continuity=refresh_continuity,
        story=story,
        from_time=from_time,
        now=now,
        user_message=user_message,
        images=images,
        participant=None if phase == "advance" else participant,
        participants=[] if (phase == "advance" and not advance_can_contact) else other_participants,
        share_participant_details=share_details,
        due_intents=visible_due_intents,
        active_consequences=visible_consequences,
        superseded_intents=superseded_intents,
        recent_entries=prompt_entries,
        memories=memories,
        scene_context=SceneContext(scene=scene, arc=arc),
        facts=facts,
        group_context=group_context,
        web_context=merged_web,
        overlay_snapshots=overlay_snapshots,
        alter_enabled=self.config.alter_system.enabled,
        emotional_offset=self._emotional_offset_for_prompt(story),
        agency_enabled=agency_enabled,
        agency_window=(
            agency_mod.active_agency_window(story.state.agency_window, now) if agency_enabled else None
        ),
    )
    return request


InterludeService._build_request = _decide  # type: ignore[attr-defined]


async def _try_decide(
    self: "InterludeService",
    story: InterludeStory,
    participant: Optional[InterludeParticipant],
    phase: str,
    from_time: datetime,
    now: datetime,
    user_message: Optional[str] = None,
    due_intents: Optional[list[NarrativeIntent]] = None,
    superseded_intents: Optional[list[NarrativeIntent]] = None,
    group_context=None,
    images=None,
) -> dict[str, Any]:
    """Run the narrator with immediate-browser handling; never raises."""
    from .prompt_builder import build_prompt_payload, system_prompt
    from .config import Prompts

    due_intents = due_intents or []
    superseded_intents = superseded_intents or []
    started = self.now()
    prompts = self.config.prompts
    self.report_operation("standard", "info", story, phase,
                          "模型调用开始 任务=主叙事 参与者=%s 时间段=%s→%s 到期计划=%d",
                          participant.id if participant else "全局",
                          format_log_time(from_time, story.setting.timezone),
                          format_log_time(now, story.setting.timezone), len(due_intents))
    try:
        effective_now = now
        immediate_observations: list[WebObservation] = []

        async def run_once(now_value: datetime, extra_web: list[WebObservation]) -> tuple[dict[str, Any], NarrativeRequest]:
            request = await self._build_request(
                story, participant, phase, from_time, now_value, user_message,
                due_intents, superseded_intents, group_context, images,
            )
            self._extra_web_context = extra_web
            try:
                payload = build_prompt_payload(request)
                system_text = system_prompt(
                    phase=request.phase.value,
                    main_prompt=prompts.main_prompt,
                    format_prompt=prompts.format_prompt,
                    fixed_prompt=prompts.fixed_prompt,
                    base_style_prompt=prompts.style_prompt,
                    story_style_prompt=story.setting.style,
                    refresh_continuity=request.refresh_continuity,
                    alter_enabled=request.alter_enabled,
                    agency_enabled=request.agency_enabled,
                )
                image_urls = [img.data_uri for img in request.images]
                raw, _attempts = await self.narrator.decide_raw(
                    request,
                    system_prompt=system_text,
                    temperature=self.config.models.main_temperature,
                    top_p=self.config.models.main_top_p,
                    max_tokens=self.config.models.main_max_tokens,
                    timeout_seconds=(self.config.models.main_timeout_ms / 1000.0),
                    response_json=True,
                )
            finally:
                self._extra_web_context = []
            return raw, request

        raw, request = await run_once(effective_now, [])

        # Immediate browser observation (opt-in mode).
        immediate_intent = None
        if (
            phase == "user-message"
            and participant is not None
            and group_context is None
            and self.config.browser.enabled
            and self.config.browser.mode == "allow-immediate"
        ):
            candidates = [
                normalize_browser_intent(item, self.config.browser)
                for item in _raw_browser_intents(raw)
            ]
            candidates = [c for c in candidates if c is not None and c.get("timing") == "immediate"]
            immediate_intent = candidates[0] if candidates else None
        if immediate_intent:
            self.report_operation("standard", "info", story, phase,
                                  "即时网页观察开始 模式=%s", immediate_intent.get("mode"))
            observation = await self.collect_web_observation(
                story, BrowserIntentDraft(**{
                    "mode": immediate_intent["mode"],
                    "query": immediate_intent.get("query"),
                    "url": immediate_intent.get("url"),
                    "purpose": immediate_intent["purpose"],
                    "timing": "immediate",
                }),
                participant.id, None, self.now(), persist=False,
            )
            immediate_observations = [observation]
            effective_now = self.now()
            raw, request = await run_once(effective_now, immediate_observations)

        duration_ms = round((self.now() - started).total_seconds() * 1000)
        script_len = len(str(raw.get("script") or ""))
        reply_mode = "?"
        interaction_raw = raw.get("interaction")
        if isinstance(interaction_raw, dict):
            reply = interaction_raw.get("reply")
            reply_mode = reply.get("mode") if isinstance(reply, dict) else "?"
        self.report_operation("standard", "info", story, phase,
                              "模型调用完成 任务=主叙事 耗时=%dms 剧本文字=%d 回复模式=%s",
                              duration_ms, script_len, reply_mode)
        if self.config.logging.log_script_preview and raw.get("script"):
            self.write_report("info", story.setting.character.name, phase,
                              "当前剧本内容：\n%s",
                              (str(raw.get("script")))[: self.config.logging.preview_length])
        return {
            "decision_raw": raw,
            "decision": None,
            "succeeded": True,
            "effective_now": effective_now,
            "immediate_observations": immediate_observations,
            "request": request,
        }
    except Exception as error:  # noqa: BLE001
        duration_ms = round((self.now() - started).total_seconds() * 1000)
        self.write_report("warn", story.setting.character.name, phase,
                          "模型调用失败 任务=主叙事 耗时=%dms 错误=%s", (duration_ms, error))
        return {
            "decision_raw": {},
            "decision": None,
            "succeeded": False,
            "effective_now": now,
            "immediate_observations": [],
            "request": None,
        }


InterludeService.try_decide = _try_decide  # type: ignore[attr-defined]


def _raw_browser_intents(raw: dict[str, Any]) -> list[Any]:
    value = raw.get("browserIntents")
    return value if isinstance(value, list) else []


def create_fact_query(participant, user_message, due_intents, superseded_intents) -> str:
    parts: list[str] = []
    if user_message:
        parts.append(f"Current user message: {user_message}")
    if participant is not None:
        state = normalize_participant_state(participant.state)
        for thread in state.open_threads:
            parts.append(f"Open thread: {thread}")
        for note in state.relationship_notes:
            parts.append(f"Relationship note: {note}")
    for intent in due_intents:
        parts.append(f"Due intent: {intent.summary}")
    for intent in superseded_intents:
        parts.append(f"Superseded plan: {intent.summary}")
    return "\n".join(parts)


async def _expire_active_consequences(self: "InterludeService", story_id: str, now: datetime) -> None:
    if not self.config.memory.active_consequences_enabled:
        return
    rows = await self.db.get(
        "interlude_intent", {"story_id": story_id, "status": "pending"},
        limit=100, order_by="updated_at",
    )
    intents = [NarrativeIntent.model_validate(r) for r in rows]
    expired = [
        i for i in intents
        if is_active_consequence(i)
        and ((consequence_expires_at(i.payload) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp() <= now.timestamp())
    ]
    if expired:
        await self.db.execute_many([
            ("UPDATE interlude_intent SET status='completed', updated_at=? WHERE id=?",
             (iso(now), intent.id)) for intent in expired
        ])


InterludeService.expire_active_consequences = _expire_active_consequences  # type: ignore[attr-defined]


async def _active_consequences(
    self: "InterludeService", story_id: str, now: datetime, participant_id: str | None = None
) -> list[NarrativeIntent]:
    if not self.config.memory.active_consequences_enabled:
        return []
    rows = await self.db.get(
        "interlude_intent", {"story_id": story_id, "status": "pending"},
        limit=100, order_by="updated_at", descending=True,
    )
    intents = [NarrativeIntent.model_validate(r) for r in rows]
    out = []
    for intent in intents:
        if not is_active_consequence(intent):
            continue
        if intent.not_before > now:
            continue
        expires_at = consequence_expires_at(intent.payload)
        if expires_at is None or expires_at <= now:
            continue
        if participant_id is not None and intent.participant_id and intent.participant_id != participant_id:
            continue
        out.append(intent)
    out.sort(key=lambda i: (-consequence_strength(i.payload), -i.updated_at.timestamp()))
    return out[: max(1, self.config.memory.active_consequence_prompt_limit)]


InterludeService.active_consequences = _active_consequences  # type: ignore[attr-defined]


# ------------------------------------------------------------------ persistence

async def _persist_decision(
    self: "InterludeService",
    story: InterludeStory,
    participant: Optional[InterludeParticipant],
    raw: dict[str, Any] | NarrativeDecision,
    from_time: datetime,
    now: datetime,
    permit_messages: bool,
    phase: str,
    context_intents: Optional[list[NarrativeIntent]] = None,
) -> list[OutgoingMessageDraft]:
    """Normalize, persist and stage delivery for one model decision."""
    from .normalize import normalize_decision
    from .types import ConversationActionDraft

    context_intents = context_intents or []
    if isinstance(raw, NarrativeDecision):
        decision = raw
        agency_window_raw = decision.agency_window
        proactive_raw = decision.proactive_contact
    else:
        all_participants = await self.participants(story.id)
        permitted = {
            p.id for p in all_participants if self.can_handle_participant(p)
        }
        memory_dict = self.config.memory.model_dump()
        refresh = self._should_refresh_continuity(story, phase)
        runtime_dict = {
            "max_script_characters": self.config.runtime.max_script_characters,
            "max_message_characters": self.config.runtime.max_message_characters,
            "minimum_delayed_reply_seconds": self.config.runtime.minimum_delayed_reply_seconds,
            "maximum_delayed_reply_minutes": self.config.runtime.maximum_delayed_reply_minutes,
            "proactive_willingness_threshold": self.config.runtime.proactive_willingness_threshold,
        }
        shared_dict = self.config.shared_story.model_dump()
        decision, agency_window_raw, proactive_raw = normalize_decision(
            raw, from_time, now, permit_messages, runtime_dict, shared_dict,
            participant.id if participant else "", permitted, phase,
            memory=memory_dict, refresh_continuity=refresh,
        )

    script_entry: Optional[ScriptEntry] = None
    if decision.script:
        script_entry = await self.append_entry(story.id, ScriptEntryDraft(
            kind="script", actor="narrator", content=decision.script,
            occurred_at=iso(now),
            metadata={
                "phase": phase,
                "interaction": decision.interaction.model_dump() if decision.interaction else None,
            },
        ), now, participant.id if participant else "")

    await self.apply_intent_updates(story.id, decision.intent_updates, now,
                                    participant.id if participant else None)
    for memory in decision.memories:
        await self.append_memory(story.id, memory, now,
                                 memory.participant_id or (participant.id if participant else "") or "")
    for intent in decision.intents:
        payload = intent.payload or {}
        if phase == "user-message" and participant is not None:
            payload = {**payload, "userInitiated": payload.get("userInitiated") is not False}
        await self.append_intent(story.id, {
            "type": intent.type,
            "summary": intent.summary,
            "not_before": intent.not_before,
            "payload": payload,
            "participant_id": intent.participant_id or (participant.id if participant else "") or "",
        }, now)

    for browser_intent in decision.browser_intents:
        if (
            participant is not None
            or phase != "user-message"
            or self.config.browser.allow_group_triggered_research
        ):
            await self.append_browser_intent(
                story.id, browser_intent, now, participant.id if participant else "",
            )
    if participant is not None and decision.state_patch:
        await self.update_participant_state(participant, decision.state_patch, now)

    is_agency_check = bool(context_intents) and all(i.type == "proactive-check" for i in context_intents)
    agency_candidate: Optional[ProactiveContactDraft] = None
    agency_allows_send = False
    agency_recheck: Optional[tuple[ProactiveContactDraft, AgencyWindowState, str, datetime]] = None

    if decision.script:
        state = normalize_story_state(story.state)
        next_count = max(0, int(state.narrative_update_count or 0)) + 1
        next_state = state.model_copy(update={"narrative_update_count": next_count})
        if decision.continuity is not None:
            # P0-5 privacy boundary: participant-scoped refreshes write ONLY
            # that branch's private continuity; the GLOBAL snapshot is
            # refreshed exclusively by unattended life turns, so raw private
            # conversation can never reach another participant's prompt.
            if phase != "advance" and participant is not None:
                pc = dict(next_state.participant_continuity or {})
                pc[participant.id] = decision.continuity
                next_state.participant_continuity = pc
                next_state.last_continuity_update_at = iso(now)
            else:
                next_state.continuity_snapshot = decision.continuity
                next_state.last_continuity_update_at = iso(now)
        alter_turn = self._update_alter_system(story, state.alter_system, decision.alter, phase, now)
        next_state.alter_system = alter_turn.state if alter_turn is not None else state.alter_system

        threshold_reached = bool(alter_turn and alter_turn.threshold_reached)

        if self.config.agency.enabled and (phase == "advance" or is_agency_check):
            source_entries: list[ScriptEntry] = []
            if agency_window_raw is not None or proactive_raw is not None:
                source_entries = await self.recent_entries(
                    story.id, max(40, self.config.runtime.context_entry_limit * 2)
                )
            valid_source_ids = {e.id for e in source_entries}
            if script_entry is not None and script_entry.id:
                valid_source_ids.add(script_entry.id)
            fallback_source = script_entry.id if script_entry is not None else None
            agency_window = agency_mod.normalize_agency_window_draft(
                agency_window_raw, now, self.agency_config(),
                frozenset(valid_source_ids), fallback_source,
            ) or agency_mod.active_agency_window(state.agency_window, now)
            next_state.agency_window = agency_window
            agency_candidate = agency_mod.normalize_proactive_contact(
                proactive_raw, now, self.agency_config(),
                frozenset({p.id for p in await self.participants(story.id) if self.can_handle_participant(p)}),
                frozenset(valid_source_ids), fallback_source,
            ) if proactive_raw is not None else None

            if is_agency_check and agency_candidate is not None and participant is not None \
                    and agency_candidate.participant_id != participant.id:
                agency_candidate = None
            if agency_candidate is not None and agency_window is not None:
                target = next((p for p in await self.participants(story.id)
                               if p.id == agency_candidate.participant_id), None)
                capacity = agency_mod.evaluate_agency_capacity(
                    agency_window, agency_candidate, now, self.agency_config(),
                    target.state.last_character_message_at if target else None,
                )
                willingness = agency_candidate.willingness if agency_candidate.willingness is not None else 0.0
                willingness_passes = willingness >= self.config.runtime.proactive_willingness_threshold
                agency_allows_send = (
                    agency_candidate.outcome == "send-now"
                    and capacity.allowed
                    and willingness_passes
                )
                if not agency_allows_send and agency_candidate.outcome != "let-go" and willingness_passes:
                    agency_recheck = (
                        agency_candidate, agency_window,
                        capacity.reason if not capacity.allowed else "model-requested-recheck",
                        agency_mod.proactive_recheck_at(agency_candidate, capacity, agency_window, now),
                    )
                self.report_operation("standard", "info", story, phase,
                                      "Agency 主动联系判断 参与者=%s 结果=%s 原因=%s 意愿=%s",
                                      agency_candidate.participant_id,
                                      "立即联系" if agency_allows_send else ("稍后重查" if agency_recheck else "自然放下"),
                                      capacity.reason, f"{willingness:.2f}")
            if agency_window is not None:
                self.report_operation("diagnostic", "debug", story, phase,
                                      "Agency Window 更新 负荷=%s 隐私=%s 设备=%s 有效至=%s",
                                      agency_window.activity_load, agency_window.privacy,
                                      agency_window.device_access,
                                      format_log_time(parse_date(agency_window.valid_until),
                                                      story.setting.timezone))

        await self.db.update("interlude_story", {"id": story.id}, {
            "state": next_state.model_dump(mode="json"), "updated_at": iso(now),
        })
        if threshold_reached:
            self.schedule_alter_analysis(story.id, phase, participant.id if participant else "")

    if agency_recheck is not None:
        candidate, window, reason, at = agency_recheck
        await self.append_proactive_check(story, candidate, at, reason, now)

    messages: list[OutgoingMessageDraft] = []
    interaction = decision.interaction
    if is_agency_check:
        interaction = interaction if (
            agency_allows_send and interaction is not None and interaction.reply.mode == "immediate"
        ) else None
    if participant is not None and not is_agency_check and interaction is not None and interaction.seen:
        await self.mark_participant_seen(participant, now)
    if (
        participant is not None
        and permit_messages
        and interaction is not None
        and interaction.reply.mode == "immediate"
        and interaction.reply.content
    ):
        messages.append(OutgoingMessageDraft(participant_id=participant.id, content=interaction.reply.content))
    if (
        participant is not None
        and permit_messages
        and interaction is not None
        and interaction.reply.mode == "delayed"
        and interaction.reply.content
        and interaction.reply.send_at
    ):
        send_at = parse_date(interaction.reply.send_at)
        await self.append_intent(story.id, {
            "type": "delayed-reply",
            "summary": "The character decided to send a delayed reply.",
            "not_before": iso(send_at),
            "payload": {
                "content": interaction.reply.content,
                "userInitiated": phase == "user-message",
                "interaction": True,
            },
        }, now, participant.id)
        if send_at is not None:
            self.schedule_due_intent_wake(story.id, send_at)

    # Cross-account actions.
    cross_actions: list[ConversationActionDraft] = []
    if phase == "user-message":
        cross_actions = list(decision.cross_conversation_actions)
    elif phase == "advance":
        if not self.config.agency.enabled and self.config.runtime.allow_proactive_messages:
            cross_actions = list(decision.cross_conversation_actions)
        elif agency_allows_send and agency_candidate is not None:
            cross_actions = [
                a for a in decision.cross_conversation_actions
                if a.participant_id == agency_candidate.participant_id and a.mode == "immediate"
            ][:1]
    if phase == "advance" and decision.cross_conversation_actions and not cross_actions:
        self.report_operation("diagnostic", "debug", story, phase,
                              "Agency 拒绝未通过容量或来源验证的 crossConversationAction 数量=%d",
                              len(decision.cross_conversation_actions))
    for action in cross_actions:
        if action.mode == "immediate":
            messages.append(OutgoingMessageDraft(participant_id=action.participant_id, content=action.content))
        else:
            if not action.send_at:
                continue
            send_at_value = parse_date(action.send_at)
            if send_at_value is None:
                continue
            await self.append_intent(story.id, {
                "type": "cross-conversation-message",
                "summary": "The character planned a message to another relationship branch.",
                "not_before": iso(send_at_value),
                "payload": {
                    "content": action.content,
                    "userInitiated": False,
                    "crossConversation": True,
                    "willingness": action.willingness,
                    "reason": action.reason,
                },
            }, now, action.participant_id)
            self.schedule_due_intent_wake(story.id, send_at_value)

    for message in messages:
        first, later = self.split_outgoing_message(message.content)
        if not first:
            continue
        message.content = first
        # P0-1 delivery boundary: stage the message as a pending outbound
        # intent. The visible character-message ScriptEntry and
        # lastCharacterMessageAt are written ONLY after the transport layer
        # confirms real delivery (finalize); on failure the staging intent is
        # cancelled and nothing was "said".
        baseline_state = None
        if participant is not None and participant.id == message.participant_id:
            baseline_state = normalize_participant_state(participant.state)
        else:
            staged_target = await self.get_participant(message.participant_id)
            if staged_target is not None:
                baseline_state = normalize_participant_state(staged_target.state)
        message.baseline_unread = baseline_state.unread_message_count if baseline_state else 0
        message.baseline_pending = baseline_state.pending_reply_count if baseline_state else 0
        message.delivery_intent_id = await self.stage_outbound_message(
            story.id, message.participant_id, first, now,
            interaction=interaction.model_dump() if interaction else None,
            user_initiated=phase == "user-message",
            baselines=(message.baseline_unread, message.baseline_pending),
        )
        typing_started_at = self.now()
        delay_ms = 0.0
        for segment in later:
            delay_ms += self.typing_delay_milliseconds(segment)
            send_at = typing_started_at + timedelta(milliseconds=delay_ms)
            await self.append_intent(story.id, {
                "type": "split-message",
                "summary": "The character is still typing the next message segment.",
                "not_before": iso(send_at),
                "payload": {
                    "content": segment,
                    "visibleMessage": True,
                    "userInitiated": phase == "user-message",
                },
            }, typing_started_at, message.participant_id)
            self.schedule_due_intent_wake(story.id, send_at)
    return messages


async def _stage_outbound_message(
    self: "InterludeService",
    story_id: str,
    participant_id: str,
    content: str,
    now: datetime,
    interaction: Optional[dict[str, Any]] = None,
    user_initiated: bool = False,
    baselines: tuple[int, int] | None = None,
    intent_type: str = "outbound-message",
    extra_payload: dict[str, Any] | None = None,
) -> int:
    """Persist one pending-delivery marker; returns its intent id."""
    payload: dict[str, Any] = {
        "content": content,
        "visibleMessage": True,
        "userInitiated": user_initiated,
        "interaction": interaction,
        # P0-D: counter snapshot at compose time. Finalize subtracts these
        # from the LATEST state so messages arriving during transport are
        # preserved instead of being zeroed by stale state.
        "snapshotUnread": (baselines[0] if baselines else 0),
        "snapshotPending": (baselines[1] if baselines else 0),
        "snapshotAt": iso(now),
    }
    if extra_payload:
        payload.update(extra_payload)
    return await self.db.insert_returning_id("interlude_intent", {
        "story_id": story_id,
        "participant_id": participant_id,
        "type": intent_type,
        "summary": "The character composed a message that is being delivered.",
        "not_before": iso(now),
        "status": "pending",
        "payload": payload,
        "created_at": iso(now),
        "updated_at": iso(now),
    })


async def _mark_intent_sending(self: "InterludeService", intent_ids: list[int]) -> None:
    """pending → sending INSIDE the story queue.

    Once a row is `sending`, cancel_pending_outgoing_messages (which only
    selects pending rows) can no longer cancel it: transport has begun and a
    sent bubble can never be retracted.
    """
    ids = [i for i in intent_ids if i]
    if not ids:
        return
    now = iso(self.now())
    await self.db.execute_many([
        ("UPDATE interlude_intent SET status='sending', updated_at=? "
         "WHERE id=? AND status='pending'", (now, i)) for i in ids
    ])


async def _finalize_delivered_message(
    self: "InterludeService",
    story: InterludeStory,
    participant: InterludeParticipant,
    content: str,
    delivery_intent_id: Optional[int],
    now: datetime,
) -> None:
    """Transport succeeded: only now does the message become a spoken fact.

    State is re-read from the database (never from the pre-transport
    snapshot object) and counter baselines captured at compose time are
    subtracted, so user messages that arrived DURING transport survive
    finalize with their unread/pending accounting intact (P0-D).
    """
    metadata: dict[str, Any] = {"visible": True}
    if delivery_intent_id:
        metadata["deliveryIntentId"] = delivery_intent_id
    await self.append_entry(story.id, ScriptEntryDraft(
        kind="character-message", actor="character", content=content,
        occurred_at=iso(now), metadata=metadata,
    ), now, participant.id)

    latest = await self.get_participant(participant.id) or participant
    current = normalize_participant_state(latest.state)
    base_unread: Optional[int] = None
    base_pending: Optional[int] = None
    if delivery_intent_id:
        rows = await self.db.get("interlude_intent", {"id": delivery_intent_id})
        if rows:
            payload = rows[0].get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            base_unread = payload.get("snapshotUnread")
            base_pending = payload.get("snapshotPending")

    updates: dict[str, Any] = {
        "last_character_message_at": iso(now),
    }
    if base_unread is not None or base_pending is not None:
        bu = int(base_unread or 0)
        bp = int(base_pending or 0)
        updates["unread_message_count"] = max(0, current.unread_message_count - bu)
        updates["pending_reply_count"] = max(0, current.pending_reply_count - bp)
    else:
        # Legacy/segment path without a snapshot: full settle.
        updates["unread_message_count"] = 0
        updates["pending_reply_count"] = 0
    new_state = current.model_copy(update={
        "unread_message_count": updates["unread_message_count"],
        "pending_reply_count": updates["pending_reply_count"],
        "last_character_message_at": updates["last_character_message_at"],
    })
    await self.db.update("interlude_participant", {"id": participant.id}, {
        "state": new_state.model_dump(mode="json"), "updated_at": iso(now),
    })
    if delivery_intent_id:
        await self.db.update("interlude_intent", {"id": delivery_intent_id},
                             {"status": "completed", "updated_at": iso(now)})


async def _defer_undelivered_for_retry(
    self: "InterludeService",
    story: InterludeStory,
    message: OutgoingMessageDraft,
    now: datetime,
) -> None:
    """Transport FAILED for a `sending` row: revert to pending (+30s) so the
    committed words still get their chance, without ever having been written
    as a spoken fact (original '发送失败保留并延后重试' semantics)."""
    if not message.delivery_intent_id:
        return
    retry_at = now + timedelta(seconds=30)
    await self.db.execute(
        "UPDATE interlude_intent SET status='pending', not_before=?, updated_at=? "
        "WHERE id=? AND status IN ('sending','pending')",
        (iso(retry_at), iso(now), message.delivery_intent_id),
    )
    self.schedule_due_intent_wake(story.id, retry_at)


async def _cancel_undelivered_messages(self: "InterludeService", story: InterludeStory, messages: list[OutgoingMessageDraft], now: datetime) -> None:
    """Superseded before transport: the world never heard these words and
    never will — only PENDING rows are cancellable by definition."""
    ids = [m.delivery_intent_id for m in messages
           if m.delivery_intent_id and m not in getattr(story, "_keep_retrying", [])]
    await self.db.execute_many([
        ("UPDATE interlude_intent SET status='cancelled', updated_at=? WHERE id=? AND status='pending'",
         (iso(now), intent_id)) for intent_id in ids
    ])
    for message_item in messages:
        if message_item.delivery_intent_id:
            # A row that was already SENDING cannot be cancelled; if we reach
            # here for one (should_cancel raced), defer it for retry instead.
            await self.db.execute(
                "UPDATE interlude_intent SET status='pending', not_before=?, updated_at=? "
                "WHERE id=? AND status='sending'",
                (iso(now + timedelta(seconds=30)), iso(now), message_item.delivery_intent_id),
            )
            self.schedule_due_intent_wake(story.id, now + timedelta(seconds=30))


async def _finalize_group_delivered(
    self: "InterludeService",
    story: InterludeStory,
    group_id: str,
    channel_id: str,
    content: str,
    delivery_intent_id: Optional[int],
    now: datetime,
) -> None:
    """Group transport succeeded: write the spoken fact."""
    await self.append_entry(story.id, ScriptEntryDraft(
        kind="character-group-message", actor="character", content=content,
        occurred_at=iso(now),
        metadata={"groupId": group_id, "channelId": channel_id,
                  "deliveryIntentId": delivery_intent_id},
    ), now)
    if delivery_intent_id:
        await self.db.update("interlude_intent", {"id": delivery_intent_id},
                             {"status": "completed", "updated_at": iso(now)})


InterludeService.stage_outbound_message = _stage_outbound_message  # type: ignore[attr-defined]
InterludeService._mark_intent_sending = _mark_intent_sending  # type: ignore[attr-defined]
InterludeService.finalize_group_delivered = _finalize_group_delivered  # type: ignore[attr-defined]


async def _deliver_group_outbound(
    self: "InterludeService",
    story: InterludeStory,
    intent: NarrativeIntent,
    now: datetime,
) -> bool:
    """Deliver a staged GROUP message through the same outbox rules.

    Routing lives in the intent payload so a crash between stage and send
    recovers on restart instead of silently dropping the message (P1).
    """
    payload = intent.payload
    group_id = str(payload.get("groupId") or "")
    channel_id = str(payload.get("channelId") or group_id)
    content = clip(payload.get("content"), self.config.runtime.max_message_characters)         if isinstance(payload.get("content"), str) else ""
    if not content or not group_id or self.group_sender is None:
        await self.db.update("interlude_intent", {"id": intent.id},
                             {"status": "cancelled", "updated_at": iso(now)})
        return False
    await self._mark_intent_sending([intent.id])
    try:
        ok = bool(await self.group_sender(story, channel_id, content))
    except Exception as error:  # noqa: BLE001
        logger.warning("[hdsi] 群 outbox 投递异常 群=%s 错误=%s", group_id, error)
        ok = False
    if ok:
        await self.finalize_group_delivered(story, group_id, channel_id,
                                            content, intent.id, now)
        return True
    retry_at = now + timedelta(seconds=30)
    await self.db.execute(
        "UPDATE interlude_intent SET not_before=?, updated_at=? "
        "WHERE id=? AND status='pending'",
        (iso(retry_at), iso(now), intent.id),
    )
    self.schedule_due_intent_wake(story.id, retry_at)
    return False


InterludeService._deliver_group_outbound = _deliver_group_outbound  # type: ignore[attr-defined]
InterludeService.finalize_delivered_message = _finalize_delivered_message  # type: ignore[attr-defined]
InterludeService.cancel_undelivered_messages = _cancel_undelivered_messages  # type: ignore[attr-defined]
InterludeService.defer_undelivered_for_retry = _defer_undelivered_for_retry  # type: ignore[attr-defined]


def _agency_config(self: "InterludeService") -> AgencyConfig:
    cfg = self.config.agency
    return AgencyConfig(
        enabled=cfg.enabled,
        max_window_minutes=cfg.max_window_minutes,
        minimum_proactive_interval_minutes=cfg.minimum_proactive_interval_minutes,
        max_candidate_hours=cfg.max_candidate_hours,
    )


InterludeService.persist_decision = _persist_decision  # type: ignore[attr-defined]
InterludeService.agency_config = _agency_config  # type: ignore[attr-defined]


async def _update_participant_state(
    self: "InterludeService",
    participant: InterludeParticipant,
    patch: dict[str, Any],
    now: datetime,
) -> None:
    current = normalize_participant_state(participant.state)
    data = current.model_dump(mode="json")
    merged = {**data, **patch}
    new_state = normalize_participant_state(merged)
    await self.db.update("interlude_participant", {"id": participant.id}, {
        "state": new_state.model_dump(mode="json"), "updated_at": iso(now),
    })


InterludeService.update_participant_state = _update_participant_state  # type: ignore[attr-defined]


async def _apply_intent_updates(
    self: "InterludeService",
    story_id: str,
    updates: list[IntentUpdateDraft],
    now: datetime,
    participant_id: str | None = None,
) -> None:
    if not updates:
        return
    ids = [u.id for u in updates]
    rows = await self.db.get("interlude_intent", {
        "story_id": story_id, "id": ids, "status": "pending",
    })
    allowed = {}
    for row in rows:
        intent = NarrativeIntent.model_validate(row)
        if not is_active_consequence(intent):
            continue
        if participant_id and intent.participant_id and intent.participant_id != participant_id:
            continue
        allowed[intent.id] = intent
    statements = []
    for update in updates:
        intent = allowed.get(update.id)
        if intent is None:
            continue
        payload = dict(intent.payload)
        if update.resolution:
            payload["resolution"] = update.resolution
        payload_json = __import__("json").dumps(payload, ensure_ascii=False)
        statements.append((
            "UPDATE interlude_intent SET status=?, payload=?, updated_at=? WHERE id=?",
            (update.status, payload_json, iso(now), intent.id),
        ))
    if statements:
        await self.db.execute_many(statements)


InterludeService.apply_intent_updates = _apply_intent_updates  # type: ignore[attr-defined]


# ------------------------------------------------------------------ alter

def _update_alter_system(
    self: "InterludeService",
    story: InterludeStory,
    current,
    alter,
    phase: str,
    now: datetime,
):
    cfg = self._alter_config_dict()
    if not cfg["enabled"] or alter is None:
        return None
    result = alter_mod.advance_alter_system(current, alter, phase, now, cfg)
    if result.offset_expired:
        self.report_operation("standard", "info", story, phase, "Alter 情绪偏移已自然消退")
    self.report_operation("diagnostic", "debug", story, phase,
                          "Alter 状态已更新 本轮=%s 累计=%s 阈值=%s 权重=%s",
                          alter, result.state.alter_value, f"{result.threshold:.2f}",
                          f"{result.state.alter_weight:.2f}")
    return result


InterludeService._update_alter_system = _update_alter_system  # type: ignore[attr-defined]


def _schedule_alter_analysis(self: "InterludeService", story_id: str, phase: str, participant_id: str = "") -> None:
    if story_id in self.scheduled_alter_analyses:
        return
    self.scheduled_alter_analyses.add(story_id)
    loop = asyncio.get_running_loop()

    async def run() -> None:
        try:
            await self.queues.run(story_id, lambda: self.analyze_alter_system(story_id, phase, participant_id))
        except Exception as error:  # noqa: BLE001
            self.report_standalone("warn", "Alter 后台分析任务失败 故事=%s 错误=%s", story_id, error)
        finally:
            self.scheduled_alter_analyses.discard(story_id)

    loop.create_task(run())


InterludeService.schedule_alter_analysis = _schedule_alter_analysis  # type: ignore[attr-defined]


async def _analyze_alter_system(self: "InterludeService", story_id: str, phase: str, participant_id: str = "") -> None:
    cfg = self._alter_config_dict()
    if not cfg["enabled"]:
        return
    story = await self.get_story(story_id)
    state = story.state.alter_system
    if state is None:
        return
    now = self.now()
    threshold = alter_mod.calculate_alter_threshold(state.history, cfg, now)
    if abs(state.alter_value) < threshold or alter_mod.alter_analysis_cooling_down(state, now):
        return
    state.last_analysis_attempt_at = iso(now)
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": story.state.model_copy(update={"alter_system": state}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    trigger_value = state.alter_value
    direction = -1 if trigger_value < 0 else 1
    scripts_rows = await self.recent_entries(story.id, 50)
    scripts = [
        {"content": e.content[:4000], "occurredAt": iso(e.occurred_at)}
        for e in scripts_rows
        if e.kind == "script"
        and e.content.strip()
        and (not e.participant_id or e.participant_id == participant_id)
    ][-10:]
    self.report_operation("standard", "info", story, phase,
                          "Alter 累积触发 数值=%s 阈值=%s 方向=%s",
                          signed_number(trigger_value), f"{threshold:.2f}",
                          "严肃" if direction > 0 else "放松")
    from .prompt_builder import alter_analysis_prompt
    from .types import AlterAnalysisRequest, EmotionalOffsetPrompt

    request_payload = AlterAnalysisRequest(
        character_name=story.setting.character.name,
        trigger_value=trigger_value,
        threshold=threshold,
        direction="serious" if direction > 0 else "relaxed",
        recent_scripts=scripts,
        history=state.history[-10:],
        setting_overlay=story.state.setting_overlay,
        current_offset=(
            EmotionalOffsetPrompt(
                direction=state.emotional_offset.direction,  # type: ignore[arg-type,union-attr]
                description=state.emotional_offset.description,  # type: ignore[union-attr]
                intensity=state.emotional_offset.intensity,  # type: ignore[union-attr]
                generated_at=state.emotional_offset.generated_at,  # type: ignore[union-attr]
                weight=state.alter_weight,
            ) if state.emotional_offset is not None else None
        ),
    )
    alter_cfg = self.config.alter_system
    try:
        result = await self.narrator.analyze_alter(
            request_payload.model_dump(mode="json"),
            system_prompt=alter_analysis_prompt(alter_cfg.prompt),
            temperature=alter_cfg.temperature,
            top_p=alter_cfg.top_p,
            max_tokens=alter_cfg.max_tokens,
            timeout_seconds=alter_cfg.timeout / 1000.0,
            response_json=True,
        )
        description = str(result.get("description", "")).strip()[:800]
        if not description:
            raise ValueError("Alter analysis returned an empty description.")
        completed = alter_mod.complete_alter_analysis(state, description, threshold, now, cfg)
        fresh = await self.get_story(story_id)
        fresh_state = normalize_story_state(fresh.state)
        fresh_state.alter_system = completed
        await self.db.update("interlude_story", {"id": story.id}, {
            "state": fresh_state.model_dump(mode="json"), "updated_at": iso(now),
        })
        offset = completed.emotional_offset
        self.report_operation("standard", "info", story, phase,
                              "情绪偏移生成完成 方向=%s 强度=%s 描述=%s",
                              offset.direction if offset else "?",
                              f"{offset.intensity:.2f}" if offset else "?", description)
        self.report_operation("standard", "info", story, phase,
                              "情绪偏移已注入后续主提示词 权重=1.00")
    except Exception as error:  # noqa: BLE001
        self.write_report("warn", story.setting.character.name, phase,
                          "Alter 分析失败，已保留累计值等待重试：%s", (error,))


InterludeService.analyze_alter_system = _analyze_alter_system  # type: ignore[attr-defined]


# ------------------------------------------------------------------ intents & wakes

async def _due_intents(self: "InterludeService", story_id: str, now: datetime) -> list[NarrativeIntent]:
    rows = await self.db.get(
        "interlude_intent",
        {"story_id": story_id, "status": "pending", "not_before": {"$lte": now}},
        order_by="not_before",
    )
    intents = [NarrativeIntent.model_validate(r) for r in rows]
    expired_agency = [
        i for i in intents
        if i.type == "proactive-check"
        and (
            not self.config.agency.enabled
            or parse_date(i.payload.get("expiresAt")) is None
            or (parse_date(i.payload.get("expiresAt")) or datetime.fromtimestamp(0, tz=timezone.utc)) <= now
        )
    ]
    if expired_agency:
        await self.db.execute_many([
            ("UPDATE interlude_intent SET status='cancelled', updated_at=? WHERE id=?",
             (iso(now), intent.id)) for intent in expired_agency
        ])
    expired_ids = {intent.id for intent in expired_agency}
    return [i for i in intents if i.id not in expired_ids and not is_active_consequence(i)]


InterludeService.due_intents = _due_intents  # type: ignore[attr-defined]


def _schedule_due_intent_wake(self: "InterludeService", story_id: str, not_before: datetime) -> None:
    delay = max(0.0, (not_before - self.now()).total_seconds())
    existing_at = self.due_wake_at.get(story_id)
    loop = asyncio.get_running_loop()
    if existing_at is not None and existing_at <= self.now().timestamp() + delay:
        return

    async def wake() -> None:
        try:
            await asyncio.sleep(delay)
            self.due_wake_tasks.pop(story_id, None)
            self.due_wake_at.pop(story_id, None)
            if self.database_resetting:
                return
            due = await self.due_intents(story_id, self.now())
            if due and all(intent.type in TRANSPORT_INTENT_TYPES for intent in due):
                await self.deliver_due_split_segments(story_id)
                return
            if self.sweep_running or self.has_pending_narrative(story_id):
                retry_task = loop.create_task(self._wake_retry(story_id))
                self.due_wake_tasks[story_id] = retry_task
                self.due_wake_at[story_id] = self.now().timestamp() + 1.0
                return
            await self.sweep()
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001
            logger.debug("到期消息唤醒失败：%s", error)

    task = loop.create_task(wake())
    old = self.due_wake_tasks.get(story_id)
    if old is not None and not old.done():
        old.cancel()
    self.due_wake_tasks[story_id] = task
    self.due_wake_at[story_id] = self.now().timestamp() + delay


async def _wake_retry(self: "InterludeService", story_id: str) -> None:
    try:
        await asyncio.sleep(1.0)
        self.due_wake_tasks.pop(story_id, None)
        self.due_wake_at.pop(story_id, None)
        if not self.database_resetting and not self.sweep_running \
                and not self.has_pending_narrative(story_id):
            await self.sweep()
    except asyncio.CancelledError:
        pass


InterludeService._wake_retry = _wake_retry  # type: ignore[attr-defined]
InterludeService.schedule_due_intent_wake = _schedule_due_intent_wake  # type: ignore[attr-defined]


async def _schedule_next_split_wake(self: "InterludeService", story_id: str) -> None:
    rows = await self.db.get(
        "interlude_intent",
        {"story_id": story_id, "status": "pending",
         "type": list(TRANSPORT_INTENT_TYPES)},
        order_by="not_before", limit=1,
    )
    if rows:
        next_intent = NarrativeIntent.model_validate(rows[0])
        self.schedule_due_intent_wake(story_id, next_intent.not_before)


InterludeService.schedule_next_split_wake = _schedule_next_split_wake  # type: ignore[attr-defined]


async def _deliver_due_split_segments(self: "InterludeService", story_id: str) -> None:
    """Deliver already-decided <sep/> segments without invoking the narrator."""
    await self.queues.run(story_id, lambda: self._deliver_due_split_segments_locked(story_id))


async def _deliver_due_split_segments_locked(self: "InterludeService", story_id: str) -> None:
    story_rows = await self.db.get("interlude_story", {"id": story_id})
    if not story_rows:
        return
    story = InterludeStory.model_validate(story_rows[0])
    now = self.now()
    rows = await self.db.get(
        "interlude_intent",
        {"story_id": story_id, "status": "pending",
         "type": list(TRANSPORT_INTENT_TYPES),
         "not_before": {"$lte": now}},
        order_by="not_before", limit=20,
    )
    due = [NarrativeIntent.model_validate(r) for r in rows]
    if due:
        intent = due[0]
        if intent.type == "outbound-group-message":
            await self._deliver_group_outbound(story, intent, now)
            await self.schedule_next_split_wake(story_id)
            return
        content = clip(intent.payload.get("content"), self.config.runtime.max_message_characters) \
            if isinstance(intent.payload.get("content"), str) else ""
        participant = await self.get_participant(intent.participant_id) if intent.participant_id else None
        if intent.participant_id and intent.participant_id in self.interrupted_typing_participants:
            return
        if not content or participant is None or participant.status != "active":
            await self.db.update("interlude_intent", {"id": intent.id},
                                 {"status": "cancelled", "updated_at": iso(now)})
        else:
            def should_cancel(target: InterludeParticipant) -> bool:
                return target.id in self.interrupted_typing_participants

            if intent.type == "outbound-message":
                await self._mark_intent_sending([intent.id])
            # P0-A: send_outgoing_messages finalizes; no manual double write.
            delivered = await self.send_outgoing_messages(
                story,
                [OutgoingMessageDraft(
                    participant_id=participant.id,
                    content=content,
                    delivery_intent_id=intent.id,
                )],
                should_cancel=should_cancel,
            )
            if not delivered:
                if participant.id in self.interrupted_typing_participants:
                    return
                retry_at = now + timedelta(seconds=30)
                await self.db.execute(
                    "UPDATE interlude_intent SET not_before=?, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (iso(retry_at), iso(now), intent.id),
                )
                self.schedule_due_intent_wake(story_id, retry_at)
                return
    remaining = due[1:]
    if remaining:
        following = remaining[0]
        if following.not_before <= now:
            content = clip(following.payload.get("content"),
                           self.config.runtime.max_message_characters) \
                if isinstance(following.payload.get("content"), str) else ""
            if content:
                await self.db.update("interlude_intent", {"id": following.id}, {
                    "not_before": iso(now + timedelta(milliseconds=self.typing_delay_milliseconds(content))),
                    "updated_at": iso(now),
                })
    await self.schedule_next_split_wake(story_id)


InterludeService.deliver_due_split_segments = _deliver_due_split_segments  # type: ignore[attr-defined]
InterludeService._deliver_due_split_segments_locked = _deliver_due_split_segments_locked  # type: ignore[attr-defined]


async def _cancel_pending_outgoing_messages(
    self: "InterludeService",
    story_id: str,
    participant_id: str,
    now: datetime,
    cancel_planned: bool = True,
) -> list[NarrativeIntent]:
    completed = False
    try:
        rows = await self.db.get(
            "interlude_intent", {"story_id": story_id, "participant_id": participant_id,
                                 "status": "pending"},
        )
        intents = [NarrativeIntent.model_validate(r) for r in rows]
        matching = [
            i for i in intents
            if i.participant_id == participant_id and (
                i.type in ("split-message", "outbound-message")
                or (cancel_planned and i.type in ("delayed-reply", "cross-conversation-message"))
            )
        ]
        if not matching:
            completed = True
            return matching
        await self.db.execute_many([
            ("UPDATE interlude_intent SET status='cancelled', updated_at=? WHERE id=?",
             (iso(now), intent.id)) for intent in matching
        ])
        wake_task = self.due_wake_tasks.pop(story_id, None)
        if wake_task is not None:
            wake_task.cancel()
        self.due_wake_at.pop(story_id, None)
        await self.schedule_next_split_wake(story_id)
        interrupted_drafts = []
        for intent in matching:
            if intent.type in ("split-message", "outbound-message"):
                content = intent.payload.get("content")
                clipped = clip(content, self.config.runtime.max_message_characters) \
                    if isinstance(content, str) else ""
                if clipped:
                    interrupted_drafts.append(clipped)
        import json as _json

        if interrupted_drafts:
            quoted = " 和 ".join(_json.dumps(d, ensure_ascii=False) for d in interrupted_drafts)
            content_text = (
                f"The protagonist wanted to send {quoted}, but had not finished typing "
                "before the user's new message arrived."
            )
        else:
            content_text = (
                "A newer user message superseded a planned outgoing message before it was sent."
            )
        await self.append_entry(story_id, ScriptEntryDraft(
            kind="intent-cancelled", actor="system", content=content_text,
            occurred_at=iso(now),
            metadata={"intentIds": [i.id for i in matching],
                      "interruptedDrafts": interrupted_drafts},
        ), now, participant_id)
        completed = True
        return matching
    finally:
        if completed:
            self.interrupted_typing_participants.discard(participant_id)


InterludeService.cancel_pending_outgoing_messages = _cancel_pending_outgoing_messages  # type: ignore[attr-defined]


# ------------------------------------------------------------------ delivery

def _split_outgoing_message(self: "InterludeService", content: str) -> tuple[str, list[str]]:
    if self.config.runtime.split_reply_messages is False:
        return content, []
    separator = (self.config.runtime.message_separator or "<sep/>").strip() or "<sep/>"
    if not separator or separator not in content:
        return content, []
    parts = [part.strip() for part in content.split(separator)]
    parts = [part for part in parts if part]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _typing_delay_milliseconds(self: "InterludeService", next_segment: str) -> float:
    import math as _math

    runtime = self.config.runtime
    base = max(0.0, float(runtime.typing_base_delay_seconds))
    cps = max(1.0, float(runtime.typing_characters_per_second))
    maximum = max(base, float(runtime.typing_max_delay_seconds))
    seconds = min(maximum, base + _math.ceil(len(next_segment) / cps))
    return seconds * 1000.0


InterludeService.split_outgoing_message = _split_outgoing_message  # type: ignore[attr-defined]
InterludeService.typing_delay_milliseconds = _typing_delay_milliseconds  # type: ignore[attr-defined]


async def _send_outgoing_messages(
    self: "InterludeService",
    story: InterludeStory,
    messages: list[OutgoingMessageDraft],
    current: Optional[InterludeParticipant] = None,
    should_cancel: Optional[Callable[[InterludeParticipant], bool]] = None,
) -> list[OutgoingMessageDraft]:
    delivered: list[OutgoingMessageDraft] = []
    if not messages:
        return delivered
    by_id: dict[str, InterludeParticipant] = {}
    ids: list[str] = []
    for message in messages:
        if message.participant_id and message.participant_id not in ids:
            ids.append(message.participant_id)
    if current is not None and current.id in ids:
        by_id[current.id] = current
    missing = [pid for pid in ids if pid not in by_id]
    for pid in missing:
        participant = await self.get_participant(pid)
        if participant is not None:
            by_id[pid] = participant
    for message in messages:
        target = by_id.get(message.participant_id)
        if target is None:
            self.write_report("warn", story.setting.character.name, "intent-due",
                              "无法投递消息：参与者不存在 %s", (message.participant_id,))
            continue
        if not self.can_handle_participant(target):
            self.write_report("warn", story.setting.character.name, "intent-due",
                              "消息被当前账号白名单拦截 参与者=%s", (target.id,))
            continue
        if should_cancel is not None and should_cancel(target):
            self.report_operation("standard", "info", story, "user-message",
                                  "新消息打断主角输入，停止发送后续分段 参与者=%s", target.id)
            await self.cancel_undelivered_messages(story, [message], self.now())
            continue
        try:
            self.report_operation("standard", "info", story, "intent-due",
                                  "消息投递开始 参与者=%s", target.id)
            if self.config.logging.log_message_content:
                self.write_report("info", story.setting.character.name, "intent-due",
                                  "主角消息内容：%s",
                                  (message.content[: self.config.logging.preview_length],))
            ok = await self.sender(story, target, message.content)
            if ok:
                delivered.append(message)
                # P0-1: only a confirmed transport makes the words "spoken".
                await self.finalize_delivered_message(
                    story, target, message.content,
                    message.delivery_intent_id, self.now(),
                )
            else:
                await self.defer_undelivered_for_retry(story, message, self.now())
                self.write_report("warn", story.setting.character.name, "intent-due",
                                  "消息投递失败：发送通道不可用，已延后重试 参与者=%s", (target.id,))
        except Exception as error:  # noqa: BLE001
            await self.defer_undelivered_for_retry(story, message, self.now())
            self.write_report("warn", story.setting.character.name, "intent-due",
                              "消息投递失败 参与者=%s 错误=%s", (target.id, error))
    return delivered


InterludeService.send_outgoing_messages = _send_outgoing_messages  # type: ignore[attr-defined]


# ------------------------------------------------------------------ advance & sweep

async def _advance_story(self: "InterludeService", story: InterludeStory, force: bool = True) -> list[OutgoingMessageDraft]:
    if not self.can_handle_story(story):
        return []

    async def task() -> list[OutgoingMessageDraft]:
        fresh_rows = await self.db.get("interlude_story", {"id": story.id})
        fresh = InterludeStory.model_validate(fresh_rows[0]) if fresh_rows else story
        return await self.advance_unlocked(fresh, self.now(), force)

    messages = await self.queues.run(story.id, task)
    if force or messages:
        self.report_operation("summary", "info", story, "advance",
                              "剧本推进完成 可见消息=%d", len(messages))
    self.schedule_compaction(story.id)
    return messages


InterludeService.advance_story = _advance_story  # type: ignore[attr-defined]


async def _sweep(self: "InterludeService") -> None:
    if self.database_resetting or self.sweep_running:
        return
    self.sweep_running = True
    started_at = self.now()
    try:
        stories = await self.active_stories()
        handled = [s for s in stories if self.can_handle_story(s)]
        if not handled:
            self.report_standalone("diagnostic",
                                   "后台扫描跳过：没有可处理的活动主剧本", verbosity="diagnostic")
            return
        for story in handled:
            await self._sweep_one(story)
    finally:
        self.sweep_running = False


async def _sweep_one(self: "InterludeService", story: InterludeStory) -> None:
    started_at = self.now()
    if self.has_pending_narrative(story.id):
        # Cooperative scheduling: a skipped sweep must still yield to the
        # event loop once, otherwise debounced turn tasks can starve on a
        # host whose only periodic driver is this sweep.
        pending_due = await self.due_intents(story.id, self.now())
        delivery_only = bool(pending_due) and all(i.type in TRANSPORT_INTENT_TYPES for i in pending_due)
        if not delivery_only:
            self.report_operation("diagnostic", "debug", story, "advance",
                                  "后台扫描跳过：前台消息回合或合并计时器仍在处理中")
            await asyncio.sleep(0)
            return
        self.report_operation("diagnostic", "debug", story, "advance",
                              "前台回合处理中，先投递已确定的分段消息 数量=%d", len(pending_due))
    automation = story.state.automation
    self.report_operation("diagnostic", "debug", story, "advance",
                          "后台扫描开始 游标=%s 下次自动推进=%s",
                          format_log_time(story.cursor_at, story.setting.timezone),
                          format_log_time(parse_date(automation.next_advance_at),
                                          story.setting.timezone))
    messages = await self.advance_story(story, force=False)
    if messages:
        await self.send_outgoing_messages(story, messages)
    elapsed_ms = round((self.now() - started_at).total_seconds() * 1000)
    self.report_operation("diagnostic", "debug", story, "advance",
                          "后台扫描完成 耗时=%dms 已投递=%d", elapsed_ms, len(messages))


InterludeService.sweep = _sweep  # type: ignore[attr-defined]
InterludeService._sweep_one = _sweep_one  # type: ignore[attr-defined]


async def _advance_unlocked(
    self: "InterludeService",
    story: InterludeStory,
    now: datetime,
    force: bool,
) -> list[OutgoingMessageDraft]:
    from .normalize import group_due_intents
    from .scheduler import (
        active_rest_window,
        automatic_interval_minutes,
        due_conversation_follow_ups,
        is_automatic_advance_due,
        is_automatic_advance_paused,
    )

    auto_cfg = {
        "enabled": self.config.runtime.auto_advance_enabled,
        "interval_minutes": self.config.runtime.auto_advance_interval_minutes,
        "jitter_minutes": self.config.runtime.auto_advance_jitter_minutes,
        "follow_up_minutes": self.config.runtime.conversation_follow_up_minutes,
        "follow_up_jitter_minutes": self.config.runtime.conversation_follow_up_jitter_minutes,
        "rest_windows": [w.model_dump() for w in self.config.runtime.rest_windows],
    }
    from_time = narrative_cursor(story, now)
    elapsed = max(0.0, (now - from_time).total_seconds())
    due = await self.due_intents(story.id, now)
    messages: list[OutgoingMessageDraft] = []
    # Later <sep/> bubbles are delivery events, at most one per wake-up.
    split_segments = sorted(
        [i for i in due if i.type in TRANSPORT_INTENT_TYPES], key=lambda i: i.not_before
    )[:1]
    split_handled = False
    for intent in split_segments:
        if intent.type == "outbound-group-message":
            handled = await self._deliver_group_outbound(story, intent, now)
            split_handled = True
            continue
        content_raw = intent.payload.get("content")
        content = clip(content_raw, self.config.runtime.max_message_characters) \
            if isinstance(content_raw, str) else ""
        participant = await self.get_participant(intent.participant_id) if intent.participant_id else None
        if intent.participant_id and intent.participant_id in self.interrupted_typing_participants:
            continue
        split_handled = True
        if not content or participant is None or participant.status != "active":
            await self.db.update("interlude_intent", {"id": intent.id},
                                 {"status": "cancelled", "updated_at": iso(now)})
            continue

        def should_cancel(target: InterludeParticipant) -> bool:
            return target.id in self.interrupted_typing_participants

        # P0-A: the intent id rides on the draft so send_outgoing_messages is
        # the ONLY writer of the spoken fact (no manual double append here).
        draft = OutgoingMessageDraft(
            participant_id=participant.id,
            content=content,
            delivery_intent_id=intent.id,
        )
        if intent.type == "outbound-message":
            await self._mark_intent_sending([intent.id])
        delivered = await self.send_outgoing_messages(
            story, [draft], should_cancel=should_cancel,
        )
        if not delivered:
            if participant.id in self.interrupted_typing_participants:
                continue
            retry_at = now + timedelta(seconds=30)
            # Only PENDING rows get retried; a row already marked sending is
            # protected from cancellation by design.
            await self.db.execute(
                "UPDATE interlude_intent SET not_before=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (iso(retry_at), iso(now), intent.id),
            )
            self.schedule_due_intent_wake(story.id, retry_at)
            continue
    if split_handled:
        await self.schedule_next_split_wake(story.id)
    due = [i for i in due if i.type not in TRANSPORT_INTENT_TYPES]
    # Browser research intents are executed here, bounded per sweep.
    browser_intents = [
        i for i in due if i.type == "browser-research"
    ][: max(1, self.config.browser.max_research_per_sweep)]
    for intent in browser_intents:
        await self.execute_deferred_browser_intent(story, intent, now)
    due = [i for i in due if i.type != "browser-research"]

    auto_advance_enabled = self.config.runtime.auto_advance_enabled
    due_follow_ups = due_conversation_follow_ups(story, now) if auto_advance_enabled else []
    automatic_due = auto_advance_enabled and (
        bool(due_follow_ups) or is_automatic_advance_due(story, now, auto_cfg)
    )
    paused_for_conversation = is_automatic_advance_paused(story, now)
    self.report_operation("diagnostic", "debug", story, "advance",
                          "后台状态 到期计划=%d 分段消息=%d 网页任务=%d 短期跟进=%d 自动推进到期=%s 对话暂停=%s",
                          len(due), len(split_segments), len(browser_intents),
                          len(due_follow_ups), automatic_due, paused_for_conversation)
    if not force and not due and (not automatic_due or paused_for_conversation):
        return messages

    minimum_manual_advance_s = max(1, self.config.runtime.minimum_advance_minutes) * MINUTE
    manual_advance_too_soon = (
        force and not due and not due_follow_ups and elapsed < minimum_manual_advance_s
    )
    if manual_advance_too_soon:
        self.report_operation("standard", "info", story, "advance",
                              "手动推进跳过：游标距离现在不足 %d 分钟，且没有到期计划或对话后续任务",
                              self.config.runtime.minimum_advance_minutes)
        return messages

    advanced = False
    delayed_reply_processed = False
    has_narrative_due = bool(due)
    if elapsed > 0 and not has_narrative_due and (force or (automatic_due and not paused_for_conversation)):
        follow_up_participant_id = story.state.automation.conversation_follow_up_participant_id \
            if due_follow_ups else ""
        follow_up_participant = (
            await self.get_participant(follow_up_participant_id) if follow_up_participant_id else None
        )
        phase = (
            "conversation-follow-up"
            if follow_up_participant is not None and follow_up_participant.status == "active"
            else "advance"
        )
        self.report_operation("standard", "info", story, phase,
                              "即将执行自动写作 类型=%s 时间段=%s→%s",
                              phase_label(phase),
                              format_log_time(from_time, story.setting.timezone),
                              format_log_time(now, story.setting.timezone))
        outcome = await self.try_decide(story, follow_up_participant, phase, from_time, now)
        decision = outcome["decision_raw"]
        succeeded = outcome["succeeded"]
        if succeeded:
            permit_messages = phase == "conversation-follow-up" or self.config.runtime.allow_proactive_messages
            messages.extend(await self.persist_decision(
                story, follow_up_participant, decision, from_time, outcome["effective_now"],
                permit_messages=permit_messages, phase=phase,
            ))
            await self.db.update("interlude_story", {"id": story.id}, {
                "cursor_at": iso(outcome["effective_now"]), "updated_at": iso(now),
            })
            advanced = True

    due_batches = group_due_intents(due)
    due_batch = due_batches[0] if due_batches else None
    if due_batch:
        current_rows = await self.db.get("interlude_story", {"id": story.id})
        current = InterludeStory.model_validate(current_rows[0]) if current_rows else story
        due_from = narrative_cursor(current, now)
        due_participant_id = due_batch[0].participant_id if due_batch else ""
        due_participant = await self.get_participant(due_participant_id) if due_participant_id else None
        self.report_operation("standard", "info", current, "intent-due",
                              "即将处理到期计划 数量=%d 类型=%s 参与者=%s",
                              len(due_batch),
                              ",".join(sorted({i.type for i in due_batch})),
                              due_participant.id if due_participant else "全局")
        outcome = await self.try_decide(
            current, due_participant, "intent-due", due_from, now, due_intents=due_batch,
        )
        decision = outcome["decision_raw"]
        succeeded = outcome["succeeded"]
        user_initiated_batch = any(i.payload.get("userInitiated") is True for i in due_batch)
        permit_messages = self.config.runtime.allow_proactive_messages or user_initiated_batch
        messages.extend(await self.persist_decision(
            current, due_participant, decision, due_from, outcome["effective_now"],
            permit_messages=permit_messages, phase="intent-due",
            context_intents=due_batch,
        ))
        if succeeded:
            await self.db.update("interlude_story", {"id": current.id}, {
                "cursor_at": iso(outcome["effective_now"]), "updated_at": iso(now),
            })
            await self.db.execute_many([
                ("UPDATE interlude_intent SET status='completed', updated_at=? WHERE id=?",
                 (iso(now), intent.id)) for intent in due_batch
            ])
            if any(i.type == "delayed-reply" for i in due_batch):
                delayed_reply_processed = True
                await self.schedule_conversation_follow_ups_after_turn(
                    story.id, now, None, due_participant.id if due_participant else "",
                )
            elif not advanced and not delayed_reply_processed:
                await self.schedule_next_automatic_advance(story.id, now)
        else:
            retries = [i for i in due_batch if i.type == "narrative-retry"]
            if retries:
                attempts = max(int(i.payload.get("attempt") or 0) for i in retries)
                await self.db.execute_many([
                    ("UPDATE interlude_intent SET status='cancelled', updated_at=? WHERE id=?",
                     (iso(now), r.id)) for r in retries
                ])
                await self.schedule_narrative_retry(
                    current.id, due_participant_id, now, previous_attempts=attempts,
                )
    if len(due_batches) > 1:
        self.report_operation("standard", "info", story, "intent-due",
                              "其余 %d 组到期计划已保留，下一次扫描将按新的时间段继续处理",
                              len(due_batches) - 1)
        self.schedule_due_intent_wake(
            story.id, now + timedelta(seconds=max(SECOND, self.config.runtime.sweep_interval_minutes * MINUTE)),
        )
    if advanced and not delayed_reply_processed:
        has_more_follow_ups = bool(due_follow_ups) and await self.complete_conversation_follow_ups(story.id, now)
        if not has_more_follow_ups:
            await self.schedule_next_automatic_advance(story.id, now)
    return messages


InterludeService.advance_unlocked = _advance_unlocked  # type: ignore[attr-defined]


# ------------------------------------------------------------------ scheduling helpers

async def _pause_automatic_advance_after_user_message(self: "InterludeService", story_id: str, now: datetime) -> None:
    from .scheduler import automatic_interval_minutes

    rows = await self.db.get("interlude_story", {"id": story_id})
    if not rows:
        return
    story = InterludeStory.model_validate(rows[0])
    auto_cfg = {
        "interval_minutes": self.config.runtime.auto_advance_interval_minutes,
        "jitter_minutes": self.config.runtime.auto_advance_jitter_minutes,
        "rest_windows": [w.model_dump() for w in self.config.runtime.rest_windows],
    }
    fallback_minutes = automatic_interval_minutes(story, now, auto_cfg)
    automation = story.state.automation.model_copy(update={
        "conversation_follow_up_at": [],
        "conversation_follow_up_participant_id": None,
        "quiet_until": None,
        "last_user_message_at": iso(now),
        "next_advance_at": iso(now + timedelta(minutes=fallback_minutes)),
    })
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": story.state.model_copy(update={"automation": automation}).model_dump(mode="json"),
        "updated_at": iso(now),
    })


InterludeService.pause_automatic_advance_after_user_message = _pause_automatic_advance_after_user_message  # type: ignore[attr-defined]


async def _schedule_conversation_follow_ups_after_turn(
    self: "InterludeService",
    story_id: str,
    now: datetime,
    raw_interaction=None,
    participant_id: str = "",
) -> None:
    from .normalize import normalize_interaction
    from .scheduler import active_rest_window, schedule_conversation_follow_ups, automatic_interval_minutes

    runtime = self.config.runtime
    auto_cfg = {
        "enabled": runtime.auto_advance_enabled,
        "interval_minutes": runtime.auto_advance_interval_minutes,
        "jitter_minutes": runtime.auto_advance_jitter_minutes,
        "follow_up_minutes": runtime.conversation_follow_up_minutes,
        "follow_up_jitter_minutes": runtime.conversation_follow_up_jitter_minutes,
        "rest_windows": [w.model_dump() for w in runtime.rest_windows],
    }
    if not auto_cfg["enabled"]:
        return
    rows = await self.db.get("interlude_story", {"id": story_id})
    if not rows:
        return
    story = InterludeStory.model_validate(rows[0])
    interaction = None
    if raw_interaction is not None:
        from .normalize import normalize_interaction as _ni

        if isinstance(raw_interaction, dict):
            interaction = _ni(
                raw_interaction, now,
                runtime.max_message_characters,
                runtime.minimum_delayed_reply_seconds,
                runtime.maximum_delayed_reply_minutes,
            )
        else:
            interaction = raw_interaction
    delayed_until = None
    if interaction is not None:
        reply = getattr(interaction, "reply", None)
        mode = getattr(reply, "mode", None) or (reply.get("mode") if isinstance(reply, dict) else None)
        send_at_value = getattr(reply, "send_at", None)
        if send_at_value is None and isinstance(reply, dict):
            send_at_value = reply.get("sendAt") or reply.get("send_at")
        if mode == "delayed":
            delayed_until = parse_date(send_at_value)
    anchor = delayed_until if (delayed_until is not None and delayed_until > now) else now
    rest_windows = [w.model_dump() for w in runtime.rest_windows]
    follow_ups: list[datetime] = [] if active_rest_window(rest_windows, story.setting.timezone, anchor) \
        else schedule_conversation_follow_ups(anchor, auto_cfg)
    normal_next = follow_ups[-1] if follow_ups else anchor + timedelta(
        minutes=automatic_interval_minutes(story, anchor, auto_cfg)
    )
    automation = story.state.automation.model_copy(update={
        "quiet_until": None,
        "conversation_follow_up_at": [iso(value) for value in follow_ups],
        "conversation_follow_up_participant_id": (participant_id or None) if follow_ups else None,
        "next_advance_at": iso(normal_next),
    })
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": story.state.model_copy(update={"automation": automation}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    self.report_operation("standard", "info", story, "conversation-follow-up",
                          "已更新对话后续计划 短期补写=%s 常规推进=%s",
                          "、".join(format_log_time(v, story.setting.timezone) for v in follow_ups) if follow_ups else "无",
                          format_log_time(normal_next, story.setting.timezone))


InterludeService.schedule_conversation_follow_ups_after_turn = _schedule_conversation_follow_ups_after_turn  # type: ignore[attr-defined]


async def _complete_conversation_follow_ups(self: "InterludeService", story_id: str, now: datetime) -> bool:
    rows = await self.db.get("interlude_story", {"id": story_id})
    if not rows:
        return False
    story = InterludeStory.model_validate(rows[0])
    remaining = sorted(
        [
            value for value in (parse_date(t) for t in story.state.automation.conversation_follow_up_at)
            if value is not None and value > now
        ],
        key=lambda v: v.timestamp(),
    )
    automation = story.state.automation.model_copy(update={
        "conversation_follow_up_at": [iso(v) for v in remaining],
        "conversation_follow_up_participant_id":
            story.state.automation.conversation_follow_up_participant_id if remaining else None,
        "next_advance_at": iso(remaining[0]) if remaining else None,
    })
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": story.state.model_copy(update={"automation": automation}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    return bool(remaining)


InterludeService.complete_conversation_follow_ups = _complete_conversation_follow_ups  # type: ignore[attr-defined]


async def _schedule_next_automatic_advance(self: "InterludeService", story_id: str, now: datetime) -> None:
    from .scheduler import automatic_interval_minutes

    if not self.config.runtime.auto_advance_enabled:
        return
    rows = await self.db.get("interlude_story", {"id": story_id})
    if not rows:
        return
    story = InterludeStory.model_validate(rows[0])
    auto_cfg = {
        "interval_minutes": self.config.runtime.auto_advance_interval_minutes,
        "jitter_minutes": self.config.runtime.auto_advance_jitter_minutes,
        "rest_windows": [w.model_dump() for w in self.config.runtime.rest_windows],
    }
    interval_minutes = automatic_interval_minutes(story, now, auto_cfg)
    next_advance_at = now + timedelta(minutes=interval_minutes)
    automation = story.state.automation.model_copy(update={
        "quiet_until": None,
        "conversation_follow_up_at": [],
        "conversation_follow_up_participant_id": None,
        "last_auto_advance_at": iso(now),
        "next_advance_at": iso(next_advance_at),
    })
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": story.state.model_copy(update={"automation": automation}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    self.report_operation("standard", "info", story, "advance",
                          "已设置下次自动推进 时间=%s 间隔=%d分钟",
                          format_log_time(next_advance_at, story.setting.timezone),
                          interval_minutes)


InterludeService.schedule_next_automatic_advance = _schedule_next_automatic_advance  # type: ignore[attr-defined]


# ------------------------------------------------------------------ retries

async def _schedule_narrative_retry(
    self: "InterludeService",
    story_id: str,
    participant_id: str,
    now: datetime,
    previous_attempts: int = 0,
) -> bool:
    delay_seconds = max(5, self.config.runtime.narrative_retry_delay_seconds)
    max_attempts = max(0, self.config.runtime.narrative_retry_max_attempts)
    rows = await self.db.get(
        "interlude_intent", {"story_id": story_id, "participant_id": participant_id,
                             "status": "pending"},
    )
    existing = [NarrativeIntent.model_validate(r) for r in rows if r.get("type") == "narrative-retry"]
    if existing:
        await self.db.execute_many([
            ("UPDATE interlude_intent SET status='cancelled', updated_at=? WHERE id=?",
             (iso(now), intent.id)) for intent in existing
        ])
    if not participant_id or previous_attempts >= max_attempts:
        self.report_standalone("warn",
                               "叙事模型自动重试已停止 故事=%s 参与者=%s 已尝试=%d 上限=%d",
                               story_id, participant_id or "全局", previous_attempts, max_attempts)
        return False
    attempt = previous_attempts + 1
    not_before = now + timedelta(seconds=delay_seconds)
    await self.append_intent(story_id, {
        "type": "narrative-retry",
        "summary": f"Retry the interrupted narrative turn after provider failure (attempt {attempt}/{max_attempts}).",
        "not_before": iso(not_before),
        "payload": {"narrativeRetry": True, "userInitiated": True, "attempt": attempt},
    }, now, participant_id)
    self.report_standalone("warn",
                           "叙事模型请求失败，已安排自动重试 故事=%s 参与者=%s 次数=%d/%d 等待=%d秒",
                           story_id, participant_id, attempt, max_attempts, delay_seconds)
    return True


InterludeService.schedule_narrative_retry = _schedule_narrative_retry  # type: ignore[attr-defined]


# ------------------------------------------------------------------ agency check

async def _append_proactive_check(
    self: "InterludeService",
    story: InterludeStory,
    candidate: ProactiveContactDraft,
    not_before: datetime,
    reason: str,
    now: datetime,
) -> None:
    expires_at = parse_date(candidate.expires_at)
    if expires_at is None or expires_at <= now or not_before >= expires_at:
        return
    fingerprint = agency_mod.proactive_candidate_fingerprint(candidate)
    rows = await self.db.get(
        "interlude_intent",
        {"story_id": story.id, "participant_id": candidate.participant_id,
         "status": "pending", "type": "proactive-check"},
    )
    for row in rows:
        if isinstance(row.get("payload"), str):
            try:
                import json as _json

                row["payload"] = _json.loads(row["payload"])
            except ValueError:
                row["payload"] = {}
        payload = row.get("payload") or {}
        if payload.get("fingerprint") == fingerprint:
            self.report_operation("diagnostic", "debug", story, "advance",
                                  "Agency 主动联系候选去重 参与者=%s 指纹=%s",
                                  candidate.participant_id, fingerprint)
            return
    await self.append_intent(story.id, {
        "type": "proactive-check",
        "summary": f"Re-evaluate a life-grounded contact motive: {candidate.motive}",
        "not_before": iso(not_before),
        "participant_id": candidate.participant_id,
        "payload": {
            "origin": candidate.origin,
            "motive": candidate.motive,
            "disclosure": candidate.disclosure,
            "sourceEntryIds": candidate.source_entry_ids,
            "willingness": candidate.willingness,
            "expiresAt": candidate.expires_at,
            "fingerprint": fingerprint,
            "agencyReason": reason,
            "userInitiated": False,
        },
    }, now)
    self.schedule_due_intent_wake(story.id, not_before)
    self.report_operation("standard", "info", story, "advance",
                          "Agency 已安排主动联系重查 参与者=%s 时间=%s 原因=%s",
                          candidate.participant_id,
                          format_log_time(not_before, story.setting.timezone), reason)


InterludeService.append_proactive_check = _append_proactive_check  # type: ignore[attr-defined]


# ------------------------------------------------------------------ browser

def normalize_browser_intent(draft_or_dict: Any, config) -> Optional[dict[str, Any]]:
    from .normalize import normalize_browser_intent_loose

    normalized = normalize_browser_intent_loose(
        draft_or_dict.model_dump(mode="json")
        if hasattr(draft_or_dict, "model_dump")
        else draft_or_dict
    )
    if normalized is None:
        return None
    if normalized["mode"] == "search" and not config.allow_search:
        return None
    if normalized["mode"] == "visit" and not config.allow_visit:
        return None
    return normalized


async def _append_browser_intent(
    self: "InterludeService",
    story_id: str,
    draft: Any,
    now: datetime,
    fallback_participant_id: str = "",
) -> None:
    from .types import BrowserIntentDraft

    config = self.config.browser
    if not config.enabled:
        return
    normalized = normalize_browser_intent(draft, config)
    if normalized is None:
        return
    participant_id = fallback_participant_id
    allowed_participant = await self.get_participant(participant_id) if participant_id else None
    if participant_id and (allowed_participant is None or not self.can_handle_participant(allowed_participant)):
        return
    purpose = clip(normalized.get("purpose"), 500) or "The character planned to read a public web page."
    await self.append_intent(story_id, {
        "type": "browser-research",
        "summary": purpose,
        "not_before": iso(now + timedelta(seconds=1)),
        "payload": {
            "mode": normalized["mode"],
            "query": normalized.get("query") or "",
            "url": normalized.get("url") or "",
            "purpose": normalized.get("purpose"),
        },
    }, now, participant_id)


InterludeService.append_browser_intent = _append_browser_intent  # type: ignore[attr-defined]


async def _execute_deferred_browser_intent(
    self: "InterludeService",
    story: InterludeStory,
    intent: NarrativeIntent,
    now: datetime,
) -> Optional[WebObservation]:
    payload = intent.payload
    draft_data = {
        "mode": payload.get("mode"),
        "query": payload.get("query") or None,
        "url": payload.get("url") or None,
        "purpose": payload.get("purpose") or "The character planned to read a public web page.",
        "timing": "deferred",
    }
    normalized = normalize_browser_intent(draft_data, self.config.browser)
    draft = BrowserIntentDraft(**{
        "mode": normalized["mode"],
        "query": normalized.get("query"),
        "url": normalized.get("url"),
        "purpose": normalized["purpose"],
        "timing": "deferred",
    }) if normalized else None
    observation = await self.collect_web_observation(
        story, draft, intent.participant_id, intent.id, now
    )
    await self.db.update("interlude_intent", {"id": intent.id},
                         {"status": "completed", "updated_at": iso(self.now())})
    return observation


InterludeService.execute_deferred_browser_intent = _execute_deferred_browser_intent  # type: ignore[attr-defined]


async def _collect_web_observation(
    self: "InterludeService",
    story: InterludeStory,
    draft: Optional[BrowserIntentDraft],
    participant_id: str,
    intent_id: Optional[int],
    now: datetime,
    persist: bool = True,
) -> WebObservation:
    """Read-only bounded web observation via the injected fetcher."""
    config = self.config.browser

    async def save(status: str, url: str, title: str, excerpt: str, summary: str,
                   mode: str = "visit", query: str = "") -> WebObservation:
        return await self.save_web_observation(
            story.id, participant_id, intent_id, mode, query, url,
            title, excerpt, summary, status, now, persist,
        )

    if self.browser_fetch is None or not config.enabled:
        return await save("blocked", "", "", "",
                          "浏览未执行：功能未启用或请求不符合安全规则。",
                          mode=draft.mode if draft else "visit",
                          query=(draft.query if draft else "") or "")

    target = resolve_browser_target(draft, config) if draft else None
    if draft is None or target is None:
        return await save("blocked", (draft.url if draft else "") or "", "", "",
                          "浏览目标未通过公开网页安全校验。",
                          mode=draft.mode if draft else "visit",
                          query=(draft.query if draft else "") or "")
    cached = await self.find_cached_web_observation(story.id, participant_id, draft, now)
    if cached is not None:
        if not persist:
            return cached.model_copy(update={"id": 0, "intent_id": intent_id,
                                             "accessed_at": now, "created_at": now})
        await self.append_entry(story.id, ScriptEntryDraft(
            kind="web-observation", actor="system",
            content=f"The character revisited a recent web observation: "
                    f"{cached.title or cached.url}.",
            occurred_at=iso(now),
            metadata={"observationId": cached.id, "cached": True, "status": cached.status},
        ), now, participant_id)
        return cached

    def should_cancel(_target=None) -> bool:
        return False

    async def task() -> WebObservation:
        try:
            title, text = await self.browser_fetch(target, timeout_ms=config.navigation_timeout)
            text_bounded = clip(text, config.max_text_characters)
            title_bounded = clip(title, 500)
            excerpt = clip(text_bounded, config.max_excerpt_characters)
            summary = clip((f"{title_bounded}。" if title_bounded else "") + excerpt,
                           config.max_excerpt_characters)
            observation = await self.save_web_observation(
                story.id, participant_id, intent_id, draft.mode,
                draft.query or "", target, title_bounded, excerpt,
                summary or "页面没有可提取的正文。", "success", self.now(), persist,
            )
            self.report_operation("standard", "info", story, "intent-due",
                                  "网页读取完成 标题=%s 正文=%d字",
                                  title_bounded or "未命名页面", len(text_bounded))
            return observation
        except Exception as error:  # noqa: BLE001
            message = clip(str(error), 500)
            return await self.save_web_observation(
                story.id, participant_id, intent_id, draft.mode, draft.query or "",
                target, "", "", f"网页读取失败：{message}", "failed", self.now(), persist,
            )

    return await self.browser_slots.run(task)


InterludeService.collect_web_observation = _collect_web_observation  # type: ignore[attr-defined]


async def _save_web_observation(
    self: "InterludeService",
    story_id: str,
    participant_id: str,
    intent_id: Optional[int],
    mode: str,
    query: str,
    url: str,
    title: str,
    excerpt: str,
    summary: str,
    status: str,
    now: datetime,
    persist: bool = True,
) -> WebObservation:
    config = self.config.browser
    candidate = WebObservation(
        id=0, story_id=story_id, participant_id=participant_id, intent_id=intent_id,
        mode="search" if mode == "search" else "visit",
        query=clip(query, 500), url=clip(url, 2000), title=clip(title, 500),
        excerpt=clip(excerpt, config.max_excerpt_characters),
        summary=clip(summary, config.max_excerpt_characters),
        status=status, accessed_at=now, created_at=now,  # type: ignore[arg-type]
    )
    if not persist:
        return candidate
    observation_id = await self.db.insert_returning_id("interlude_web_observation", {
        key: value for key, value in candidate.model_dump(mode="json").items() if key != "id"
    })
    fetched = await self.db.fetch_one(
        "SELECT * FROM interlude_web_observation WHERE id=?",
        (observation_id,)
    )
    observation = WebObservation.model_validate(self.db.row_to_dict("interlude_web_observation", fetched)) \
        if fetched else candidate
    await self.append_entry(story_id, ScriptEntryDraft(
        kind="web-observation", actor="system",
        content=web_observation_entry_content(observation),
        occurred_at=iso(now),
        metadata={"observationId": observation.id, "status": status,
                  "mode": mode, "url": observation.url},
    ), now, participant_id)
    return observation


InterludeService.save_web_observation = _save_web_observation  # type: ignore[attr-defined]


async def _persist_collected_web_observation(self: "InterludeService", observation: WebObservation) -> None:
    await self.save_web_observation(
        observation.story_id, observation.participant_id, observation.intent_id,
        observation.mode, observation.query, observation.url, observation.title,
        observation.excerpt, observation.summary, observation.status,
        observation.accessed_at,
    )


InterludeService.persist_collected_web_observation = _persist_collected_web_observation  # type: ignore[attr-defined]


async def _find_cached_web_observation(
    self: "InterludeService",
    story_id: str,
    participant_id: str,
    draft: BrowserIntentDraft,
    now: datetime,
) -> Optional[WebObservation]:
    minutes = self.config.browser.cache_minutes
    if minutes <= 0:
        return None
    cutoff = now - timedelta(minutes=minutes)
    rows = await self.db.get(
        "interlude_web_observation",
        {"story_id": story_id, "participant_id": participant_id, "status": "success"},
        limit=20, order_by="accessed_at", descending=True,
    )
    observations = [WebObservation.model_validate(r) for r in rows]
    for observation in observations:
        if observation.accessed_at < cutoff:
            continue
        if observation.mode != draft.mode:
            continue
        if draft.mode == "search" and observation.query != (draft.query or ""):
            continue
        if draft.mode == "visit" and observation.url != (draft.url or ""):
            continue
        return observation
    return None


InterludeService.find_cached_web_observation = _find_cached_web_observation  # type: ignore[attr-defined]


def resolve_browser_target(draft: BrowserIntentDraft, config) -> Optional[str]:
    from urllib.parse import quote

    if draft.mode == "search":
        template = (config.search_url_template or "").strip()
        if not template or "{query}" not in template:
            return None
        target = template.replace("{query}", quote(draft.query or "", safe=""))
        from .agency import is_safe_public_web_url

        return target if is_safe_public_web_url(
            target, config.blocked_domains, config.allowed_domains
        ) else None
    from .agency import is_safe_public_web_url

    if draft.url and is_safe_public_web_url(draft.url, config.blocked_domains, config.allowed_domains):
        return draft.url
    return None


def web_observation_entry_content(observation: WebObservation) -> str:
    if observation.status == "success":
        source = observation.title or observation.url or "a public web page"
        return f"The character read a public web page: {source}."
    return (
        "The character's attempted web lookup did not complete: "
        + clip(observation.summary, 800)
    )


# ------------------------------------------------------------------ embed

async def _embed_text(self: "InterludeService", value: str) -> list[float]:
    try:
        return await self.embedder.embed(value)
    except Exception as error:  # noqa: BLE001
        logger.debug("Embedding 请求跳过：%s", error)
        return []


InterludeService.embed_text = _embed_text  # type: ignore[attr-defined]


# ------------------------------------------------------------------ compaction

def _schedule_compaction(self: "InterludeService", story_id: str) -> None:
    if not self.config.memory.enabled or story_id in self.scheduled_compactions:
        return
    self.scheduled_compactions.add(story_id)
    loop = asyncio.get_running_loop()

    async def run() -> None:
        try:
            if self.database_resetting:
                return
            if self.has_pending_narrative(story_id):
                await asyncio.sleep(0.5)
                if self.has_pending_narrative(story_id) or self.database_resetting:
                    return

            async def task() -> None:
                if self.has_pending_narrative(story_id):
                    return
                rows = await self.db.get("interlude_story", {"id": story_id})
                if rows:
                    story = InterludeStory.model_validate(rows[0])
                    await self.compact_unlocked(story, self.now(), force=False)

            await self.queues.run(story_id, task)
        except Exception as error:  # noqa: BLE001
            logger.debug("记忆压缩跳过：%s", error)
        finally:
            self.scheduled_compactions.discard(story_id)

    loop.create_task(run())


InterludeService.schedule_compaction = _schedule_compaction  # type: ignore[attr-defined]


async def _compact_story(self: "InterludeService", story: InterludeStory, force: bool = True) -> bool:
    if not self.can_handle_story(story):
        return False

    async def task() -> bool:
        rows = await self.db.get("interlude_story", {"id": story.id})
        fresh = InterludeStory.model_validate(rows[0]) if rows else story
        return await self.compact_unlocked(fresh, self.now(), force)

    return await self.queues.run(story.id, task)


InterludeService.compact_story = _compact_story  # type: ignore[attr-defined]


async def _compact_overlay(self: "InterludeService", story: InterludeStory) -> bool:
    if not self.can_handle_story(story):
        return False

    async def task() -> bool:
        rows = await self.db.get("interlude_story", {"id": story.id})
        fresh = InterludeStory.model_validate(rows[0]) if rows else story
        return await self.compact_overlay_unlocked(fresh, self.now())

    return await self.queues.run(story.id, task)


InterludeService.compact_overlay = _compact_overlay  # type: ignore[attr-defined]


async def _compact_stories_sweep(self: "InterludeService") -> None:
    if not self.config.memory.enabled or self.compaction_sweep_running:
        return
    self.compaction_sweep_running = True
    try:
        for story in await self.active_stories():
            if not self.can_handle_story(story):
                continue
            self.schedule_fact_embedding_backfill(story.id)
            self.schedule_compaction(story.id)
    finally:
        self.compaction_sweep_running = False


InterludeService.compact_stories = _compact_stories_sweep  # type: ignore[attr-defined]


async def _compact_unlocked(self: "InterludeService", story: InterludeStory, now: datetime, force: bool) -> bool:
    from .prompt_builder import compaction_prompt

    memory = self.config.memory
    await self.ensure_continuity(story, now)
    overlay_compacted = await self.compact_overlay_unlocked(story, now)
    scene = await self.active_scene(story.id)
    if scene is None:
        return overlay_compacted
    clauses = ["story_id = ?", "occurred_at >= ?"]
    params: list[Any] = [story.id, iso(scene.started_at)]
    if scene.last_entry_id is not None:
        clauses.append("id > ?")
        params.append(scene.last_entry_id)
    rows = await self.db.fetch_all(
        f"SELECT * FROM interlude_script_entry WHERE {' AND '.join(clauses)} "
        "ORDER BY occurred_at ASC LIMIT ?",
        [*params, max(memory.compaction_entry_limit * 2, memory.compaction_entry_limit)],
    )
    entries = [ScriptEntry.model_validate(self.db.row_to_dict("interlude_script_entry", r)) for r in rows]
    scene_entries = limit_entries_by_characters(entries, memory.compaction_character_limit)
    chars = sum(len(e.content) for e in scene_entries)
    if not force and len(scene_entries) < memory.scene_entry_threshold \
            and chars < memory.scene_character_threshold:
        self.report_operation("diagnostic", "debug", story, "advance",
                              "记忆整理跳过：未达到阈值 条目=%d/%d 字符=%d/%d",
                              len(scene_entries), memory.scene_entry_threshold,
                              chars, memory.scene_character_threshold)
        return overlay_compacted
    current_rows = await self.db.get("interlude_story", {"id": story.id})
    current = InterludeStory.model_validate(current_rows[0]) if current_rows else story
    participants_list = await self.participants(story.id)
    share_details = self.config.shared_story.share_participant_details

    visible_entries = (
        scene_entries if share_details
        else [
            e.model_copy(update={"participant_id": "",
                                 "content": "[participant-specific conversation omitted by privacy setting]"})
            if e.participant_id else e
            for e in scene_entries
        ]
    )
    visible_entries = [e for e in visible_entries if e.content.strip()]
    all_facts = await self.facts(story.id, memory.max_facts_per_story)
    visible_facts = all_facts if share_details else [f for f in all_facts if not f.participant_id]

    started = self.now()
    self.report_operation("standard", "info", story, "advance",
                          "记忆整理开始 条目=%d 字符=%d 强制=%s",
                          len(scene_entries), chars, force)
    try:
        decision_raw = await self.narrator.compact_raw(
            payload=build_compaction_payload(
                current, scene.started_at, now, visible_entries,
                scene, await self.active_arc(story.id),
                participants_list, visible_facts,
            ),
            system_prompt=compaction_prompt(
                self.config.prompts.fixed_prompt,
                self.config.prompts.compaction_prompt,
                self.config.prompts.compaction_fixed_prompt,
                self.config.prompts.compaction_style_prompt,
            ),
            temperature=0.3,
            top_p=1.0,
            max_tokens=2048,
            timeout_seconds=60.0,
            response_json=True,
        )
        from .types import CompactionDecision, FactDraft, StatePatchDraft

        decision = CompactionDecision.model_validate(decision_raw)
    except Exception as error:  # noqa: BLE001
        self.write_report("warn", story.setting.character.name, "advance",
                          "记忆压缩失败：%s", (error,))
        return False
    await self.persist_compaction(current, scene, decision, scene_entries, now)
    duration_ms = round((self.now() - started).total_seconds() * 1000)
    self.report_operation("standard", "info", story, "advance",
                          "记忆整理完成 耗时=%dms 剧本条目=%d 长期事实=%d 状态变更=%d",
                          duration_ms, len(scene_entries),
                          len(decision.facts), len(decision.state_patches))
    return True


InterludeService.compact_unlocked = _compact_unlocked  # type: ignore[attr-defined]


def build_compaction_payload(
    story: InterludeStory,
    from_time: datetime,
    now: datetime,
    entries: list[ScriptEntry],
    scene: Optional[InterludeScene],
    arc: Optional[InterludeArc],
    participants: list[InterludeParticipant],
    facts: list[NarrativeFact],
) -> dict[str, Any]:
    from .prompt_builder import participant_prompt_payload

    setting = story.setting.model_dump(mode="json")
    camel_setting = {
        "character": setting.get("character"),
        "user": {"displayName": "Multiple participants", "profile": ""},
        "relationship": "",
        "world": setting.get("world", ""),
        "supportingCast": setting.get("supporting_cast", ""),
        "location": setting.get("location", ""),
        "style": setting.get("style", ""),
        "timezone": setting.get("timezone", ""),
    }

    def scene_dict(s):
        if s is None:
            return None
        return {
            "id": s.id, "status": s.status.value, "startedAt": iso(s.started_at),
            "endedAt": iso(s.ended_at) if s.ended_at else None, "hook": s.hook,
            "summary": s.summary, "entryCount": s.entry_count,
            "lastEntryId": s.last_entry_id, "createdAt": iso(s.created_at),
            "updatedAt": iso(s.updated_at),
        }

    def arc_dict(a):
        if a is None:
            return None
        return {"id": a.id, "status": a.status, "title": a.title, "summary": a.summary,
                "sceneCount": a.scene_count, "createdAt": iso(a.created_at),
                "updatedAt": iso(a.updated_at)}

    from .prompt_builder import _story_state_for_payload

    return {
        "interval": {"from": iso(from_time), "now": iso(now)},
        "setting": camel_setting,
        "evolvingState": _story_state_for_payload(story.state),
        "scene": scene_dict(scene),
        "arc": arc_dict(arc),
        "participants": [participant_prompt_payload(p, False) for p in participants],
        "existingFacts": [
            {
                "participantId": f.participant_id, "scope": f.scope.value,
                "content": f.content, "importance": f.importance,
                "confidence": f.confidence, "unresolved": f.unresolved,
            }
            for f in facts
        ],
        "entries": [
            {
                "id": e.id, "participantId": e.participant_id, "kind": e.kind,
                "actor": e.actor, "content": e.content, "occurredAt": iso(e.occurred_at),
            }
            for e in entries
        ],
    }


async def _persist_compaction(
    self: "InterludeService",
    story: InterludeStory,
    scene: InterludeScene,
    decision,
    entries: list[ScriptEntry],
    now: datetime,
) -> None:
    memory = self.config.memory
    scene_patch = decision.scene or {}
    hook = clip(scene_patch.get("hook") or scene.hook, memory.scene_hook_characters) \
        if isinstance(scene_patch.get("hook") or scene.hook, str) else scene.hook
    summary_value = clip(scene_patch.get("summary") or scene.summary, memory.scene_summary_characters) \
        if isinstance(scene_patch.get("summary") or scene.summary, str) else scene.summary
    last_entry_id = entries[-1].id if entries else scene.last_entry_id
    await self.db.update("interlude_scene", {"id": scene.id}, {
        "hook": hook, "summary": summary_value, "entry_count": 0,
        "last_entry_id": last_entry_id, "updated_at": iso(now),
    })
    if scene_patch.get("close"):
        await self.db.update("interlude_scene", {"id": scene.id}, {
            "status": "closed", "ended_at": iso(now), "updated_at": iso(now),
        })
        await self.ensure_continuity(story, now)
    arc = await self.active_arc(story.id)
    arc_patch = decision.arc or {}
    if arc is not None and arc_patch:
        title = clip(arc_patch.get("title") or arc.title, 255)
        summary_arc = clip(arc_patch.get("summary") or arc.summary, memory.arc_summary_characters)
        await self.db.update("interlude_arc", {"id": arc.id}, {
            "title": title, "summary": summary_arc, "updated_at": iso(now),
        })
    entry_ids = {e.id for e in entries}
    for fact in decision.facts:
        source_ids = fact.source_entry_ids or []
        if not any(sid in entry_ids for sid in source_ids):
            continue
        await self.persist_fact(story.id, fact, entries, now)
    for patch in decision.state_patches:
        source_ids = patch.source_entry_ids or []
        if not any(sid in entry_ids for sid in source_ids):
            continue
        await self.persist_state_patch(story, patch, entries, now)


InterludeService.persist_compaction = _persist_compaction  # type: ignore[attr-defined]


async def _persist_fact(
    self: "InterludeService",
    story_id: str,
    draft,
    entries: list[ScriptEntry],
    now: datetime,
) -> None:
    memory = self.config.memory
    content = clip(draft.content, memory.fact_content_characters)
    if not content:
        return
    participant_id = resolve_participant_id(draft.participant_id, draft.source_entry_ids, entries)
    rows = await self.db.get("interlude_fact", {"story_id": story_id, "status": "active"})
    facts = [NarrativeFact.model_validate(r) for r in rows]
    normalized_new = normalize_fact_text(content)
    same = next((
        f for f in facts
        if normalize_fact_text(f.content) == normalized_new
        and (not f.participant_id or f.participant_id == participant_id)
    ), None)
    source_entry_ids = [
        sid for sid in (draft.source_entry_ids or [])
        if any(e.id == sid for e in entries)
    ][:20]
    unresolved = (
        draft.unresolved is True
        or (draft.unresolved is None and draft.scope == FactScope.PROMISE)
    )
    embedding = []
    if same is not None:
        embedding = same.embedding or await self.embed_text(content)
        importance = max(same.importance, clamp_number(draft.importance, same.importance, 0, 1))
        confidence = max(same.confidence, clamp_number(draft.confidence, same.confidence, 0, 1))
        merged_sources = list(dict.fromkeys([*same.source_entry_ids, *source_entry_ids]))
        update_data: dict[str, Any] = {
            "importance": importance,
            "confidence": confidence,
            "unresolved": 1 if (same.unresolved or unresolved) else 0,
            "source_entry_ids": merged_sources,
            "last_seen_at": iso(now),
            "updated_at": iso(now),
        }
        if embedding:
            update_data["embedding"] = embedding
        await self.db.update("interlude_fact", {"id": same.id}, update_data)
        return
    if len(facts) >= memory.max_facts_per_story and facts:
        weakest = min(facts, key=lambda f: f.importance * f.confidence)
        await self.db.update("interlude_fact", {"id": weakest.id},
                             {"status": "superseded", "updated_at": iso(now)})
    if not embedding:
        embedding = await self.embed_text(content)
    scope_value = draft.scope.value if hasattr(draft.scope, "value") else str(draft.scope)
    await self.db.insert("interlude_fact", {
        "story_id": story_id, "participant_id": participant_id, "scope": scope_value,
        "content": content,
        "importance": clamp_number(draft.importance, 0.5, 0, 1),
        "confidence": clamp_number(draft.confidence, 0.5, 0, 1),
        "unresolved": 1 if unresolved else 0,
        "embedding": embedding,
        "status": "active", "source_entry_ids": source_entry_ids,
        "last_seen_at": iso(now), "created_at": iso(now), "updated_at": iso(now),
    })


InterludeService.persist_fact = _persist_fact  # type: ignore[attr-defined]


def resolve_participant_id(explicit, source_entry_ids, entries: list[ScriptEntry]) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    ids = [e.participant_id for sid in (source_entry_ids or [])
           for e in entries if e.id == sid and e.participant_id]
    return ids[0] if ids else ""


def normalize_fact_text(value: str) -> str:
    import re as _re

    return _re.sub(r"\s+", " ", value.strip().lower())


def limit_entries_by_characters(entries: list[ScriptEntry], limit: int) -> list[ScriptEntry]:
    if limit <= 0:
        return []
    used = 0
    selected: list[ScriptEntry] = []
    for entry in reversed(entries):
        if selected and used + len(entry.content) > limit:
            break
        selected.insert(0, entry)
        used += len(entry.content)
    return selected


# ------------------------------------------------------------------ state patches & overlay

async def _persist_state_patch(
    self: "InterludeService",
    story: InterludeStory,
    draft,
    entries: list[ScriptEntry],
    now: datetime,
) -> None:
    memory = self.config.memory
    confidence = clamp_number(draft.confidence, 0, 0, 1)
    participant_id = resolve_participant_id(draft.participant_id, draft.source_entry_ids, entries)
    path = clip(draft.path, 255)
    source_entry_ids = [
        sid for sid in (draft.source_entry_ids or [])
        if any(e.id == sid for e in entries)
    ][:20]
    proposed_value = clip(draft.proposed_value if hasattr(draft, "proposed_value") else draft.proposedValue,
                          4_000)
    impact = draft.impact if getattr(draft, "impact", None) in ("minor", "major") else "minor"
    target_value = draft.target.value if hasattr(draft.target, "value") else str(draft.target)
    evidence_text = clip(getattr(draft, "evidence", "") or "", 4_000)
    if not path or not proposed_value or not source_entry_ids:
        return

    rows = await self.db.get("interlude_state_patch", {
        "story_id": story.id, "participant_id": participant_id,
        "target": target_value, "path": path,
    })
    candidates = [StatePatchProposal.model_validate(r) for r in rows]
    matching = [c for c in candidates if patch_claims_match(c.proposed_value, proposed_value)]
    if any(c.status.value in ("applied", "compacted") for c in matching):
        return
    candidate = next((c for c in matching if c.status.value == "proposed"), None)
    merged_source_ids = list(dict.fromkeys([
        *(candidate.source_entry_ids if candidate else []),
        *source_entry_ids,
    ]))[:80]
    source_rows = await self.db.get("interlude_script_entry",
                                    {"story_id": story.id, "id": merged_source_ids})
    source_entries = [ScriptEntry.model_validate(r) for r in source_rows]
    evidence = state_patch_evidence(source_entries, story.setting.timezone)

    minimum_turns = max(3, memory.state_patch_min_turns)
    minimum_days = max(1, memory.state_patch_min_days)
    minimum = (
        memory.major_state_patch_confidence_threshold
        if impact == "major"
        else memory.state_patch_confidence_threshold
    )
    merged_confidence = max(candidate.confidence if candidate else 0.0, confidence)
    merged_evidence_text = merge_note(
        candidate.evidence if candidate else None, evidence_text
    )
    proposal_id: Optional[int] = candidate.id if candidate is not None else None
    if candidate is None:
        proposal_id = await self.db.insert_returning_id("interlude_state_patch", {
            "story_id": story.id, "participant_id": participant_id,
            "target": target_value, "path": path, "proposed_value": proposed_value,
            "evidence": clip(merged_evidence_text, 4_000), "confidence": merged_confidence,
            "impact": impact, "status": "proposed",
            "source_entry_ids": merged_source_ids, "created_at": iso(now),
            "applied_at": None,
        })
    else:
        candidate_impact = (
            candidate.impact.value if hasattr(candidate.impact, "value") else str(candidate.impact)
        )
        await self.db.update("interlude_state_patch", {"id": candidate.id}, {
            "evidence": clip(merged_evidence_text, 4_000),
            "confidence": merged_confidence,
            "impact": "major" if (candidate_impact == "major" or impact == "major") else "minor",
            "source_entry_ids": merged_source_ids,
        })

    if not memory.auto_apply_state_patches \
            or (impact == "major" and not memory.allow_major_state_changes):
        return
    stable_evidence = (
        merged_confidence >= minimum
        if impact == "major"
        else merged_confidence >= minimum and evidence["turns"] >= minimum_turns
        and evidence["days"] >= minimum_days
    )
    if not stable_evidence:
        self.report_operation("diagnostic", "debug", story, "advance",
                              "Overlay 候选继续累计 目标=%s/%s 回合=%d/%d 日期=%d/%d",
                              target_value, path, evidence["turns"], minimum_turns,
                              evidence["days"], minimum_days)
        return
    cooldown_hours = max(1, memory.state_patch_cooldown_hours)
    applied_times = sorted(
        [
            (c.applied_at or c.created_at)
            for c in candidates if c.status.value in ("applied", "compacted")
        ],
        reverse=True,
    )
    recent_applied = applied_times[0] if applied_times else None
    if recent_applied is not None and (now - recent_applied).total_seconds() < cooldown_hours * HOUR:
        self.report_operation("diagnostic", "debug", story, "advance",
                              "Overlay 冷却中，候选保留 目标=%s/%s 冷却=%d小时",
                              target_value, path, cooldown_hours)
        return

    state = normalize_story_state(story.state)
    overlay = state.setting_overlay.model_copy(deep=True)
    if target_value == "character":
        if "trait" in path:
            traits = list(overlay.character_traits)
            traits.append(clip(proposed_value, 500))
            overlay.character_traits = list(dict.fromkeys(traits))[-30:]
        else:
            overlay.character_profile = merge_note(overlay.character_profile, proposed_value)
    elif target_value == "relationship" and participant_id:
        participant = await self.get_participant(participant_id)
        if participant is not None:
            p_state = normalize_participant_state(participant.state)
            new_overlay = merge_note(p_state.relationship_overlay, proposed_value)
            await self.db.update("interlude_participant", {"id": participant.id}, {
                "state": p_state.model_copy(
                    update={"relationship_overlay": new_overlay}
                ).model_dump(mode="json"),
                "updated_at": iso(now),
            })
    elif target_value == "relationship":
        overlay.relationship = merge_note(overlay.relationship, proposed_value)
    else:
        overlay.world = merge_note(overlay.world, proposed_value)
    if not (target_value == "relationship" and participant_id):
        await self.db.update("interlude_story", {"id": story.id}, {
            "state": state.model_copy(update={"setting_overlay": overlay}).model_dump(mode="json"),
            "updated_at": iso(now),
        })
    if proposal_id is not None:
        await self.db.update("interlude_state_patch", {"id": proposal_id},
                             {"status": "applied", "applied_at": iso(now)})


InterludeService.persist_state_patch = _persist_state_patch  # type: ignore[attr-defined]


def patch_claims_match(left: str, right: str) -> bool:
    import re as _re

    def strip(value: str) -> str:
        normalized = normalize_fact_text(value)
        return _re.sub(r"[，。！？、,.!?；;:：]", "", normalized)

    a, b = strip(left), strip(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 8 and (a in b or b in a)


def merge_note(existing: Optional[str], next_value: str) -> Optional[str]:
    value = clip(next_value, 2_000)
    if not value:
        return existing
    if not existing:
        return value
    if normalize_fact_text(existing).find(normalize_fact_text(value)) >= 0:
        return existing
    combined = f"{existing}\n{value}"
    return combined[-6_000:]


def state_patch_evidence(entries: list[ScriptEntry], timezone_name: str) -> dict[str, int]:
    narrative = [e for e in entries if e.kind == "script" or e.actor == "narrator"]
    turns = len({int(e.occurred_at.timestamp()) for e in narrative})
    days = len({calendar_day_key(e.occurred_at, timezone_name) for e in narrative})
    return {"turns": turns, "days": days}


async def _compact_overlay_unlocked(self: "InterludeService", story: InterludeStory, now: datetime) -> bool:
    from .prompt_builder import overlay_compaction_prompt
    from .types import OverlayCompactionDecision

    config = self.config.memory
    if not config.overlay_compression_enabled:
        return False
    try:
        recent_cutoff = now - timedelta(days=config.overlay_recent_days)
        monthly_cutoff = now - timedelta(days=config.overlay_monthly_after_days)
        rows = await self.db.get("interlude_state_patch",
                                 {"story_id": story.id, "status": "applied"},
                                 order_by="applied_at")
        applied = [StatePatchProposal.model_validate(r) for r in rows]
        weekly = [
            p for p in applied
            if (p.applied_at or p.created_at) <= recent_cutoff
        ]
        changed = False
        compaction_cfg = {
            "temperature": 0.3, "top_p": 1.0, "max_tokens": 2048, "timeout_seconds": 60.0,
        }
        for group in group_overlay_patches(weekly, config.overlay_weekly_window_days):
            existing = await self.db.fetch_one(
                "SELECT * FROM interlude_overlay_snapshot WHERE story_id=? AND participant_id=? "
                "AND target=? AND tier='weekly' AND period_start=? LIMIT 1",
                (story.id, group["participant_id"], group["target"], iso(group["from"])),
            )
            if existing:
                continue
            participant = (
                await self.get_participant(group["participant_id"])
                if group["participant_id"] else None
            )
            decision_raw = await self.narrator.compact_raw(
                payload=build_overlay_compaction_payload(
                    story, participant, StatePatchTarget(group["target"]), "weekly",
                    group["from"], group["to"], group["patches"], [],
                ),
                system_prompt=overlay_compaction_prompt(
                    self.config.prompts.fixed_prompt,
                    self.config.prompts.compaction_fixed_prompt,
                    self.config.prompts.compaction_style_prompt,
                ),
                **compaction_cfg,
            )
            decision = OverlayCompactionDecision.model_validate(decision_raw)
            summary = clip(decision.summary, config.overlay_weekly_summary_characters)
            if not summary:
                continue
            await self.db.insert("interlude_overlay_snapshot", {
                "story_id": story.id, "participant_id": group["participant_id"],
                "target": group["target"], "tier": "weekly",
                "period_start": iso(group["from"]), "period_end": iso(group["to"]),
                "summary": summary,
                "major_events": normalize_major_events(decision.major_events, group["patches"], []),
                "source_patch_ids": [p.id for p in group["patches"]],
                "status": "active", "created_at": iso(now), "updated_at": iso(now),
            })
            await self.db.execute_many([
                ("UPDATE interlude_state_patch SET status='compacted' WHERE id=?",
                 (p.id,)) for p in group["patches"]
            ])
            changed = True

        snapshot_rows = await self.db.get(
            "interlude_overlay_snapshot",
            {"story_id": story.id, "tier": "weekly", "status": "active"},
            order_by="period_end",
        )
        snapshots = [OverlaySnapshot.model_validate(r) for r in snapshot_rows]
        monthly_candidates = [s for s in snapshots if s.period_end <= monthly_cutoff]
        for group in group_overlay_snapshots(monthly_candidates, config.overlay_monthly_window_days):
            existing = await self.db.fetch_one(
                "SELECT * FROM interlude_overlay_snapshot WHERE story_id=? AND participant_id=? "
                "AND target=? AND tier='monthly' AND period_start=? LIMIT 1",
                (story.id, group["participant_id"], group["target"], iso(group["from"])),
            )
            if existing:
                continue
            participant = (
                await self.get_participant(group["participant_id"])
                if group["participant_id"] else None
            )
            decision_raw = await self.narrator.compact_raw(
                payload=build_overlay_compaction_payload(
                    story, participant, StatePatchTarget(group["target"]), "monthly",
                    group["from"], group["to"], [], group["snapshots"],
                ),
                system_prompt=overlay_compaction_prompt(
                    self.config.prompts.fixed_prompt,
                    self.config.prompts.compaction_fixed_prompt,
                    self.config.prompts.compaction_style_prompt,
                ),
                **compaction_cfg,
            )
            decision = OverlayCompactionDecision.model_validate(decision_raw)
            summary = clip(decision.summary, config.overlay_monthly_summary_characters)
            if not summary:
                continue
            await self.db.insert("interlude_overlay_snapshot", {
                "story_id": story.id, "participant_id": group["participant_id"],
                "target": group["target"], "tier": "monthly",
                "period_start": iso(group["from"]), "period_end": iso(group["to"]),
                "summary": summary,
                "major_events": normalize_major_events(
                    decision.major_events, [],
                    group["snapshots"],
                ),
                "source_patch_ids": [
                    pid for s in group["snapshots"] for pid in s.source_patch_ids
                ],
                "status": "active", "created_at": iso(now), "updated_at": iso(now),
            })
            await self.db.execute_many([
                ("UPDATE interlude_overlay_snapshot SET status='superseded', updated_at=? WHERE id=?",
                 (iso(now), s.id)) for s in group["snapshots"]
            ])
            changed = True
        if changed:
            fresh_rows = await self.db.get("interlude_story", {"id": story.id})
            fresh = InterludeStory.model_validate(fresh_rows[0]) if fresh_rows else story
            await self.rebuild_live_overlay_state(fresh, now)
            self.report_operation("standard", "info", story, "advance",
                                  "Overlay 分层归档完成：最近 %d 天保留原始补丁，短期窗口=%d天，长期窗口=%d天",
                                  config.overlay_recent_days, config.overlay_weekly_window_days,
                                  config.overlay_monthly_window_days)
        return changed
    except Exception as error:  # noqa: BLE001
        self.report_operation("standard", "warn", story, "advance",
                              "Overlay 分层归档跳过：%s", error)
        return False


InterludeService.compact_overlay_unlocked = _compact_overlay_unlocked  # type: ignore[attr-defined]


def build_overlay_compaction_payload(
    story: InterludeStory,
    participant: Optional[InterludeParticipant],
    target: StatePatchTarget,
    tier: str,
    from_time: datetime,
    to_time: datetime,
    patches: list[StatePatchProposal],
    snapshots: list[OverlaySnapshot],
) -> dict[str, Any]:
    target_value = target.value if hasattr(target, "value") else str(target)
    canon = ""
    if target_value == "character":
        canon = story.setting.character.profile
    elif target_value == "world":
        canon = story.setting.world
    else:
        canon = participant.relationship if participant else story.setting.relationship
    return {
        "tier": tier,
        "target": target_value,
        "participantId": participant.id if participant else "",
        "period": {"from": iso(from_time), "to": iso(to_time)},
        "canon": canon,
        "patches": [
            {
                "id": p.id, "value": p.proposed_value, "evidence": p.evidence,
                "impact": p.impact.value if hasattr(p.impact, "value") else str(p.impact),
                "appliedAt": iso(p.applied_at) if p.applied_at else None,
            }
            for p in patches
        ],
        "earlierSnapshots": [
            {
                "summary": s.summary, "majorEvents": s.major_events,
                "periodEnd": iso(s.period_end),
            }
            for s in snapshots
        ],
    }


def start_of_utc_window(value: datetime, window_days: int) -> datetime:
    size = max(1, int(window_days))
    epoch_day = int(value.timestamp() // DAY)
    start_day = (epoch_day // size) * size
    return datetime.fromtimestamp(start_day * DAY, tz=timezone.utc)


def group_overlay_patches(patches: list[StatePatchProposal], window_days: int = 5) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for patch in patches:
        anchor = patch.applied_at or patch.created_at
        from_time = start_of_utc_window(anchor, window_days)
        key = f"{patch.participant_id}|{patch.target.value}|{iso(from_time)}"
        if key not in groups:
            groups[key] = {
                "participant_id": patch.participant_id,
                "target": patch.target.value,
                "from": from_time,
                "to": from_time + timedelta(days=window_days),
                "patches": [],
            }
        groups[key]["patches"].append(patch)
    return list(groups.values())


def group_overlay_snapshots(snapshots: list[OverlaySnapshot], window_days: int = 10) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        from_time = start_of_utc_window(snapshot.period_end, window_days)
        key = f"{snapshot.participant_id}|{snapshot.target.value}|{iso(from_time)}"
        if key not in groups:
            groups[key] = {
                "participant_id": snapshot.participant_id,
                "target": snapshot.target.value,
                "from": from_time,
                "to": from_time + timedelta(days=window_days),
                "snapshots": [],
            }
        groups[key]["snapshots"].append(snapshot)
    return list(groups.values())


def normalize_major_events(value, patches: list[StatePatchProposal], snapshots: list[OverlaySnapshot]) -> list[str]:
    model_events = []
    if isinstance(value, list):
        model_events = [clip(item, 600) for item in value if isinstance(item, str)]
    retained = []
    for snapshot in snapshots:
        retained.extend(snapshot.major_events or [])
    for patch in patches:
        impact_value = patch.impact.value if hasattr(patch.impact, "value") else str(patch.impact)
        if impact_value == "major":
            retained.append(clip(patch.proposed_value or patch.evidence, 600))
    seen: set[str] = set()
    out: list[str] = []
    for item in [*retained, *model_events]:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out[-20:]


async def _rebuild_live_overlay_state(self: "InterludeService", story: InterludeStory, now: datetime) -> None:
    rows = await self.db.get("interlude_state_patch",
                             {"story_id": story.id, "status": "applied"})
    applied = [StatePatchProposal.model_validate(r) for r in rows]
    snap_rows = await self.db.get("interlude_overlay_snapshot",
                                  {"story_id": story.id, "status": "active"})
    snapshots = [OverlaySnapshot.model_validate(r) for r in snap_rows]
    state = normalize_story_state(story.state)
    overlay = state.setting_overlay.model_copy(deep=True)

    def has_global_history(target: str) -> bool:
        return any(
            s.target.value == target and not s.participant_id for s in snapshots
        )

    if has_global_history("character"):
        overlay.character_profile = None
        overlay.character_traits = []
        for patch in applied:
            if patch.participant_id or patch.target.value != "character":
                continue
            if "trait" in patch.path:
                overlay.character_traits.append(clip(patch.proposed_value, 500))
            else:
                overlay.character_profile = merge_note(overlay.character_profile, patch.proposed_value)
        overlay.character_traits = list(dict.fromkeys(overlay.character_traits))[-30:]
    if has_global_history("world"):
        overlay.world = None
        for patch in applied:
            if patch.participant_id or patch.target.value != "world":
                continue
            overlay.world = merge_note(overlay.world, patch.proposed_value)
    if has_global_history("relationship"):
        overlay.relationship = None
        for patch in applied:
            if patch.participant_id or patch.target.value != "relationship":
                continue
            overlay.relationship = merge_note(overlay.relationship, patch.proposed_value)
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": state.model_copy(update={"setting_overlay": overlay}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    relationship_ids = {
        s.participant_id for s in snapshots
        if s.target.value == "relationship" and s.participant_id
    }
    for participant_id in relationship_ids:
        participant = await self.get_participant(participant_id)
        if participant is None:
            continue
        p_state = normalize_participant_state(participant.state)
        new_overlay: Optional[str] = None
        for patch in applied:
            if patch.target.value == "relationship" and patch.participant_id == participant_id:
                new_overlay = merge_note(new_overlay, patch.proposed_value)
        await self.db.update("interlude_participant", {"id": participant.id}, {
            "state": p_state.model_copy(
                update={"relationship_overlay": new_overlay}
            ).model_dump(mode="json"),
            "updated_at": iso(now),
        })


InterludeService.rebuild_live_overlay_state = _rebuild_live_overlay_state  # type: ignore[attr-defined]


def _schedule_fact_embedding_backfill(self: "InterludeService", story_id: str) -> None:
    batch_size = self.config.models.embedding_backfill_batch_size
    if not self.config.models.embedding_model.strip() or batch_size <= 0:
        return
    if story_id in self.fact_backfills:
        return
    self.fact_backfills.add(story_id)

    async def run() -> None:
        try:
            rows = await self.db.get("interlude_fact",
                                     {"story_id": story_id, "status": "active"})
            facts = [NarrativeFact.model_validate(r) for r in rows]
            missing = sorted(
                [f for f in facts if not f.embedding],
                key=lambda f: f.updated_at.timestamp(), reverse=True,
            )[:batch_size]
            for fact in missing:
                embedding = await self.embed_text(fact.content)
                if embedding:
                    await self.db.update("interlude_fact", {"id": fact.id},
                                         {"embedding": embedding, "updated_at": iso(self.now())})
        except Exception as error:  # noqa: BLE001
            logger.debug("长期事实向量补齐跳过：%s", error)
        finally:
            self.fact_backfills.discard(story_id)

    asyncio.get_running_loop().create_task(run())


InterludeService.schedule_fact_embedding_backfill = _schedule_fact_embedding_backfill  # type: ignore[attr-defined]


# ------------------------------------------------------------------ admin ops

async def _clear_setting_overlay(
    self: "InterludeService",
    story: InterludeStory,
    target: str,
) -> dict[str, int]:
    self.invalidate_buffered_narratives(story.id)

    async def task() -> dict[str, int]:
        rows = await self.db.get("interlude_story", {"id": story.id})
        fresh = InterludeStory.model_validate(rows[0]) if rows else story
        return await self.clear_setting_overlay_unlocked(fresh, target)

    return await self.queues.run(story.id, task)


async def _clear_setting_overlay_unlocked(self: "InterludeService", story: InterludeStory, target: str) -> dict[str, int]:
    now = self.now()
    state = normalize_story_state(story.state)
    overlay = state.setting_overlay.model_copy(deep=True)
    if target in ("character", "all"):
        overlay.character_profile = None
        overlay.character_traits = []
    if target in ("relationship", "all"):
        overlay.relationship = None
    if target in ("world", "all"):
        overlay.world = None
    await self.db.update("interlude_story", {"id": story.id}, {
        "state": state.model_copy(update={"setting_overlay": overlay}).model_dump(mode="json"),
        "updated_at": iso(now),
    })
    participant_count = 0
    if target in ("relationship", "all"):
        for participant in await self.participants(story.id, include_paused=True):
            p_state = normalize_participant_state(participant.state)
            if not p_state.relationship_overlay:
                continue
            participant_count += 1
            await self.db.update("interlude_participant", {"id": participant.id}, {
                "state": p_state.model_copy(update={"relationship_overlay": None}).model_dump(mode="json"),
                "updated_at": iso(now),
            })
    rows = await self.db.get("interlude_state_patch", {"story_id": story.id})
    statements = []
    for row in rows:
        status_value = row.get("status")
        target_value = row.get("target")
        if status_value not in ("proposed", "applied", "compacted"):
            continue
        if target != "all" and target_value != target:
            continue
        statements.append((
            "UPDATE interlude_state_patch SET status='cleared' WHERE id=?",
            (row["id"],),
        ))
    snapshot_rows = await self.db.get("interlude_overlay_snapshot",
                                      {"story_id": story.id, "status": "active"})
    for row in snapshot_rows:
        if target != "all" and row.get("target") != target:
            continue
        statements.append((
            "UPDATE interlude_overlay_snapshot SET status='superseded', updated_at=? WHERE id=?",
            (iso(now), row["id"]),
        ))
    if statements:
        await self.db.execute_many(statements)
    return {"participant_count": participant_count}


InterludeService.clear_setting_overlay = _clear_setting_overlay  # type: ignore[attr-defined]
InterludeService.clear_setting_overlay_unlocked = _clear_setting_overlay_unlocked  # type: ignore[attr-defined]


async def _purge_all_story_data(self: "InterludeService", story_id: str) -> None:
    self.invalidate_buffered_narratives(story_id)
    redaction = {
        "interlude_script_entry": (
            {"kind": "redacted", "actor": "system",
             "content": "[管理员已删除剧本内容]", "metadata": '{"redacted": true}'},
            "occurred_at IS NOT NULL AND story_id = ?",
        ),
    }
    # Redact rather than physically delete where possible (audit parity).
    rows = await self.db.get("interlude_script_entry", {"story_id": story_id})
    statements = [
        ("UPDATE interlude_script_entry SET kind='redacted', actor='system', "
         "content=?, metadata=? WHERE id=?",
         ("[管理员已删除剧本内容]", '{"redacted": true}', r["id"]))
        for r in rows
    ]
    rows = await self.db.get("interlude_memory", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_memory SET status='deleted', content=? WHERE id=?",
         ("[管理员已删除记忆]", r["id"])) for r in rows
    ]
    rows = await self.db.get("interlude_intent", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_intent SET status='cancelled', summary=? WHERE id=?",
         ("[管理员已取消意图]", r["id"])) for r in rows
    ]
    rows = await self.db.get("interlude_scene", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_scene SET status='closed', hook='', summary='', entry_count=0 WHERE id=?",
         (r["id"],)) for r in rows
    ]
    rows = await self.db.get("interlude_arc", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_arc SET status='closed', summary='', scene_count=0 WHERE id=?",
         (r["id"],)) for r in rows
    ]
    rows = await self.db.get("interlude_fact", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_fact SET status='superseded', content=? WHERE id=?",
         ("[管理员已删除事实]", r["id"])) for r in rows
    ]
    rows = await self.db.get("interlude_state_patch", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_state_patch SET status='rejected', proposed_value=?, evidence='' WHERE id=?",
         ("[管理员已删除提案]", r["id"])) for r in rows
    ]
    rows = await self.db.get("interlude_overlay_snapshot", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_overlay_snapshot SET status='superseded', summary=?, major_events='[]', source_patch_ids='[]' WHERE id=?",
         ("[管理员已删除 overlay 归档]", r["id"])) for r in rows
    ]
    rows = await self.db.get("interlude_web_observation", {"story_id": story_id})
    statements += [
        ("UPDATE interlude_web_observation SET status='deleted', url='', title='', excerpt='', summary=? WHERE id=?",
         ("[管理员已删除网页观察]", r["id"])) for r in rows
    ]
    if statements:
        await self.db.execute_many(statements)
    now = self.now()
    setting = self.initial_story_setting()
    await self.db.update("interlude_story", {"id": story_id}, {
        "setting": setting.model_dump(mode="json"),
        "state": empty_story_state().model_dump(mode="json"),
        "cursor_at": iso(now), "updated_at": iso(now),
    })
    await self.reset_participant_canon(story_id, now)


InterludeService.purge_all_story_data = _purge_all_story_data  # type: ignore[attr-defined]


async def _reset_participant_canon(self: "InterludeService", story_id: str, now: datetime) -> None:
    rows = await self.db.get("interlude_participant", {"story_id": story_id})
    defaults = self.config.story_defaults
    statements = []
    for row in rows:
        account = self.user_rule(row.get("session_key") or "")
        person_id = (account.person_id.strip() if account else "") or row.get("person_id") or row.get("session_key") or ""
        display_name = (account.label.strip() if account else "") or row.get("display_name") or row.get("session_key") or ""
        profile = (account.profile.strip() if account else "") or defaults.user_profile
        relationship = (account.relationship.strip() if account else "") or defaults.relationship
        statements.append((
            "UPDATE interlude_participant SET person_id=?, display_name=?, profile=?, "
            "relationship=?, state=?, updated_at=? WHERE id=?",
            (person_id, display_name, profile, relationship,
             json.dumps(empty_participant_state().model_dump(), ensure_ascii=False),
             iso(now), row["id"]),
        ))
    if statements:
        await self.db.execute_many(statements)


InterludeService.reset_participant_canon = _reset_participant_canon  # type: ignore[attr-defined]


async def _purge_all_data(self: "InterludeService", preferred_story_id: Optional[str] = None) -> Optional[str]:
    rows = await self.db.get("interlude_story", {}, order_by="updated_at", descending=True)
    stories = [InterludeStory.model_validate(r) for r in rows]
    active = [s for s in stories if s.status.value == "active"]
    if not active:
        return None
    canonical = next((s for s in active if s.id == preferred_story_id), None) or active[0]
    now = self.now()
    for story in stories:
        await self.purge_all_story_data(story.id)
        if story.id != canonical.id:
            await self.db.update("interlude_story", {"id": story.id},
                                 {"status": "archived", "updated_at": iso(now)})
    fresh_rows = await self.db.get("interlude_story", {"id": canonical.id})
    if fresh_rows:
        canonical = InterludeStory.model_validate(fresh_rows[0])
        await self.ensure_continuity(canonical, now)
    return canonical.id


InterludeService.purge_all_data = _purge_all_data  # type: ignore[attr-defined]


async def _purge_platform_data(self: "InterludeService", platform: str) -> int:
    rows = await self.db.get("interlude_story", {}, order_by="updated_at", descending=True)
    stories = [InterludeStory.model_validate(r) for r in rows]
    normalized = str(platform).strip().lower()
    targets = [s for s in stories if s.platform_id.lower() == normalized]
    now = self.now()
    for story in targets:
        await self.purge_all_story_data(story.id)
        await self.db.update("interlude_story", {"id": story.id},
                             {"status": "archived", "updated_at": iso(now)})
    return len(targets)


InterludeService.purge_platform_data = _purge_platform_data  # type: ignore[attr-defined]


async def _purge_story_range(self: "InterludeService", story_id: str, from_time: datetime, to_time: datetime) -> None:
    self.invalidate_buffered_narratives(story_id)

    def in_range(value: Optional[datetime]) -> bool:
        return value is not None and from_time <= value <= to_time

    entries_rows = await self.db.get("interlude_script_entry", {"story_id": story_id})
    entry_ids = {r["id"] for r in entries_rows if in_range(parse_date(r.get("occurred_at")))}
    if entry_ids:
        marks = ",".join("?" for _ in entry_ids)
        await self.db.execute_many([
            (f"UPDATE interlude_script_entry SET kind='redacted', actor='system', content=?, metadata=? "
             f"WHERE id IN ({marks})",
             ["[管理员已删除剧本内容]", '{"redacted": true}', *entry_ids]),
        ])
    memory_rows = await self.db.get("interlude_memory", {"story_id": story_id})
    memory_statements = []
    for row in memory_rows:
        created = parse_date(row.get("created_at"))
        source_id = row.get("source_entry_id")
        if in_range(created) or (source_id is not None and source_id in entry_ids):
            memory_statements.append((
                "UPDATE interlude_memory SET status='deleted', content=? WHERE id=?",
                ("[管理员已删除记忆]", row["id"]),
            ))
    fact_rows = await self.db.get("interlude_fact", {"story_id": story_id})
    import json as _json

    for row in fact_rows:
        try:
            sources = _json.loads(row.get("source_entry_ids") or "[]")
        except ValueError:
            sources = []
        sourced = any(sid in entry_ids for sid in sources)
        if (in_range(parse_date(row.get("created_at")))
                or in_range(parse_date(row.get("updated_at")))
                or in_range(parse_date(row.get("last_seen_at"))) or sourced):
            memory_statements.append((
                "UPDATE interlude_fact SET status='superseded', content=? WHERE id=?",
                ("[管理员已删除事实]", row["id"]),
            ))
    intent_rows = await self.db.get("interlude_intent", {"story_id": story_id})
    for row in intent_rows:
        if (in_range(parse_date(row.get("created_at")))
                or in_range(parse_date(row.get("not_before")))
                or in_range(parse_date(row.get("updated_at")))):
            memory_statements.append((
                "UPDATE interlude_intent SET status='cancelled', summary=? WHERE id=?",
                ("[管理员已取消意图]", row["id"]),
            ))
    scene_rows = await self.db.get("interlude_scene", {"story_id": story_id})
    for row in scene_rows:
        started = parse_date(row.get("started_at"))
        ended = parse_date(row.get("ended_at"))
        overlaps = (
            started is not None and started <= to_time
            and (ended is None or ended >= from_time)
        )
        if overlaps:
            memory_statements.append((
                "UPDATE interlude_scene SET status='closed', hook='', summary='', entry_count=0 WHERE id=?",
                (row["id"],),
            ))
    patch_rows = await self.db.get("interlude_state_patch", {"story_id": story_id})
    for row in patch_rows:
        if in_range(parse_date(row.get("created_at"))) or in_range(parse_date(row.get("applied_at"))):
            memory_statements.append((
                "UPDATE interlude_state_patch SET status='rejected', proposed_value=?, evidence='' WHERE id=?",
                ("[管理员已删除提案]", row["id"]),
            ))
    observation_rows = await self.db.get("interlude_web_observation", {"story_id": story_id})
    for row in observation_rows:
        if in_range(parse_date(row.get("created_at"))) or in_range(parse_date(row.get("accessed_at"))):
            memory_statements.append((
                "UPDATE interlude_web_observation SET status='deleted', url='', title='', excerpt='', summary=? WHERE id=?",
                ("[管理员已删除网页观察]", row["id"]),
            ))
    arc_rows = await self.db.get("interlude_arc", {"story_id": story_id})
    for row in arc_rows:
        if in_range(parse_date(row.get("created_at"))) or in_range(parse_date(row.get("updated_at"))):
            memory_statements.append((
                "UPDATE interlude_arc SET status='closed', summary='', scene_count=0 WHERE id=?",
                (row["id"],),
            ))
    if memory_statements:
        await self.db.execute_many(memory_statements)
    story_rows = await self.db.get("interlude_story", {"id": story_id})
    if story_rows:
        await self.ensure_continuity(InterludeStory.model_validate(story_rows[0]), self.now())


InterludeService.purge_story_range = _purge_story_range  # type: ignore[attr-defined]


# ------------------------------------------------------------------ background loop

def _start_background_tasks(self: "InterludeService") -> None:
    if self._background_task is not None:
        return
    sweep_interval = max(1, self.config.runtime.sweep_interval_minutes) * MINUTE
    memory_interval = max(1, self.config.memory.background_interval_minutes) * MINUTE
    loop = asyncio.get_running_loop()

    async def sweep_loop() -> None:
        try:
            while True:
                await asyncio.sleep(sweep_interval)
                try:
                    await self.sweep()
                except Exception as error:  # noqa: BLE001
                    logger.warning("后台推进失败：%s", error)
        except asyncio.CancelledError:
            pass

    async def memory_loop() -> None:
        try:
            while True:
                await asyncio.sleep(memory_interval)
                try:
                    await self.compact_stories()
                except Exception as error:  # noqa: BLE001
                    logger.warning("后台记忆整理失败：%s", error)
        except asyncio.CancelledError:
            pass

    self._background_task = loop.create_task(sweep_loop())
    if self.config.memory.enabled:
        self._memory_task = loop.create_task(memory_loop())
    self.report_standalone("info", "后台调度已启动 剧本扫描=%d分钟 记忆扫描=%d分钟",
                           max(1, self.config.runtime.sweep_interval_minutes),
                           max(1, self.config.memory.background_interval_minutes))


async def _stop_background_tasks(self: "InterludeService") -> None:
    for attr in ("_background_task", "_memory_task"):
        task = getattr(self, attr)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            setattr(self, attr, None)
    for key in list(self.buffered_turns):
        turn = self.buffered_turns[key]
        _cancel_timer(turn.timer_task)
        turn.timer_task = None
    for key in list(self.due_wake_tasks):
        task = self.due_wake_tasks.pop(key)
        task.cancel()


InterludeService.start_background_tasks = _start_background_tasks  # type: ignore[attr-defined]
InterludeService.stop_background_tasks = _stop_background_tasks  # type: ignore[attr-defined]


def normalize_group_reply_local(raw: dict[str, Any], max_characters: int) -> str:
    """Extract a normalized group reply string from a raw decision payload."""
    if not isinstance(raw, dict):
        return ""
    interaction = raw.get("interaction")
    if not isinstance(interaction, dict):
        return ""
    reply = interaction.get("reply")
    if not isinstance(reply, dict) or reply.get("mode") != "immediate":
        return ""
    content = reply.get("content")
    return clip(content, max(1, max_characters)) if isinstance(content, str) else ""
