"""HDS Interlude domain types.

Python port of MomoiCore/HDS-Interlude ``src/types.ts`` (0.1.3-beta1).
Names, field meanings, bounds and defaults follow the original unless the
AstrBot environment required a documented change (see MIGRATION_NOTES.md).
"""

from __future__ import annotations

import enum
import math
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 / datetime coercion used across all boundaries."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if not math.isfinite(value):
                return None
            # JS Date semantics: numbers are epoch milliseconds.
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        candidate = text
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def iso(value: Any) -> str:
    date = parse_date(value)
    if date is None:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Model(BaseModel):
    """Base model: ignore unknown fields so old payloads never break reads."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- enums

class StoryStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class CharacterStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SceneStatus(str, enum.Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class IntentStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StatePatchTarget(str, enum.Enum):
    CHARACTER = "character"
    WORLD = "world"
    RELATIONSHIP = "relationship"


class StatePatchStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    COMPACTED = "compacted"
    REJECTED = "rejected"
    CLEARED = "cleared"


class FactScope(str, enum.Enum):
    CHARACTER = "character"
    WORLD = "world"
    RELATIONSHIP = "relationship"
    EVENT = "event"
    PROMISE = "promise"


class NarrativePhase(str, enum.Enum):
    ADVANCE = "advance"
    CONVERSATION_FOLLOW_UP = "conversation-follow-up"
    USER_MESSAGE = "user-message"
    INTENT_DUE = "intent-due"


PHASES: tuple[str, ...] = tuple(phase.value for phase in NarrativePhase)


AgencyActivityLoad = Literal["free", "occupied", "overloaded"]
AGENCY_ACTIVITY_LOADS: tuple[str, ...] = ("free", "occupied", "overloaded")
AgencyPrivacy = Literal["private", "shared", "public"]
AGENCY_PRIVACIES: tuple[str, ...] = ("private", "shared", "public")
AgencyDeviceAccess = Literal["available", "limited", "unavailable"]
AGENCY_DEVICE_ACCESS: tuple[str, ...] = ("available", "limited", "unavailable")

ProactiveContactOrigin = Literal[
    "life-event", "promise", "practical-update", "relationship-follow-up"
]
PROACTIVE_ORIGINS: tuple[str, ...] = (
    "life-event", "promise", "practical-update", "relationship-follow-up",
)
ProactiveDisclosure = Literal["ordinary", "personal"]
PROACTIVE_DISCLOSURES: tuple[str, ...] = ("ordinary", "personal")
ProactiveOutcome = Literal["send-now", "recheck-later", "let-go"]
PROACTIVE_OUTCOMES: tuple[str, ...] = ("send-now", "recheck-later", "let-go")

InteractionReplyMode = Literal["none", "immediate", "delayed"]

RecentScriptOwnership = Literal[
    "protagonist-narrative",
    "user-delivered-message",
    "protagonist-delivered-message",
    "external-group-message",
    "system-event",
]


# ---------------------------------------------------------------- canon

class CharacterSetting(_Model):
    name: str = "Unnamed character"
    profile: str = ""


class StorySetting(_Model):
    """Initial canon; only explicit configuration edits this. Model-driven long
    term change belongs to StoryState.setting_overlay."""

    character: CharacterSetting = Field(default_factory=CharacterSetting)
    user_display_name: str = ""
    user_profile: str = ""
    relationship: str = ""
    world: str = ""
    supporting_cast: str = ""
    location: str = ""
    style: str = "Realistic, restrained, and centered on ordinary life."
    timezone: str = "Asia/Shanghai"

    def merged(self, patch: dict[str, Any]) -> "StorySetting":
        data = self.model_dump()
        for key, value in patch.items():
            if key == "character" and isinstance(value, dict):
                data["character"] = {**data["character"], **value}
            elif key in data:
                data[key] = value
        return StorySetting.model_validate(data)

    # Prompt payload compatibility helpers -------------------------------
    @property
    def user(self) -> dict[str, str]:
        return {"displayName": self.user_display_name, "profile": self.user_profile}


class StorySettingOverlay(_Model):
    character_profile: Optional[str] = None
    relationship: Optional[str] = None
    world: Optional[str] = None
    supporting_cast: Optional[str] = None
    location: Optional[str] = None
    character_traits: list[str] = Field(default_factory=list)


class ContinuitySnapshot(_Model):
    current: str = ""
    next: list[str] = Field(default_factory=list)
    recent: list[str] = Field(default_factory=list)
    salient: list[str] = Field(default_factory=list)


class ParticipantState(_Model):
    open_threads: list[str] = Field(default_factory=list)
    relationship_notes: list[str] = Field(default_factory=list)
    relationship_overlay: Optional[str] = None
    unread_message_count: int = 0
    pending_reply_count: int = 0
    last_user_message_at: Optional[str] = None
    last_character_message_at: Optional[str] = None


class StoryAutomationState(_Model):
    quiet_until: Optional[str] = None
    next_advance_at: Optional[str] = None
    last_auto_advance_at: Optional[str] = None
    last_user_message_at: Optional[str] = None
    conversation_follow_up_at: list[str] = Field(default_factory=list)
    conversation_follow_up_participant_id: Optional[str] = None


class AgencyWindowState(_Model):
    activity_load: AgencyActivityLoad
    privacy: AgencyPrivacy
    device_access: AgencyDeviceAccess
    next_opportunity_at: Optional[str] = None
    valid_until: str
    basis: str
    source_entry_ids: list[int] = Field(default_factory=list)
    updated_at: str


# ---------------------------------------------------------------- Alter

class AlterHistoryEntry(_Model):
    turn: int
    phase: str  # NarrativePhase value
    alter: int
    alter_value: float
    timestamp: str


class EmotionalOffset(_Model):
    direction: Literal["serious", "relaxed"]
    description: str
    intensity: float
    generated_at: str


class EmotionalOffsetPrompt(EmotionalOffset):
    weight: float


class AlterSystemState(_Model):
    alter_value: float = 0
    alter_weight: float = 0
    last_trigger_direction: int = 0  # -1 | 0 | 1
    emotional_offset: Optional[EmotionalOffset] = None
    history: list[AlterHistoryEntry] = Field(default_factory=list)
    last_updated_at: str = Field(default_factory=lambda: iso(utcnow()))
    last_analysis_attempt_at: Optional[str] = None


# ---------------------------------------------------------------- rows

class CharacterRecord(_Model):
    id: str
    name: str
    avatar: str = ""
    description: str = ""
    story_id: str
    status: CharacterStatus = CharacterStatus.ACTIVE
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class ConversationBinding(_Model):
    id: Optional[int] = None
    platform_id: str = ""
    self_id: str = ""
    conversation_id: str = ""
    character_id: str
    created_at: datetime
    updated_at: datetime


class InterludeStory(_Model):
    id: str
    platform_id: str
    self_id: str
    status: StoryStatus = StoryStatus.ACTIVE
    setting: StorySetting = Field(default_factory=StorySetting)
    state: "StoryState" = None  # type: ignore[assignment]
    cursor_at: datetime
    created_at: datetime
    updated_at: datetime


class StoryState(_Model):
    setting_overlay: StorySettingOverlay = Field(default_factory=StorySettingOverlay)
    active_scene_id: Optional[int] = None
    active_arc_id: Optional[int] = None
    """Global continuity describes the protagonist's PUBLIC life state.
    It is refreshed only by unattended life turns (advance) so raw private
    conversation can never leak into another participant's prompt (P0-5)."""
    continuity_snapshot: Optional[ContinuitySnapshot] = None
    """Per-participant private continuity from relationship-scoped refreshes."""
    participant_continuity: dict[str, ContinuitySnapshot] = Field(default_factory=dict)
    narrative_update_count: int = 0
    last_continuity_update_at: Optional[str] = None
    automation: StoryAutomationState = Field(default_factory=StoryAutomationState)
    alter_system: Optional[AlterSystemState] = None
    agency_window: Optional[AgencyWindowState] = None


