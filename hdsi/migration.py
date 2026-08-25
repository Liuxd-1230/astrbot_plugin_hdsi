"""Migration tools: Koishi HDS-Interlude → astrbot_plugin_hdsi.

Two layers:
1. ``migrate_koishi_config`` maps the original Console configuration
   (camelCase) onto this plugin's full config dict (snake_case).
2. ``import_koishi_database`` reads a Koishi instance's SQLite file and
   imports the 11 domain tables row by row, preserving ids, timestamps,
   participants and privacy-relevant participant_id scoping so a running
   story continues seamlessly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _to_snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper():
            lower = char.lower()
            if index > 0 and (not name[index - 1].isupper() or (
                index + 1 < len(name) and name[index + 1].islower()
            )):
                out.append("_")
            out.append(lower)
        else:
            out.append(char)
    return "".join(out)


def deep_snake(data: Any) -> Any:
    if isinstance(data, dict):
        return {_to_snake(key): deep_snake(value) for key, value in data.items()}
    if isinstance(data, list):
        return [deep_snake(item) for item in data]
    return data


# ------------------------------------------------------------------ config

CONFIG_SECTION_MAP = {
    "storyDefaults": "story_defaults",
    "sharedStory": "shared_story",
    "alterSystem": "alter_system",
}

RUNTIME_KEY_MAP = {
    "captureDirectMessages": "capture_direct_messages",
    "autoCreate": "auto_create",
    "ignoreCommandMessages": "ignore_command_messages",
    "allowProactiveMessages": "allow_proactive_messages",
    "proactiveWillingnessThreshold": "proactive_willingness_threshold",
    "sweepIntervalMinutes": "sweep_interval_minutes",
    "minimumAdvanceMinutes": "minimum_advance_minutes",
    "contextEntryLimit": "context_entry_limit",
    "memoryLimit": "memory_limit",
    "maxScriptCharacters": "max_script_characters",
    "maxMessageCharacters": "max_message_characters",
    "minimumDelayedReplySeconds": "minimum_delayed_reply_seconds",
    "maximumDelayedReplyMinutes": "maximum_delayed_reply_minutes",
    "cancelDelayedRepliesOnUserMessage": "cancel_delayed_replies_on_user_message",
    "narrativeRetryDelaySeconds": "narrative_retry_delay_seconds",
    "narrativeRetryMaxAttempts": "narrative_retry_max_attempts",
    "splitReplyMessages": "split_reply_messages",
    "messageSeparator": "message_separator",
    "typingBaseDelaySeconds": "typing_base_delay_seconds",
    "typingCharactersPerSecond": "typing_characters_per_second",
    "typingMaxDelaySeconds": "typing_max_delay_seconds",
    "userMessageDebounceSeconds": "user_message_debounce_seconds",
    "autoAdvanceEnabled": "auto_advance_enabled",
    "autoAdvanceIntervalMinutes": "auto_advance_interval_minutes",
    "autoAdvanceJitterMinutes": "auto_advance_jitter_minutes",
    "conversationFollowUpMinutes": "conversation_follow_up_minutes",
    "conversationFollowUpJitterMinutes": "conversation_follow_up_jitter_minutes",
    "restWindows": "rest_windows",
}

REST_WINDOW_KEY_MAP = {
    "enabled": "enabled", "label": "label", "start": "start", "end": "end",
    "minIntervalMinutes": "min_interval_minutes",
    "maxIntervalMinutes": "max_interval_minutes",
}


def _map_keys(source: dict[str, Any], key_map: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for old, new in key_map.items():
        if old in source:
            value = source[old]
            if new == "rest_windows" and isinstance(value, list):
                value = [
                    {new_key: item.get(old_key)
                     for old_key, new_key in REST_WINDOW_KEY_MAP.items()
                     if old_key in item}
                    for item in value if isinstance(item, dict)
                ]
            out[new] = value
    return out


def migrate_koishi_config(koishi_config: dict[str, Any]) -> dict[str, Any]:
    """Convert an original HDS-Interlude Console config into ours."""
    if not isinstance(koishi_config, dict):
        raise ValueError("Koishi 配置必须是 JSON 对象")
    result: dict[str, Any] = {}

    defaults = koishi_config.get("storyDefaults") or {}
    if defaults:
        result["story_defaults"] = {
            "character_name": defaults.get("characterName"),
            "character_profile": defaults.get("characterProfile"),
            "user_profile": defaults.get("userProfile"),
            "relationship": defaults.get("relationship"),
            "world": defaults.get("world"),
            "supporting_cast": defaults.get("supportingCast"),
            "location": defaults.get("location"),
            "style": defaults.get("style"),
            "timezone": defaults.get("timezone"),
        }
        result["story_defaults"] = {
            key: value for key, value in result["story_defaults"].items() if value is not None
        }

    model = koishi_config.get("model") or {}
    prompts_patch: dict[str, Any] = {}
    if isinstance(model.get("mainPrompt"), str):
        prompts_patch["main_prompt"] = model["mainPrompt"]
    if isinstance(model.get("stylePrompt"), str):
        prompts_patch["style_prompt"] = model["stylePrompt"]
    if isinstance(model.get("fixedPrompt"), str):
        prompts_patch["fixed_prompt"] = model["fixedPrompt"]
    if isinstance(model.get("formatPrompt"), str):
        prompts_patch["format_prompt"] = model["formatPrompt"]
    compaction = model.get("compaction") or {}
    if isinstance(compaction.get("mainPrompt"), str):
        prompts_patch["compaction_prompt"] = compaction["mainPrompt"]
    if isinstance(compaction.get("fixedPrompt"), str):
        prompts_patch["compaction_fixed_prompt"] = compaction["fixedPrompt"]
    if isinstance(compaction.get("stylePrompt"), str):
        prompts_patch["compaction_style_prompt"] = compaction["stylePrompt"]
    if prompts_patch:
        result["prompts"] = prompts_patch

    runtime = koishi_config.get("runtime")
    if isinstance(runtime, dict):
        mapped_runtime = _map_keys(runtime, RUNTIME_KEY_MAP)
        if mapped_runtime:
            result["runtime"] = mapped_runtime

    memory = koishi_config.get("memory")
    if isinstance(memory, dict):
        result["memory"] = deep_snake(memory)

    alter = koishi_config.get("alterSystem")
    if isinstance(alter, dict):
        mapped_alter = deep_sncale_helper(alter)
        # modelId/model/providerId collapse into a slot expression.
        slot = ""
        if mapped_alter.get("provider_id"):
            slot = str(mapped_alter["provider_id"])
            if mapped_alter.get("model"):
                slot += ":" + str(mapped_alter["model"])
        elif mapped_alter.get("model_id"):
            slot = str(mapped_alter["model_id"])
        mapped_alter.pop("model_id", None)
        mapped_alter.pop("provider_id", None)
        mapped_alter.pop("model", None)
        if slot:
            mapped_alter["model_slot"] = slot
        result["alter_system"] = mapped_alter

    agency = koishi_config.get("agency")
    if isinstance(agency, dict):
        result["agency"] = deep_snake({
            key: value for key, value in agency.items() if not isinstance(value, (dict, list))
        })

    shared = koishi_config.get("sharedStory")
    if isinstance(shared, dict):
        mapped_shared = deep_snake({
            key: value for key, value in shared.items()
            if not isinstance(value, (dict, list)) or key == "managerAccounts"
        })
        result.setdefault("shared_story", {}).update(mapped_shared)

    onebot = koishi_config.get("onebot")
    gate: dict[str, Any] = {}
    if isinstance(onebot, dict):
        bot_rules = [
            {"id": rule.get("qq"), "label": rule.get("label", ""), "enabled": rule.get("enabled", True)}
            for rule in onebot.get("botAccounts", []) if isinstance(rule, dict)
        ]
        user_rules = [
            {
                "id": rule.get("qq"),
                "label": rule.get("label", ""),
                "person_id": rule.get("personId", ""),
                "profile": rule.get("profile", ""),
                "relationship": rule.get("relationship", ""),
                "enabled": rule.get("enabled", True),
            }
            for rule in onebot.get("userAccounts", []) if isinstance(rule, dict)
        ]
        group_rules = [
            {
                "id": rule.get("groupId"),
                "label": rule.get("label", ""),
                "purpose": rule.get("purpose", ""),
                "character_role": rule.get("characterRole", ""),
                "response_mode": rule.get("responseMode", "mention-only"),
                "context_limit": rule.get("contextLimit", 20),
                "debounce_seconds": rule.get("debounceSeconds", 1),
                "cooldown_seconds": rule.get("cooldownSeconds", 60),
                "enabled": rule.get("enabled", True),
            }
            for rule in onebot.get("groupChats", []) if isinstance(rule, dict)
        ]
        if bot_rules:
            gate["bot_accounts"] = bot_rules
        if user_rules:
            gate["user_accounts"] = user_rules
        if group_rules:
            gate["group_chats"] = group_rules
        if "ignoreSelfMessages" in onebot:
            gate["ignore_self_messages"] = bool(onebot["ignoreSelfMessages"])

    browser = koishi_config.get("browser")
    if isinstance(browser, dict):
        mapped_browser = deep_snake(browser)
        result["browser"] = mapped_browser

    vision_enabled = bool((model.get("vision") or {}).get("enabled"))
    embedding = model.get("embedding") or {}
    models_section: dict[str, Any] = {}
    if vision_enabled:
        models_section["vision_enabled"] = True
    if isinstance(embedding, dict) and embedding.get("enabled"):
        models_section["embedding_live_query"] = bool(embedding.get("liveQuery"))
        if embedding.get("dimensions"):
            models_section["embedding_dimensions"] = int(embedding["dimensions"])
    if models_section:
        result["models"] = models_section
    if gate:
        result["platform_gate"] = gate
    return result


def deep_sncale_helper(value: dict[str, Any]) -> dict[str, Any]:
    return deep_snake(value)


# ------------------------------------------------------------------ database

KOISHI_TABLES: tuple[str, ...] = (
    "interlude_story",
    "interlude_participant",
    "interlude_script_entry",
    "interlude_memory",
    "interlude_intent",
    "interlude_scene",
    "interlude_arc",
    "interlude_fact",
    "interlude_state_patch",
    "interlude_overlay_snapshot",
    "interlude_web_observation",
)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def import_koishi_database(
    koishi_db_path: str | Path,
    target_database: Any,
    *,
    overwrite: bool = False,
) -> dict[str, int]:
    """Import rows from a Koishi instance's SQLite file.

    Column names keep their original camelCase; they are translated to this
    plugin's snake_case schema on insert. Story/participant ids and
    participant scoping are preserved so privacy boundaries survive.
    """
    source = sqlite3.connect(f"file:{Path(koishi_db_path)}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        for table in KOISHI_TABLES:
            try:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                counts[table] = 0
                continue
            imported = 0
            for row in rows:
                record = {key: _normalize_value(row[key]) for key in row.keys()}
                converted = convert_koishi_row(table, record)
                if converted is None:
                    continue
                existing = target_database.get(table, {"id": converted["id"]})
                if existing and not overwrite:
                    continue
                if existing and overwrite:
                    target_database.update(table, {"id": converted["id"]}, converted)
                else:
                    target_database.insert(table, converted)
                imported += 1
            counts[table] = imported
    finally:
        source.close()
    return counts


COLUMN_TRANSLATION: dict[str, dict[str, str]] = {
    "interlude_story": {
        "id": "id", "platform": "platform_id", "selfId": "self_id",
        "userId": "", "channelId": "", "status": "status",
        "setting": "setting", "state": "state",
        "cursorAt": "cursor_at", "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_participant": {
        "id": "id", "storyId": "story_id", "platform": "platform_id",
        "selfId": "self_id", "userId": "session_key", "channelId": "",
        "personId": "person_id", "displayName": "display_name",
        "profile": "profile", "relationship": "relationship",
        "state": "state", "status": "status",
        "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_script_entry": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "kind": "kind", "actor": "actor", "content": "content",
        "occurredAt": "occurred_at", "metadata": "metadata", "createdAt": "created_at",
    },
    "interlude_memory": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "category": "category", "content": "content", "importance": "importance",
        "status": "status", "sourceEntryId": "source_entry_id",
        "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_intent": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "type": "type", "summary": "summary", "notBefore": "not_before",
        "status": "status", "payload": "payload",
        "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_scene": {
        "id": "id", "storyId": "story_id", "status": "status",
        "startedAt": "started_at", "endedAt": "ended_at", "hook": "hook",
        "summary": "summary", "entryCount": "entry_count",
        "lastEntryId": "last_entry_id", "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_arc": {
        "id": "id", "storyId": "story_id", "status": "status", "title": "title",
        "summary": "summary", "sceneCount": "scene_count",
        "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_fact": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "scope": "scope", "content": "content", "importance": "importance",
        "confidence": "confidence", "unresolved": "unresolved",
        "embedding": "embedding", "status": "status",
        "sourceEntryIds": "source_entry_ids", "lastSeenAt": "last_seen_at",
        "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_state_patch": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "target": "target", "path": "path", "proposedValue": "proposed_value",
        "evidence": "evidence", "confidence": "confidence", "impact": "impact",
        "status": "status", "sourceEntryIds": "source_entry_ids",
        "createdAt": "created_at", "appliedAt": "applied_at",
    },
    "interlude_overlay_snapshot": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "target": "target", "tier": "tier", "periodStart": "period_start",
        "periodEnd": "period_end", "summary": "summary",
        "majorEvents": "major_events", "sourcePatchIds": "source_patch_ids",
        "status": "status", "createdAt": "created_at", "updatedAt": "updated_at",
    },
    "interlude_web_observation": {
        "id": "id", "storyId": "story_id", "participantId": "participant_id",
        "intentId": "intent_id", "mode": "mode", "query": "query", "url": "url",
        "title": "title", "excerpt": "excerpt", "summary": "summary",
        "status": "status", "accessedAt": "accessed_at", "createdAt": "created_at",
    },
}

JSON_COLUMNS = {
    "interlude_story": {"setting", "state"},
    "interlude_participant": {"state"},
    "interlude_script_entry": {"metadata"},
    "interlude_intent": {"payload"},
    "interlude_fact": {"embedding", "source_entry_ids"},
    "interlude_state_patch": {"source_entry_ids"},
    "interlude_overlay_snapshot": {"major_events", "source_patch_ids"},
}

BOOL_COLUMNS = {"interlude_fact": {"unresolved"}}

STORY_ID_REWRITE_NOTE = (
    "Koishi 故事 id 若为旧版 per-account 形式（platform:selfId:userId），"
    "导入时会改写为 character:{platform}:{selfId} 并保留原始 id 于备注。"
)


def rewrite_story_id(story_id: str) -> str:
    parts = str(story_id or "").split(":")
    if len(parts) == 3 and parts[0] != "character":
        return f"character:{parts[0]}:{parts[1]}"
    return str(story_id)


def convert_koishi_row(table: str, record: dict[str, Any]) -> Optional[dict[str, Any]]:
    translation = COLUMN_TRANSLATION.get(table, {})
    out: dict[str, Any] = {}
    for old_column, value in record.items():
        new_column = translation.get(old_column)
        if not new_column:
            continue
        if table in JSON_COLUMNS and new_column in JSON_COLUMNS[table]:
            if isinstance(value, str):
                try:
                    value = json.loads(value) if value.strip() else ({}
                    if new_column in ("setting", "state", "metadata", "payload")
                    else [])
                except ValueError:
                    value = {} if new_column != "embedding" else []
        if table in BOOL_COLUMNS and new_column in BOOL_COLUMNS[table]:
            value = 1 if value in (True, 1, "1", b"1") else 0
        out[new_column] = value
    if table == "interlude_story":
        old_id = str(record.get("id") or "")
        out["id"] = rewrite_story_id(old_id)
        if out["id"] != old_id:
            state = out.get("state") if isinstance(out.get("state"), dict) else {}
            state["migratedFromStoryId"] = old_id
            out["state"] = state
    if table == "interlude_participant":
        session_key = str(record.get("userId") or "")
        out["session_key"] = session_key
        out.setdefault("umo", "")
        message_type = "GroupMessage" if "group" in str(record.get("channelId", "")).lower() \
            else "FriendMessage"
        out.setdefault("message_type", message_type)
    if table == "interlude_fact" and isinstance(out.get("embedding"), list) and not out["embedding"]:
        out["embedding"] = "[]"
        del out["embedding"]
    return out or None
