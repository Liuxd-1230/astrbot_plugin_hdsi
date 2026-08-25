"""Plugin configuration.

The full HDSI configuration lives in a plugin-owned JSON file
(data/plugin_data/astrbot_plugin_hdsi/config.json) editable from the WebUI
management page. _conf_schema.json only carries the four model-slot bindings.
Defaults and bounds mirror HDS-Interlude 0.1.3-beta1 Console schemas.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("hdsi.config")


class RestWindow(BaseModel):
    enabled: bool = True
    label: str = "night sleep"
    start: str = "23:00"
    end: str = "07:00"
    min_interval_minutes: int = Field(default=120, ge=30, le=1440)
    max_interval_minutes: int = Field(default=240, ge=30, le=1440)


class ModelSlots(BaseModel):
    """Model slot bindings; each is 'inherit' or 'provider_id' or 'provider_id:model'."""

    main_model: str = "inherit"
    compaction_model: str = "inherit"
    alter_model: str = "inherit"
    embedding_model: str = ""

    main_temperature: float = Field(default=0.8, ge=0, le=2)
    main_top_p: float = Field(default=1, ge=0, le=1)
    main_max_tokens: int = Field(default=0, ge=0, le=100_000)
    main_timeout_ms: int = Field(default=0, ge=0, le=300_000)

    failover_enabled: bool = True
    failover_max_attempts_per_provider: int = Field(default=1, ge=1, le=5)
    failover_cooldown_minutes: int = Field(default=5, ge=0, le=1440)

    vision_enabled: bool = False

    embedding_live_query: bool = False
    embedding_dimensions: int = Field(default=0, ge=0, le=32_768)
    embedding_timeout_ms: int = Field(default=10_000, ge=500, le=120_000)
    embedding_max_input_characters: int = Field(default=4_000, ge=100, le=32_000)
    embedding_backfill_batch_size: int = Field(default=5, ge=0, le=100)


class Prompts(BaseModel):
    main_prompt: str = (
        "Continue the character-centered life script with grounded actions, "
        "motives, relationships, and ordinary time passing."
    )
    format_prompt: str = ""
    fixed_prompt: str = ""
    style_prompt: str = (
        "Use restrained, realistic prose with concrete daily details, natural "
        "pauses, and no forced drama."
    )
    compaction_prompt: str = (
        "Compress completed scenes into concise continuity notes while "
        "preserving causality, promises, unresolved matters, and gradual "
        "character change."
    )
    compaction_fixed_prompt: str = ""
    compaction_style_prompt: str = "Concise, factual, chronological, and concrete."


class RuntimeConfig(BaseModel):
    capture_direct_messages: bool = True
    auto_create: bool = False
    ignore_command_messages: bool = True
    allow_proactive_messages: bool = False
    proactive_willingness_threshold: float = Field(default=0.65, ge=0, le=1)
    sweep_interval_minutes: int = Field(default=5, ge=1, le=1440)
    minimum_advance_minutes: int = Field(default=30, ge=1, le=10_080)
    context_entry_limit: int = Field(default=30, ge=1, le=200)
    memory_limit: int = Field(default=20, ge=1, le=200)
    max_script_characters: int = Field(default=8_000, ge=500, le=12_000)
    max_message_characters: int = Field(default=2_000, ge=1, le=12_000)
    minimum_delayed_reply_seconds: int = Field(default=10, ge=0, le=86_400)
    maximum_delayed_reply_minutes: int = Field(default=1_440, ge=1, le=43_200)
    cancel_delayed_replies_on_user_message: bool = True
    narrative_retry_delay_seconds: int = Field(default=60, ge=5, le=3_600)
    narrative_retry_max_attempts: int = Field(default=6, ge=0, le=50)
    split_reply_messages: bool = True
    message_separator: str = "<sep/>"
    typing_base_delay_seconds: float = Field(default=1, ge=0, le=60)
    typing_characters_per_second: float = Field(default=8, ge=1, le=100)
    typing_max_delay_seconds: float = Field(default=12, ge=0, le=120)
    user_message_debounce_seconds: float = Field(default=2, ge=0, le=15)
    auto_advance_enabled: bool = True
    auto_advance_interval_minutes: int = Field(default=40, ge=5, le=1_440)
    auto_advance_jitter_minutes: int = Field(default=5, ge=0, le=60)
    conversation_follow_up_minutes: list[int] = Field(default_factory=lambda: [10, 20])
    conversation_follow_up_jitter_minutes: int = Field(default=1, ge=0, le=10)
    rest_windows: list[RestWindow] = Field(
        default_factory=lambda: [RestWindow()],
    )


class MemoryConfig(BaseModel):
    enabled: bool = True
    background_interval_minutes: int = Field(default=10, ge=1, le=1_440)
    scene_entry_threshold: int = Field(default=12, ge=1, le=500)
    scene_character_threshold: int = Field(default=8_000, ge=500, le=200_000)
    recent_entry_limit: int = Field(default=30, ge=1, le=200)
    fact_limit: int = Field(default=20, ge=1, le=200)
    state_patch_confidence_threshold: float = Field(default=0.82, ge=0, le=1)
    major_state_patch_confidence_threshold: float = Field(default=0.95, ge=0, le=1)
    state_patch_min_turns: int = Field(default=3, ge=3, le=20)
    state_patch_min_days: int = Field(default=2, ge=1, le=30)
    state_patch_cooldown_hours: int = Field(default=72, ge=1, le=720)
    auto_apply_state_patches: bool = True
    allow_major_state_changes: bool = True
    max_facts_per_story: int = Field(default=200, ge=10, le=2_000)
    compaction_entry_limit: int = Field(default=80, ge=1, le=500)
    compaction_character_limit: int = Field(default=32_000, ge=500, le=200_000)
    scene_hook_characters: int = Field(default=2_000, ge=100, le=10_000)
    scene_summary_characters: int = Field(default=8_000, ge=500, le=50_000)
    arc_summary_characters: int = Field(default=12_000, ge=500, le=100_000)
    fact_content_characters: int = Field(default=4_000, ge=100, le=20_000)
    fact_importance_weight: float = Field(default=0.5, ge=0, le=1)
    fact_confidence_weight: float = Field(default=0.35, ge=0, le=1)
    fact_recency_weight: float = Field(default=0.15, ge=0, le=1)
    semantic_weight: float = Field(default=0.55, ge=0, le=2)
    unresolved_weight: float = Field(default=0.2, ge=0, le=2)
    active_consequences_enabled: bool = True
    active_consequence_prompt_limit: int = Field(default=6, ge=1, le=20)
    active_consequence_max_days: int = Field(default=7, ge=1, le=30)
    active_consequence_default_strength: float = Field(default=0.55, ge=0, le=1)
    overlay_compression_enabled: bool = True
    overlay_recent_days: int = Field(default=2, ge=1, le=14)
    overlay_monthly_after_days: int = Field(default=10, ge=5, le=180)
    overlay_weekly_window_days: int = Field(default=5, ge=1, le=14)
    overlay_monthly_window_days: int = Field(default=10, ge=5, le=30)
    overlay_weekly_summary_characters: int = Field(default=1_600, ge=300, le=8_000)
    overlay_monthly_summary_characters: int = Field(default=2_400, ge=300, le=12_000)


class AlterSystemConfig(BaseModel):
    enabled: bool = True
    base_threshold: float = Field(default=10, ge=1, le=50)
    density_factor: float = Field(default=0.3, ge=0, le=1)
    same_direction_boost: float = Field(default=0.05, ge=0, le=1)
    opposite_decay: float = Field(default=0.15, ge=0, le=1)
    min_weight: float = Field(default=0.2, ge=0, le=1)
    max_intensity: float = Field(default=2, ge=1, le=3)
    model_slot: str = ""
    temperature: float = Field(default=0.3, ge=0, le=2)
    top_p: float = Field(default=1, ge=0, le=1)
    max_tokens: int = Field(default=400, ge=64, le=2_000)
    timeout: int = Field(default=30_000, ge=1_000, le=120_000)
    prompt: str = ""


class AgencyConfigModel(BaseModel):
    enabled: bool = True
    max_window_minutes: int = Field(default=240, ge=5, le=1_440)
    minimum_proactive_interval_minutes: int = Field(default=60, ge=0, le=10_080)
    max_candidate_hours: int = Field(default=24, ge=1, le=168)


class BrowserConfig(BaseModel):
    enabled: bool = False
    mode: str = "deferred-only"  # deferred-only | allow-immediate
    allow_search: bool = True
    allow_visit: bool = True
    search_url_template: str = "https://html.duckduckgo.com/html/?q={query}"
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_concurrent_pages: int = Field(default=1, ge=1, le=4)
    max_research_per_sweep: int = Field(default=1, ge=1, le=20)
    navigation_timeout: int = Field(default=15_000, ge=1_000, le=120_000)
    max_text_characters: int = Field(default=12_000, ge=500, le=50_000)
    max_excerpt_characters: int = Field(default=3_000, ge=200, le=12_000)
    max_observations_in_prompt: int = Field(default=4, ge=1, le=20)
    cache_minutes: int = Field(default=30, ge=0, le=10_080)
    allow_group_triggered_research: bool = False


class AccessRule(BaseModel):
    """One allowlist row for bot accounts / users / groups."""

    id: str = ""
    label: str = ""
    person_id: str = ""
    profile: str = ""
    relationship: str = ""
    purpose: str = ""
    character_role: str = ""
    response_mode: str = "mention-only"  # mention-only | always
    context_limit: int = Field(default=20, ge=4, le=100)
    debounce_seconds: float = Field(default=1, ge=0, le=10)
    cooldown_seconds: int = Field(default=60, ge=0, le=86_400)
    enabled: bool = True


class PlatformGateConfig(BaseModel):
    """Generalized replacement of the OneBot/NapCat gate.

    Empty user list keeps private chat closed until an administrator enrolls
    participants (matching the original explicit-allowlist behavior).
    """

    bot_accounts: list[AccessRule] = Field(default_factory=list)
    user_accounts: list[AccessRule] = Field(default_factory=list)
    group_chats: list[AccessRule] = Field(default_factory=list)
    ignore_self_messages: bool = True


class SharedStoryConfig(BaseModel):
    auto_enroll_participants: bool = True
    allow_cross_conversation_messages: bool = True
    share_participant_details: bool = False
    max_cross_conversation_actions: int = Field(default=1, ge=0, le=5)
    participant_context_limit: int = Field(default=6, ge=1, le=20)
    manager_ids: list[str] = Field(default_factory=list)


class StoryDefaults(BaseModel):
    character_name: str = "Unnamed character"
    character_profile: str = ""
    user_profile: str = ""
    relationship: str = ""
    world: str = ""
    supporting_cast: str = ""
    location: str = ""
    style: str = "现实主义日常叙事，情绪克制，关系变化缓慢而具体。"
    timezone: str = "Asia/Shanghai"


class LoggingConfig(BaseModel):
    level: str = "info"  # silent | error | warn | info | debug
    verbosity: str = "standard"  # summary | standard | diagnostic
    format: str = "layered"  # layered | compact | detailed
    colors: bool = True
    color_theme: str = "dark"
    kaomoji: bool = True
    log_script_preview: bool = False
    log_message_content: bool = False
    preview_length: int = Field(default=500, ge=50, le=4_000)


class HdsiConfig(BaseModel):
    enable: bool = True
    models: ModelSlots = Field(default_factory=ModelSlots)
    prompts: Prompts = Field(default_factory=Prompts)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    alter_system: AlterSystemConfig = Field(default_factory=AlterSystemConfig)
    agency: AgencyConfigModel = Field(default_factory=AgencyConfigModel)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    platform_gate: PlatformGateConfig = Field(default_factory=PlatformGateConfig)
    shared_story: SharedStoryConfig = Field(default_factory=SharedStoryConfig)
    story_defaults: StoryDefaults = Field(default_factory=StoryDefaults)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


DEFAULT_CONFIG_DICT = HdsiConfig().model_dump()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config_file(path: Path) -> HdsiConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HdsiConfig()
    except ValueError as error:
        logger.warning("HDSI 配置文件损坏，使用默认配置：%s", error)
        return HdsiConfig()
    merged = deep_merge(DEFAULT_CONFIG_DICT, raw if isinstance(raw, dict) else {})
    try:
        return HdsiConfig.model_validate(merged)
    except Exception as error:
        logger.warning("HDSI 配置校验失败，忽略非法字段：%s", error)
        # Validate field-by-field to keep whatever is valid.
        data = dict(merged)
        for section in list(data.keys()):
            single = {section: data[section]}
            try:
                HdsiConfig.model_validate({**{k: v for k, v in DEFAULT_CONFIG_DICT.items() if k == section}, **single})
            except Exception:
                data.pop(section, None)
                logger.warning("HDSI 配置节 %s 已回退默认值", section)
        try:
            return HdsiConfig.model_validate(deep_merge(DEFAULT_CONFIG_DICT, data))
        except Exception:
            return HdsiConfig()


def save_config_file(path: Path, config: HdsiConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