class InterludeParticipant(_Model):
    id: str
    story_id: str
    platform_id: str
    self_id: str
    session_key: str  # unified session id within the platform instance
    umo: str  # AstrBot unified_msg_origin; delivery target
    message_type: str  # MessageType.value, e.g. FriendMessage / GroupMessage
    person_id: str
    display_name: str
    profile: str
    relationship: str
    state: ParticipantState = Field(default_factory=ParticipantState)
    status: Literal["active", "paused", "archived"] = "active"
    created_at: datetime
    updated_at: datetime


class ScriptEntry(_Model):
    id: int
    story_id: str
    participant_id: str
    kind: str
    actor: str
    content: str
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class NarrativeMemory(_Model):
    id: int
    story_id: str
    participant_id: str
    category: str
    content: str
    importance: float
    status: str
    source_entry_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class InterludeScene(_Model):
    id: int
    story_id: str
    status: SceneStatus = SceneStatus.ACTIVE
    started_at: datetime
    ended_at: Optional[datetime] = None
    hook: str = ""
    summary: str = ""
    entry_count: int = 0
    last_entry_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class InterludeArc(_Model):
    id: int
    story_id: str
    status: Literal["active", "closed"] = "active"
    title: str = ""
    summary: str = ""
    scene_count: int = 0
    created_at: datetime
    updated_at: datetime


