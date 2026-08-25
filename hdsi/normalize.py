"""Decision normalization. Port of service.ts normalizeDecision and helpers.

Every field the model returns is validated, clamped or dropped before it can
touch the database. Future leakage is impossible by construction here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from .types import (
    IntentDraft,
    MemoryDraft,
    NarrativeDecision,
    NarrativeInteraction,
    NarrativeReply,
    clip,
    clamp_number,
    iso,
    parse_date,
)


def normalize_interaction(
    value: Any,
    now: datetime,
    runtime_max_message_characters: int,
    minimum_delayed_reply_seconds: int,
    maximum_delayed_reply_minutes: int,
) -> Optional[NarrativeInteraction]:
    if not isinstance(value, dict):
        return None
    seen = value.get("seen")
    reply = value.get("reply")
    if not isinstance(seen, bool) or not isinstance(reply, dict):
        return None
    mode = reply.get("mode")
    if mode not in ("none", "immediate", "delayed"):
        return None
    content_raw = reply.get("content")
    content = content_raw.strip()[:runtime_max_message_characters] if isinstance(content_raw, str) else None
    send_at = parse_date(reply.get("sendAt", reply.get("send_at")))

    if not seen:
        return NarrativeInteraction(seen=False, reply=NarrativeReply(mode="none"))
    if mode == "none":
        return NarrativeInteraction(seen=True, reply=NarrativeReply(mode="none"))
    if not content:
        return NarrativeInteraction(seen=True, reply=NarrativeReply(mode="none"))
    if mode == "immediate":
        return NarrativeInteraction(seen=True, reply=NarrativeReply(mode=mode, content=content))
    delay_seconds = (send_at - now).total_seconds() if send_at is not None else None
    if (
        send_at is None
        or delay_seconds is None
        or delay_seconds < minimum_delayed_reply_seconds
        or delay_seconds > maximum_delayed_reply_minutes * 60
    ):
        return NarrativeInteraction(seen=True, reply=NarrativeReply(mode="none"))
    return NarrativeInteraction(
        seen=True,
        reply=NarrativeReply(mode="delayed", content=content, send_at=iso(send_at)),
    )


def valid_memory(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("category"), str)
        and isinstance(value.get("content"), str)
        and bool(value.get("content", "").strip())
    )


def consequence_expires_at(payload: Any) -> Optional[datetime]:
    if not isinstance(payload, dict):
        return None
    expires = payload.get("expiresAt", payload.get("expires_at"))
    return parse_date(expires)


def is_active_consequence_draft(value: Any) -> bool:
    payload = value.get("payload") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and value.get("type") in ("active-consequence",)
        and isinstance(payload, dict)
        and payload.get("lifecycle") == "active"
    )


def valid_intent(
    value: Any,
    from_time: datetime,
    now: datetime,
    active_consequences_enabled: bool,
    max_consequence_days: int = 7,
) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("type"), str) or not isinstance(value.get("summary"), str):
        return False
    not_before = parse_date(value.get("notBefore", value.get("not_before")))
    if not_before is None:
        return False
    if not is_active_consequence_draft(value):
        return not_before > now
    payload = value.get("payload") or {}
    effect = payload.get("effect").strip() if isinstance(payload.get("effect"), str) else ""
    strength = payload.get("strength")
    strength_ok = (
        strength is None
        or (isinstance(strength, (int, float)) and not isinstance(strength, bool) and 0 <= strength <= 1)
    )
    maximum_lifetime = max(1, max_consequence_days) * 86400
    expires_at = consequence_expires_at(payload)
    return bool(
        active_consequences_enabled
        and effect
        and strength_ok
        and from_time <= not_before <= now
        and expires_at is not None
        and expires_at > now
        and (expires_at - now).total_seconds() <= maximum_lifetime
    )


def normalize_intent_updates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        status = item.get("status")
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
            continue
        if status not in ("completed", "cancelled"):
            continue
        entry: dict[str, Any] = {"id": item_id, "status": status}
        resolution = item.get("resolution")
        if isinstance(resolution, str) and resolution.strip():
            entry["resolution"] = clip(resolution, 1000)
        out.append(entry)
    return out[:8]


def normalize_browser_intent_loose(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    purpose = value.get("purpose")
    if mode not in ("search", "visit") or not isinstance(purpose, str):
        return None
    query = clip(value.get("query"), 500) if isinstance(value.get("query"), str) else ""
    url = clip(value.get("url"), 2000) if isinstance(value.get("url"), str) else ""
    if mode == "search" and not query:
        return None
    if mode == "visit" and not url:
        return None
    timing = "immediate" if value.get("timing") == "immediate" else "deferred"
    participant_id = value.get("participantId", value.get("participant_id"))
    out: dict[str, Any] = {
        "mode": mode,
        "purpose": clip(purpose, 500),
        "timing": timing,
    }
    if query:
        out["query"] = query
    if url:
        out["url"] = url
    if isinstance(participant_id, str) and participant_id.strip():
        out["participant_id"] = participant_id.strip()
    return out


def permitted_or_global(
    value: Any,
    fallback: str,
    permitted_participant_ids: set[str],
) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate and candidate in permitted_participant_ids:
        return candidate
    if fallback and fallback in permitted_participant_ids:
        return fallback
    return ""


def normalize_conversation_action(
    value: Any,
    runtime: dict[str, Any],
    permitted_participant_ids: set[str],
    current_participant_id: str,
    now: datetime,
    proactive: bool = False,
) -> Optional[dict[str, Any]]:
    """Validate one cross-conversation action draft."""
    if not isinstance(value, dict):
        return None
    participant_id = value.get("participantId", value.get("participant_id"))
    if not isinstance(participant_id, str) or not participant_id or participant_id == current_participant_id:
        return None
    if participant_id not in permitted_participant_ids:
        return None
    mode = value.get("mode")
    if mode not in ("immediate", "delayed"):
        return None
    content = value.get("content")
    content = content.strip()[: runtime["max_message_characters"]] if isinstance(content, str) else ""
    if not content:
        return None
    willingness_raw = value.get("willingness")
    willingness: Optional[float] = None
    if isinstance(willingness_raw, (int, float)) and not isinstance(willingness_raw, bool):
        try:
            willingness = clamp_number(float(willingness_raw), 0, 0, 1)
        except Exception:
            willingness = None
    threshold = float(runtime.get("proactive_willingness_threshold", 0.65))
    if proactive and (willingness is None or willingness < threshold):
        return None
    reason = clip(value.get("reason"), 300) if isinstance(value.get("reason"), str) else None
    base: dict[str, Any] = {"participant_id": participant_id, "mode": mode, "content": content}
    if willingness is not None:
        base["willingness"] = willingness
    if reason:
        base["reason"] = reason
    if mode == "immediate":
        return base
    send_at_raw = value.get("sendAt", value.get("send_at"))
    send_at = parse_date(send_at_raw)
    if send_at is None:
        return None
    delay = (send_at - now).total_seconds()
    if (
        delay < float(runtime.get("minimum_delayed_reply_seconds", 10))
        or delay > float(runtime.get("maximum_delayed_reply_minutes", 1440)) * 60
    ):
        return None
    base["send_at"] = iso(send_at)
    return base


def pick_participant_state_patch(value: dict[str, Any]) -> Optional[dict[str, list[str]]]:
    patch: dict[str, list[str]] = {}
    open_threads = value.get("openThreads", value.get("open_threads"))
    if isinstance(open_threads, list) and all(isinstance(i, str) for i in open_threads):
        patch["open_threads"] = [clip(i, 500) for i in open_threads][:50]
    notes = value.get("relationshipNotes", value.get("relationship_notes"))
    if isinstance(notes, list) and all(isinstance(i, str) for i in notes):
        patch["relationship_notes"] = [clip(i, 500) for i in notes][:50]
    return patch or None


def normalize_continuity(value: Any):
    from .types import ContinuitySnapshot

    if not isinstance(value, dict):
        return None

    def text(item: Any, limit: int) -> str:
        return clip(item, limit).strip() if isinstance(item, str) else ""

    def as_list(item: Any, limit: int, size: int) -> list[str]:
        if not isinstance(item, list):
            return []
        return [t for t in (text(v, limit) for v in item) if t][:size]

    current = text(value.get("current"), 500)
    nxt = as_list(value.get("next"), 300, 3)
    recent = as_list(value.get("recent"), 300, 5)
    salient = as_list(value.get("salient"), 400, 5)
    if not current and not nxt and not recent and not salient:
        return None
    return ContinuitySnapshot(current=current, next=nxt, recent=recent, salient=salient)


def normalize_decision(
    raw: dict[str, Any],
    from_time: datetime,
    now: datetime,
    permit_messages: bool,
    runtime: dict[str, Any],
    shared_story: dict[str, Any],
    current_participant_id: str,
    permitted_participant_ids: set[str],
    phase: str,
    memory: dict[str, Any] | None = None,
    refresh_continuity: bool = False,
) -> tuple[NarrativeDecision, Optional[Any], Optional[Any]]:
    """Normalize a raw model decision.

    Returns (decision, agency_window_raw, proactive_contact_raw); the two raw
    payloads are further validated by hdsi.agency inside persist_decision.
    """
    from .alter import normalize_alter_value

    script = clip(raw.get("script"), runtime["max_script_characters"]) if isinstance(raw.get("script"), str) else ""

    interaction = (
        None if phase == "advance"
        else normalize_interaction(
            raw.get("interaction"),
            now,
            int(runtime["max_message_characters"]),
            int(runtime["minimum_delayed_reply_seconds"]),
            int(runtime["maximum_delayed_reply_minutes"]),
        )
    )

    memories_raw = raw.get("memories")
    memories: list[MemoryDraft] = []
    if isinstance(memories_raw, list):
        for item in memories_raw:
            if valid_memory(item):
                memories.append(MemoryDraft(
                    category=str(item.get("category")),
                    content=str(item.get("content")),
                    importance=item.get("importance") if isinstance(item.get("importance"), (int, float)) else None,
                    participant_id=permitted_or_global(
                        item.get("participantId", item.get("participant_id")),
                        current_participant_id,
                        permitted_participant_ids,
                    ),
                ))

    intents: list[IntentDraft] = []
    intents_raw = raw.get("intents")
    if isinstance(intents_raw, list):
        for item in intents_raw:
            if not valid_intent(
                item,
                from_time,
                now,
                bool((memory or {}).get("active_consequences_enabled", True)),
                int((memory or {}).get("active_consequence_max_days", 7)),
            ):
                continue
            payload = item.get("payload")
            intents.append(IntentDraft(
                type=str(item.get("type")),
                summary=str(item.get("summary"))[:4000],
                not_before=iso(parse_date(item.get("notBefore", item.get("not_before")))),
                payload=payload if isinstance(payload, dict) else {},
                participant_id=permitted_or_global(
                    item.get("participantId", item.get("participant_id")),
                    current_participant_id,
                    permitted_participant_ids,
                ),
            ))
    intents = intents[:8]

    browser_intents_raw = raw.get("browserIntents")
    browser_intents = []
    if isinstance(browser_intents_raw, list):
        for item in browser_intents_raw:
            normalized = normalize_browser_intent_loose(item)
            if normalized:
                browser_intents.append(normalized)
    browser_intents = browser_intents[:1]

    cross_actions_raw = raw.get("crossConversationActions")
    agency_gated_proactive = phase == "advance" and not isinstance(raw.get("proactiveContact"), dict)
    cross_actions = []
    if (
        permit_messages
        and shared_story.get("allow_cross_conversation_messages", True)
        and isinstance(cross_actions_raw, list)
    ):
        for item in cross_actions_raw:
            normalized = normalize_conversation_action(
                item,
                runtime,
                permitted_participant_ids,
                current_participant_id,
                now,
                agency_gated_proactive,
            )
            if normalized:
                cross_actions.append(normalized)
    max_cross = int(shared_story.get("max_cross_conversation_actions", 1))
    cross_actions = cross_actions[: max(0, max_cross)]

    state_patch_raw = raw.get("statePatch")
    state_patch = pick_participant_state_patch(state_patch_raw) if isinstance(state_patch_raw, dict) else None

    continuity = normalize_continuity(raw.get("continuity")) if refresh_continuity else None
    alter = normalize_alter_value(raw.get("alter"))

    decision = NarrativeDecision(script=script)
    decision.alter = alter
    decision.interaction = interaction
    decision.continuity = continuity
    decision.memories = memories
    decision.intents = intents
    updates = normalize_intent_updates(raw.get("intentUpdates", raw.get("intent_updates")))
    from .types import IntentUpdateDraft

    decision.intent_updates = [
        IntentUpdateDraft(**update) for update in updates
    ]
    from .types import BrowserIntentDraft

    decision.browser_intents = [BrowserIntentDraft(**item) for item in browser_intents]
    decision.state_patch = state_patch
    from .types import ConversationActionDraft

    decision.cross_conversation_actions = [
        ConversationActionDraft(**{
            "participant_id": a["participant_id"],
            "mode": a["mode"],
            "content": a["content"],
            "send_at": a.get("send_at"),
            "willingness": a.get("willingness"),
            "reason": a.get("reason"),
        })
        for a in cross_actions
    ]

    agency_window_raw = raw.get("agencyWindow") if isinstance(raw.get("agencyWindow"), dict) else None
    proactive_raw = raw.get("proactiveContact") if isinstance(raw.get("proactiveContact"), dict) else None
    return decision, agency_window_raw, proactive_raw


def group_due_intents(intents: list) -> list[list]:
    """Port of groupDueIntents: one relationship branch (+agency family) per batch."""
    batches: dict[str, list] = {}
    ordered = sorted(intents, key=lambda i: (i.not_before, i.id))
    for intent in ordered:
        family = "agency" if intent.type == "proactive-check" else "normal"
        key = f"{intent.participant_id or '__global__'}|{family}"
        batches.setdefault(key, []).append(intent)
    return list(batches.values())