class StatePatchProposal(_Model):
    id: int
    story_id: str
    participant_id: str
    target: StatePatchTarget
    path: str
    proposed_value: str
    evidence: str
    confidence: float
    impact: Literal["minor", "major"] = "minor"
    status: StatePatchStatus = StatePatchStatus.PROPOSED
    source_entry_ids: list[int] = Field(default_factory=list)
    created_at: datetime
    applied_at: Optional[datetime] = None


class OverlaySnapshot(_Model):
    id: int
    story_id: str
    participant_id: str
    target: StatePatchTarget
    tier: Literal["weekly", "monthly"]
    period_start: datetime
    period_end: datetime
    summary: str
    major_events: list[str] = Field(default_factory=list)
    source_patch_ids: list[int] = Field(default_factory=list)
    status: Literal["active", "superseded"] = "active"
    created_at: datetime
    updated_at: datetime


class NarrativeFact(_Model):
    id: int
    story_id: str
    participant_id: str
    scope: FactScope
    content: str
    importance: float
    confidence: float
    unresolved: bool
    embedding: list[float] = Field(default_factory=list)
    status: Literal["active", "superseded"] = "active"
    source_entry_ids: list[int] = Field(default_factory=list)
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class NarrativeIntent(_Model):
    id: int
    story_id: str
    participant_id: str
    type: str
    summary: str
    not_before: datetime
    status: IntentStatus = IntentStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WebObservation(_Model):
    id: int
    story_id: str
    participant_id: str
    intent_id: Optional[int] = None
    mode: Literal["search", "visit"]
    query: str = ""
    url: str = ""
    title: str = ""
    excerpt: str = ""
    summary: str = ""
    status: Literal["success", "failed", "blocked", "deleted"]
    accessed_at: datetime
    created_at: datetime


# ---------------------------------------------------------------- drafts

class ScriptEntryDraft(_Model):
    kind: str
    actor: Optional[str] = None
    content: str
    occurred_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryDraft(_Model):
    category: str
    content: str
    importance: Optional[float] = None
    participant_id: Optional[str] = None


class IntentDraft(_Model):
    type: str
    summary: str
    not_before: str
    payload: dict[str, Any] = Field(default_factory=dict)
    participant_id: Optional[str] = None


class IntentUpdateDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    status: Literal["completed", "cancelled"]
    resolution: Optional[str] = None


class OutgoingMessageDraft(BaseModel):
    participant_id: str
    content: str
    """Intent row staged for this delivery (P0-1 outbox). The visible
    character-message ScriptEntry is written only after real transport
    success; on failure the intent is cancelled and nothing was "said"."""
    delivery_intent_id: Optional[int] = None
    """Participant counter snapshot taken when the reply was composed.
    Finalize subtracts these so user messages that arrive DURING transport
    are never erased from unread/pending accounting (P0-D)."""
    baseline_unread: Optional[int] = None
    baseline_pending: Optional[int] = None


class BrowserIntentDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: Literal["search", "visit"]
    query: Optional[str] = None
    url: Optional[str] = None
    purpose: str
    timing: Literal["deferred", "immediate"] = "deferred"
    participant_id: Optional[str] = None


class ConversationActionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    participant_id: str
    mode: Literal["immediate", "delayed"]
    content: str
    send_at: Optional[str] = None
    willingness: Optional[float] = None
    reason: Optional[str] = None


class ProactiveContactDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    participant_id: str
    origin: ProactiveContactOrigin
    motive: str
    disclosure: ProactiveDisclosure
    source_entry_ids: list[int] = Field(default_factory=list)
    willingness: Optional[float] = None
    outcome: ProactiveOutcome
    not_before: Optional[str] = None
    expires_at: Optional[str] = None


class AgencyConfig(BaseModel):
    enabled: bool = True
    max_window_minutes: int = 240
    minimum_proactive_interval_minutes: int = 60
    max_candidate_hours: int = 24


DEFAULT_AGENCY_CONFIG = AgencyConfig()


class NarrativeInteraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seen: bool
    reply: "NarrativeReply"


class NarrativeReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: InteractionReplyMode = "none"
    content: Optional[str] = None
    send_at: Optional[str] = None


NarrativeInteraction.model_rebuild()


class NarrativeDecision(BaseModel):
    """Normalized result of one main narrative model call.

    Built by hdsi.normalize.normalize_decision from raw model JSON.
    """

    script: str = ""
    alter: Optional[int] = None
    agency_window: Optional[dict[str, Any]] = None
    proactive_contact: Optional[dict[str, Any]] = None
    interaction: Optional[NarrativeInteraction] = None
    continuity: Optional[ContinuitySnapshot] = None
    memories: list[MemoryDraft] = Field(default_factory=list)
    intents: list[IntentDraft] = Field(default_factory=list)
    intent_updates: list[IntentUpdateDraft] = Field(default_factory=list)
    browser_intents: list[BrowserIntentDraft] = Field(default_factory=list)
    state_patch: Optional[dict[str, list[str]]] = None
    cross_conversation_actions: list[ConversationActionDraft] = Field(default_factory=list)


class GroupMessageContext(BaseModel):
    sender_id: str
    sender_name: str
    content: str
    occurred_at: datetime
    direction: Optional[Literal["user", "character"]] = None


class GroupContext(BaseModel):
    group_id: str
    channel_id: str
    label: str
    purpose: str
    character_role: str
    messages: list[GroupMessageContext] = Field(default_factory=list)


class NarrativeImage(BaseModel):
    id: str
    mime_type: str
    data_uri: str


class SceneContext(BaseModel):
    scene: Optional[InterludeScene]
    arc: Optional[InterludeArc]


class AlterAnalysisRequest(BaseModel):
    character_name: str
    trigger_value: float
    threshold: float
    direction: Literal["serious", "relaxed"]
    recent_scripts: list[dict[str, str]]
    history: list[AlterHistoryEntry]
    setting_overlay: StorySettingOverlay
    current_offset: Optional[EmotionalOffsetPrompt]


class AlterAnalysisDecision(BaseModel):
    description: str


class CompactionRequest(BaseModel):
    story: InterludeStory
    from_time: datetime
    now: datetime
    entries: list[ScriptEntry]
    scene: Optional[InterludeScene]
    arc: Optional[InterludeArc]
    participants: list[InterludeParticipant]
    facts: list[NarrativeFact]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FactDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scope: FactScope
    participant_id: Optional[str] = None
    content: str
    importance: Optional[float] = None
    confidence: Optional[float] = None
    unresolved: Optional[bool] = None
    source_entry_ids: list[int] = Field(default_factory=list)


class StatePatchDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: StatePatchTarget
    participant_id: Optional[str] = None
    path: str
    proposed_value: str
    evidence: str
    confidence: Optional[float] = None
    impact: Literal["minor", "major"] = "minor"
    source_entry_ids: list[int] = Field(default_factory=list)


class CompactionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scene: Optional[dict[str, Any]] = None
    arc: Optional[dict[str, Any]] = None
    facts: list[FactDraft] = Field(default_factory=list)
    state_patches: list[StatePatchDraft] = Field(default_factory=list)


class OverlayCompactionRequest(BaseModel):
    story: InterludeStory
    participant: Optional[InterludeParticipant]
    target: StatePatchTarget
    tier: Literal["weekly", "monthly"]
    from_time: datetime
    to_time: datetime
    patches: list[StatePatchProposal]
    snapshots: list[OverlaySnapshot] = Field(default_factory=list)


class OverlayCompactionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    major_events: Optional[list[str]] = None


class NarrativeRequest(BaseModel):
    phase: NarrativePhase
    refresh_continuity: bool = False
    story: InterludeStory
    from_time: datetime
    now: datetime
    user_message: Optional[str] = None
    images: list[NarrativeImage] = Field(default_factory=list)
    participant: Optional[InterludeParticipant]
    participants: list[InterludeParticipant] = Field(default_factory=list)
    share_participant_details: bool = False
    due_intents: list[NarrativeIntent] = Field(default_factory=list)
    active_consequences: list[NarrativeIntent] = Field(default_factory=list)
    superseded_intents: list[NarrativeIntent] = Field(default_factory=list)
    recent_entries: list[ScriptEntry] = Field(default_factory=list)
    memories: list[NarrativeMemory] = Field(default_factory=list)
    scene_context: Optional[SceneContext] = None
    facts: list[NarrativeFact] = Field(default_factory=list)
    overlay_snapshots: list[OverlaySnapshot] = Field(default_factory=list)
    web_context: list[WebObservation] = Field(default_factory=list)
    group_context: Optional[GroupContext] = None
    alter_enabled: bool = False
    emotional_offset: Optional[EmotionalOffsetPrompt] = None
    agency_enabled: bool = False
    agency_window: Optional[AgencyWindowState] = None


# ---------------------------------------------------------------- factories

def empty_story_setting() -> StorySetting:
    return StorySetting(
        character=CharacterSetting(name="Unnamed character", profile=""),
        user_display_name="",
        user_profile="",
        relationship="",
        world="",
        supporting_cast="",
        location="",
        style="Realistic, restrained, and centered on ordinary life.",
        timezone="Asia/Shanghai",
    )


def empty_story_state() -> StoryState:
    return StoryState(
        setting_overlay=StorySettingOverlay(character_traits=[]),
        automation=StoryAutomationState(),
        narrative_update_count=0,
    )


def empty_participant_state() -> ParticipantState:
    return ParticipantState(
        open_threads=[],
        relationship_notes=[],
        unread_message_count=0,
        pending_reply_count=0,
    )


# InterludeStory.state forward reference resolution
InterludeStory.model_rebuild()


def normalize_story_state(value: Any) -> StoryState:
    """Read-time normalization mirroring service.ts normalizeStoryState."""
    if isinstance(value, StoryState):
        return value
    record = value if isinstance(value, dict) else {}
    overlay_raw = record.get("settingOverlay") or record.get("setting_overlay")
    overlay = overlay_raw if isinstance(overlay_raw, dict) else {}

    def _opt_str(key_ts: str, key_py: str | None = None) -> Optional[str]:
        keys = [key_ts] + ([key_py] if key_py and key_py != key_ts else [])
        for key in keys:
            raw = overlay.get(key)
            if isinstance(raw, str):
                return raw
        return None

    automation_raw = record.get("automation")
    automation = automation_raw if isinstance(automation_raw, dict) else {}
    continuity_raw = record.get("continuitySnapshot") or record.get("continuity_snapshot")
    participant_continuity_raw = (
        record.get("participantContinuity") or record.get("participant_continuity")
    )
    participant_continuity: dict[str, ContinuitySnapshot] = {}
    if isinstance(participant_continuity_raw, dict):
        for pid_key, snap_value in participant_continuity_raw.items():
            snap = normalize_continuity_snapshot(snap_value)
            if snap is not None and isinstance(pid_key, str) and pid_key:
                participant_continuity[pid_key] = snap

    alter_raw = record.get("alterSystem") or record.get("alter_system")
    agency_raw = record.get("agencyWindow") or record.get("agency_window")

    count_raw = record.get("narrativeUpdateCount", record.get("narrative_update_count", 0))
    try:
        count = max(0, math.floor(float(count_raw)))
    except (TypeError, ValueError):
        count = 0

    follow_ups_raw = automation.get("conversationFollowUpAt", automation.get("conversation_follow_up_at", []))
    follow_ups = [item for item in follow_ups_raw if isinstance(item, str)][:8] if isinstance(follow_ups_raw, list) else []

    follow_pid = automation.get("conversationFollowUpParticipantId", automation.get("conversation_follow_up_participant_id"))

    quiet_until = automation.get("quietUntil", automation.get("quiet_until"))
    next_advance = automation.get("nextAdvanceAt", automation.get("next_advance_at"))
    last_auto = automation.get("lastAutoAdvanceAt", automation.get("last_auto_advance_at"))
    last_user = automation.get("lastUserMessageAt", automation.get("last_user_message_at"))

    traits_raw = overlay.get("characterTraits", overlay.get("character_traits"))
    traits = [
        item for item in traits_raw if isinstance(item, str)
    ] if isinstance(traits_raw, list) else []

    return StoryState(
        setting_overlay=StorySettingOverlay(
            character_profile=_opt_str("characterProfile", "character_profile"),
            relationship=_opt_str("relationship"),
            world=_opt_str("world"),
            supporting_cast=_opt_str("supportingCast", "supporting_cast"),
            location=_opt_str("location"),
            character_traits=traits,
        ),
        active_scene_id=record.get("activeSceneId", record.get("active_scene_id")),
        active_arc_id=record.get("activeArcId", record.get("active_arc_id")),
        continuity_snapshot=normalize_continuity_snapshot(continuity_raw),
        participant_continuity=participant_continuity,
        narrative_update_count=count,
        last_continuity_update_at=(
            record.get("lastContinuityUpdateAt") or record.get("last_continuity_update_at")
        ) if isinstance(record.get("lastContinuityUpdateAt", record.get("last_continuity_update_at")), str) else None,
        automation=StoryAutomationState(
            quiet_until=quiet_until if isinstance(quiet_until, str) else None,
            next_advance_at=next_advance if isinstance(next_advance, str) else None,
            last_auto_advance_at=last_auto if isinstance(last_auto, str) else None,
            last_user_message_at=last_user if isinstance(last_user, str) else None,
            conversation_follow_up_at=follow_ups,
            conversation_follow_up_participant_id=(
                str(follow_pid)[:255] if isinstance(follow_pid, str) and follow_pid else None
            ),
        ),
        alter_system=normalize_alter_state(alter_raw),
        agency_window=normalize_agency_window_state(agency_raw),
    )


def normalize_continuity_snapshot(value: Any) -> Optional[ContinuitySnapshot]:
    if isinstance(value, ContinuitySnapshot):
        return value
    if not isinstance(value, dict):
        return None

    def _text(item: Any, limit: int) -> str:
        return str(item).strip()[:limit] if isinstance(item, str) else ""

    def _list(item: Any, limit: int, size: int) -> list[str]:
        if not isinstance(item, list):
            return []
        return [t for t in (_text(v, limit) for v in item) if t][:size]

    current = _text(value.get("current"), 500)
    nxt = _list(value.get("next"), 300, 3)
    recent = _list(value.get("recent"), 300, 5)
    salient = _list(value.get("salient"), 400, 5)
    if not current and not nxt and not recent and not salient:
        return None
    return ContinuitySnapshot(current=current, next=nxt, recent=recent, salient=salient)


def normalize_alter_state(value: Any) -> Optional[AlterSystemState]:
    from . import alter as alter_mod

    if isinstance(value, AlterSystemState):
        return value
    return alter_mod.normalize_alter_system_state(value)


def normalize_agency_window_state(value: Any) -> Optional[AgencyWindowState]:
    from . import agency as agency_mod

    if isinstance(value, AgencyWindowState):
        return value
    return agency_mod.normalize_agency_window_state(value)


def normalize_participant_state(value: Any) -> ParticipantState:
    """Read-time normalization mirroring service.ts normalizeParticipantState."""
    if isinstance(value, ParticipantState):
        return value
    record = value if isinstance(value, dict) else {}

    def _strings(key_ts: str, key_py: str) -> list[str]:
        raw = record.get(key_ts, record.get(key_py))
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                clipped = item.strip()[:500]
                if clipped:
                    out.append(clipped)
        return out[:50]

    def _count(key_ts: str, key_py: str) -> int:
        raw = record.get(key_ts, record.get(key_py))
        try:
            return max(0, math.floor(float(raw))) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    overlay = record.get("relationshipOverlay", record.get("relationship_overlay"))
    last_user = record.get("lastUserMessageAt", record.get("last_user_message_at"))
    last_char = record.get("lastCharacterMessageAt", record.get("last_character_message_at"))
    return ParticipantState(
        open_threads=_strings("openThreads", "open_threads"),
        relationship_notes=_strings("relationshipNotes", "relationship_notes"),
        relationship_overlay=str(overlay)[:4000] if isinstance(overlay, str) else None,
        unread_message_count=_count("unreadMessageCount", "unread_message_count"),
        pending_reply_count=_count("pendingReplyCount", "pending_reply_count"),
        last_user_message_at=last_user if isinstance(last_user, str) else None,
        last_character_message_at=last_char if isinstance(last_char, str) else None,
    )


def clip(value: Any, length: int) -> str:
    return value.strip()[:length] if isinstance(value, str) else ""


def clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return fallback
    try:
        if math.isnan(value):
            return fallback
    except TypeError:
        return fallback
    return max(minimum, min(maximum, value))
