"""HDS Interlude for AstrBot — plugin entry & platform adapter.

The narrative runtime lives in the ``hdsi`` package; this file bridges it to
AstrBot: event intake, provider-backed model calls, delivery, admin commands,
WebUI endpoints and lifecycle/reload recovery.

收到被 HDSI 管理的消息后，本插件会调用 ``event.should_call_llm(False)`` 禁止
AstrBot 默认 LLM 回复链，然后由 HDSI 自己完成：写入真实 ScriptEntry → debounce
→ Story 串行队列 → 一次主叙事模型调用 → 决定 seen/silent/immediate/delayed →
保存状态 → 最后才进行真实消息投递。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageEventResult, filter
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from .hdsi import agency as agency_mod
from .hdsi.config import (
    AccessRule,
    HdsiConfig,
    load_config_file,
    save_config_file,
)
from .hdsi.database.connection import Database
from .hdsi.json_repair import extract_chat_text, extract_json_object
from .hdsi.narrator import ProviderRouter, RouterOptions, SlotBinding, parse_slot
from .hdsi.service import (
    IncomingEvent,
    InterludeService,
    account_enabled,
    normalize_account_id,
    normalize_group_id,
)
from .hdsi.time import format_log_time
from .hdsi.types import (
    CharacterRecord,
    CharacterStatus,
    ConversationBinding,
    InterludeParticipant,
    InterludeStory,
    NarrativeIntent,
    iso,
    parse_date,
)

PLUGIN_NAME = "astrbot_plugin_hdsi"

_current_umo: contextvars.ContextVar[str] = contextvars.ContextVar("hdsi_current_umo", default="")

COMMAND_PREFIXES = ("hdsi",)


# ------------------------------------------------------------------ narrator


class AstrBotNarrator:
    """Model stack backed by AstrBot Providers with failover + cooldowns."""

    def __init__(self, context: Context, slots, router_options: RouterOptions) -> None:
        self.context = context
        self.slots = slots  # hdsi.config.ModelSlots
        self.main_router = ProviderRouter(router_options)
        self.compaction_router = ProviderRouter(router_options)
        self.alter_router = ProviderRouter(router_options)

    async def _resolve_provider(self, binding: SlotBinding) -> Any:
        if binding.kind == "inherit":
            umo = _current_umo.get()
            try:
                provider = await self.context.get_using_provider_async(umo=umo or None)
            except TypeError:
                provider = await self.context.get_using_provider_async()
            except Exception:  # noqa: BLE001
                provider = None
            if provider is not None:
                return provider
            # Session-scoped lookup can be empty early after startup or for
            # unrouted sessions; fall back to the global default, then to any
            # enabled chat provider.
            try:
                provider = await self.context.get_using_provider_async()
                if provider is not None:
                    return provider
            except Exception:  # noqa: BLE001
                pass
            try:
                sync_get = getattr(self.context, "get_using_provider", None)
                if sync_get is not None:
                    provider = sync_get()
                    if provider is not None:
                        return provider
            except Exception:  # noqa: BLE001
                pass
            candidates = self.context.get_all_providers() or []
            return candidates[0] if candidates else None
        return self.context.get_provider_by_id(binding.provider_id)

    async def _chat(
        self,
        router: ProviderRouter,
        slot_value: str,
        *,
        system_prompt: str,
        user_content: str,
        image_urls: Optional[list[str]] = None,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float,
        response_json: bool,
    ) -> str:
        bindings = parse_slot(slot_value)
        candidates = router.select(bindings)
        failures: list[str] = []
        last_error: Exception | None = None
        for binding in candidates:
            attempts = max(1, router.options.max_attempts_per_provider)
            for attempt in range(1, attempts + 1):
                try:
                    provider = await self._resolve_provider(binding)
                    if provider is None:
                        raise RuntimeError(f"provider not found or not enabled: {binding.key}")
                    model_override = binding.model or None

                    async def _call() -> Any:
                        return await provider.text_chat(
                            prompt=user_content,
                            session_id=None,
                            image_urls=image_urls or None,
                            contexts=None,
                            system_prompt=system_prompt,
                            model=model_override,
                            **({"temperature": temperature} if temperature is not None else {}),
                            # Forwarded via **kwargs; OpenAI-compatible sources
                            # merge them into the request payload.
                            top_p=top_p,
                            max_tokens=max_tokens or None,
                        )

                    # Honour an explicitly configured slot timeout; 0 means
                    # defer entirely to the provider's own retry policy
                    # (matching pre-adapter behaviour).
                    if timeout_seconds and timeout_seconds > 0:
                        response = await asyncio.wait_for(_call(), timeout=timeout_seconds)
                    else:
                        response = await _call()
                    text = extract_chat_text(_llm_response_text(response))
                    if not text.strip():
                        raise RuntimeError("provider returned an empty response")
                    router.mark_success(binding)
                    return text
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    detail = str(error)
                    failures.append(f"{binding.key}(attempt {attempt}): {detail}")
                    logger.debug("[hdsi] 叙事模型服务商失败：%s；尝试=%s", binding.key, detail)
            router.mark_failure(binding)
            if not router.options.failover_enabled:
                break
        raise RuntimeError(
            "All narrative providers failed. " + " | ".join(failures[-6:])
        ) from last_error

    async def decide_raw(self, request, *, system_prompt: str, temperature: float,
                         top_p: float, max_tokens: int, timeout_seconds: float,
                         response_json: bool, max_repairs: int = 1):
        from .hdsi.prompt_builder import build_prompt_payload

        from .hdsi.json_repair import REPAIR_INSTRUCTION

        payload = json.dumps(build_prompt_payload(request), ensure_ascii=False)
        image_urls = None
        if request.images and request.phase.value == "user-message":
            image_urls = [image.data_uri for image in request.images]
        last_error: Exception | None = None
        for attempt in range(1 + max(0, max_repairs)):
            user_content = payload if attempt == 0 else payload + REPAIR_INSTRUCTION
            text = await self._chat(
                self.main_router, self.slots.main_model,
                system_prompt=system_prompt, user_content=user_content,
                image_urls=image_urls,
                temperature=temperature, top_p=top_p,
                max_tokens=max_tokens, timeout_seconds=timeout_seconds,
                response_json=response_json,
            )
            try:
                raw = extract_json_object(text, "Narrative provider")
                return raw, []
            except ValueError as error:
                last_error = error
                logger.warning("[hdsi] 主叙事返回非法 JSON（第 %d 次），尝试修复重试",
                               attempt + 1)
        raise last_error if last_error else RuntimeError("empty narrative response")

    async def compact_raw(self, *, payload: dict[str, Any], system_prompt: str,
                          temperature: float, top_p: float, max_tokens: int,
                          timeout_seconds: float, response_json: bool):
        text = await self._chat(
            self.compaction_router, self.slots.compaction_model,
            system_prompt=system_prompt,
            user_content=json.dumps(payload, ensure_ascii=False),
            temperature=temperature, top_p=top_p,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            response_json=response_json,
        )
        return extract_json_object(text, "Compaction provider")

    async def analyze_alter(self, request_payload: dict[str, Any], *,
                            system_prompt: str, temperature: float, top_p: float,
                            max_tokens: int, timeout_seconds: float,
                            response_json: bool):
        text = await self._chat(
            self.alter_router, self.slots.alter_model,
            system_prompt=system_prompt,
            user_content=json.dumps(request_payload, ensure_ascii=False),
            temperature=temperature, top_p=top_p,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            response_json=response_json,
        )
        return extract_json_object(text, "Alter analysis provider")


def _llm_response_text(response: Any) -> str:
    """Extract plain text from an LLMResponse-like object."""
    if response is None:
        return ""
    text = getattr(response, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chain = getattr(response, "result_chain", None)
    if chain is not None:
        try:
            plain = chain.get_plain_text()
            if isinstance(plain, str):
                return plain
        except Exception:  # noqa: BLE001
            pass
    raw = getattr(response, "raw_completion", None)
    if isinstance(raw, dict):
        choices = raw.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


class AstrBotEmbedder:
    def __init__(self, context: Context, provider_id: str) -> None:
        self.context = context
        self.provider_id = provider_id.strip()

    async def embed(self, input_text: str) -> list[float]:
        if not self.provider_id:
            return []
        provider = self.context.get_provider_by_id(self.provider_id)
        if provider is None:
            return []
        get_embedding = getattr(provider, "get_embedding", None)
        if get_embedding is None:
            return []
        return list(await get_embedding(input_text))


# ------------------------------------------------------------------ plugin


class HdsiInterludePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config = config
        self.service: InterludeService | None = None
        self.db: Database | None = None
        self.narrator: AstrBotNarrator | None = None
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.config_path = self.data_dir / "config.json"
        self.hdsi_config: HdsiConfig = HdsiConfig()
        self.pending_confirmations: dict[str, tuple[str, datetime]] = {}

    # ------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        self.hdsi_config = load_config_file(self.config_path)
        if self.raw_config is not None:
            self._apply_schema_overrides()
        self.db = Database(self.data_dir / "interlude.db")
        await self.db.connect()
        router_options = RouterOptions(
            failover_enabled=self.hdsi_config.models.failover_enabled,
            max_attempts_per_provider=self.hdsi_config.models.failover_max_attempts_per_provider,
            cooldown_minutes=self.hdsi_config.models.failover_cooldown_minutes,
        )
        self.narrator = AstrBotNarrator(self.context, self.hdsi_config.models, router_options)
        embedder = AstrBotEmbedder(self.context, self.hdsi_config.models.embedding_model)
        self.service = InterludeService(
            db=self.db,
            config=self.hdsi_config,
            narrator=self.narrator,
            embedder=embedder,
            sender=self._send_to_participant,
            group_sender=self._send_to_group,
            now_fn=lambda: datetime.now(timezone.utc),
            browser_fetch=self._browser_fetch,
            image_loader=self._image_loader,
        )
        # Routes are registered under `{PLUGIN_NAME}/hdsi/*`. The dashboard
        # bridge builds /api/v1/plugins/extensions/{PLUGIN}/{endpoint} which
        # full-matches this registration.
        api = f"/{PLUGIN_NAME}/hdsi"
        self.context.register_web_api(f"{api}/overview", self._api_overview, ["GET"], "HDSI 总览")
        self.context.register_web_api(f"{api}/config", self._api_get_config, ["GET"], "HDSI 配置读取")
        self.context.register_web_api(f"{api}/config", self._api_set_config, ["POST"], "HDSI 配置保存")
        self.context.register_web_api(f"{api}/participants", self._api_participants, ["GET"], "HDSI 参与者")
        self.context.register_web_api(f"{api}/script", self._api_script, ["GET"], "HDSI 剧本查看")
        self.context.register_web_api(f"{api}/intents", self._api_intents, ["GET"], "HDSI 意图列表")
        self.context.register_web_api(f"{api}/maintenance", self._api_maintenance, ["POST"], "HDSI 维护操作")
        self.context.register_web_api(f"{api}/migrate_config", self._api_migrate_config, ["POST"], "HDSI Koishi 配置导入")

        # Multi-character & binding & facts routes
        self.context.register_web_api(f"{api}/characters", self._api_characters_list, ["GET"], "HDSI 角色列表")
        self.context.register_web_api(f"{api}/characters/create", self._api_characters_create, ["POST"], "HDSI 创建角色")
        self.context.register_web_api(f"{api}/characters/detail", self._api_characters_detail, ["GET"], "HDSI 角色详情")
        self.context.register_web_api(f"{api}/characters/canon", self._api_characters_canon_get, ["GET"], "HDSI 角色 Canon 设定")
        self.context.register_web_api(f"{api}/characters/canon", self._api_characters_canon_set, ["POST"], "HDSI 保存角色 Canon 设定")
        self.context.register_web_api(f"{api}/characters/update", self._api_characters_update, ["POST"], "HDSI 更新角色")
        self.context.register_web_api(f"{api}/characters/clone", self._api_characters_clone, ["POST"], "HDSI 复制角色")
        self.context.register_web_api(f"{api}/characters/delete", self._api_characters_delete, ["POST"], "HDSI 删除角色")
        self.context.register_web_api(f"{api}/characters/set_default", self._api_characters_set_default, ["POST"], "HDSI 设置默认角色")
        self.context.register_web_api(f"{api}/characters/export", self._api_characters_export, ["GET"], "HDSI 导出角色")
        self.context.register_web_api(f"{api}/characters/import", self._api_characters_import, ["POST"], "HDSI 导入角色")
        self.context.register_web_api(f"{api}/bindings", self._api_bindings_list, ["GET"], "HDSI 会话绑定列表")
        self.context.register_web_api(f"{api}/bindings/save", self._api_bindings_save, ["POST"], "HDSI 保存会话绑定")
        self.context.register_web_api(f"{api}/bindings/delete", self._api_bindings_delete, ["POST"], "HDSI 删除会话绑定")
        self.context.register_web_api(f"{api}/memory", self._api_memory_list, ["GET"], "HDSI 长期记忆列表")
        self.context.register_web_api(f"{api}/memory/create", self._api_memory_create, ["POST"], "HDSI 创建长期记忆")
        self.context.register_web_api(f"{api}/memory/delete", self._api_memory_delete, ["POST"], "HDSI 删除长期记忆")
        self.context.register_web_api(f"{api}/facts", self._api_facts_list, ["GET"], "HDSI 事实列表")
        self.context.register_web_api(f"{api}/facts/create", self._api_facts_create, ["POST"], "HDSI 创建事实")
        self.context.register_web_api(f"{api}/facts/delete", self._api_facts_delete, ["POST"], "HDSI 删除事实")
        self.context.register_web_api(f"{api}/participants/update", self._api_participants_update, ["POST"], "HDSI 更新参与者")
        self.context.register_web_api(f"{api}/participants/clear_unread", self._api_participants_clear_unread, ["POST"], "HDSI 清空参与者未读")
        self.context.register_web_api(f"{api}/participants/reset", self._api_participants_reset, ["POST"], "HDSI 重置参与者状态")
        self.context.register_web_api(f"{api}/participants/delete", self._api_participants_delete, ["POST"], "HDSI 删除参与者")
        self.context.register_web_api(f"{api}/backup", self._api_backup, ["GET"], "HDSI 完整备份")
        self.context.register_web_api(f"{api}/restore", self._api_restore, ["POST"], "HDSI 完整恢复")

        # Round-3 P0-2: crash-left `sending` rows become `uncertain`
        # (no resend, no fabricated spoken fact) before anything else.
        uncertain = await self.service.recover_stale_sending()
        if uncertain:
            logger.warning("[hdsi] %d 条投递因崩溃标记为 uncertain", uncertain)
        await self.service.ensure_character_registry()
        await self._recover_pending_tasks()
        if self.hdsi_config.enable:
            self.service.start_background_tasks()
        logger.info("[hdsi] HDS Interlude 已加载 v1.1.0（持续叙事运行时）")

    async def terminate(self) -> None:
        if self.service is not None:
            await self.service.stop_background_tasks()
            self.service.invalidate_buffered_narratives()
        if self.db is not None:
            await self.db.close()
        logger.info("[hdsi] HDS Interlude 已卸载")

    def _apply_schema_overrides(self) -> None:
        """Merge the four _conf_schema.json bindings into the full config."""
        changed = False
        mapping = {
            "main_model": ("models", "main_model"),
            "compaction_model": ("models", "compaction_model"),
            "alter_model": ("models", "alter_model"),
            "embedding_model": ("models", "embedding_model"),
            "enable": (None, "enable"),
        }
        for key, value in dict(self.raw_config).items():
            if key not in mapping:
                continue
            section, field_name = mapping[key]
            target = self.hdsi_config.models if section == "models" else self.hdsi_config
            current_value = getattr(target, field_name)
            if current_value != value:
                setattr(target, field_name, value)
                changed = True
                try:
                    setattr(self.raw_config, key, value)
                except Exception:  # noqa: BLE001
                    self.raw_config[key] = value
        if changed:
            save_config_file(self.config_path, self.hdsi_config)

    async def _recover_pending_tasks(self) -> None:
        """Restart-safety: re-arm wakes for persisted pending intents of
        EVERY active story (multi-bot safe, P0-4)."""
        assert self.service is not None
        stories = await self.service.active_stories()
        now = datetime.now(timezone.utc)
        wake_types = {"split-message", "outbound-message",
                      "outbound-group-message", "delayed-reply",
                      "proactive-check", "narrative-retry",
                      "cross-conversation-message"}
        total = 0
        for story in stories:
            rows = await self.db.get(
                "interlude_intent", {"story_id": story.id, "status": "pending"},
                order_by="not_before",
            )
            count = 0
            for row in rows:
                intent = NarrativeIntent.model_validate(row)
                if intent.type not in wake_types:
                    continue
                when = intent.not_before if intent.not_before > now else now + timedelta(seconds=2)
                self.service.schedule_due_intent_wake(story.id, when)
                count += 1
            next_advance = parse_date(story.state.automation.next_advance_at)
            if next_advance is not None:
                self.service.schedule_due_intent_wake(story.id, max(next_advance, now))
            total += count
        if total:
            logger.info("[hdsi] 重启恢复：%d 个待处理任务已重新调度（%d 个故事）",
                        total, len(stories))

    # ------------------------------------------------------------ event intake

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        if not self.hdsi_config.enable or self.service is None:
            yield None
            return
        incoming = self.normalize_event(event)
        if incoming is None:
            yield None
            return
        # Management confirmation flow takes precedence.
        if self._consume_confirmation(incoming):
            event.should_call_llm(False)
            event.stop_event()
            yield None
            return
        if self._looks_like_command(incoming.content):
            yield None
            return

        token = _current_umo.set(event.unified_msg_origin)
        try:
            if incoming.message_type == "GroupMessage":
                handled = await self._receive_group(incoming)
            else:
                handled = await self.service.receive(incoming)
        finally:
            _current_umo.reset(token)
        if handled:
            event.should_call_llm(False)
            event.stop_event()
        yield None

    def normalize_event(self, event: AstrMessageEvent) -> Optional[IncomingEvent]:
        try:
            message_type = event.get_message_type().value  # FriendMessage / GroupMessage / OtherMessage
            if message_type == "OtherMessage":
                return None
            components = event.get_messages()
            image_sources: list[str] = []
            texts: list[str] = []
            is_mention = False
            self_id = event.get_self_id()
            for component in components or []:
                comp_type = getattr(component, "type", None)
                type_name = getattr(comp_type, "value", "") if comp_type is not None else ""
                if type_name == "image" or type_name == "Image":
                    source = getattr(component, "url", "") or getattr(component, "file", "") or ""
                    if source:
                        image_sources.append(str(source))
                elif type_name == "at" or type_name == "At":
                    qq = str(getattr(component, "qq", ""))
                    if qq == str(self_id):
                        is_mention = True
                elif type_name == "plain" or type_name == "Plain":
                    texts.append(str(getattr(component, "text", "")))
            content = event.message_str or "".join(texts).strip()
            group_id = event.get_group_id() if message_type == "GroupMessage" else ""
            return IncomingEvent(
                platform_id=event.get_platform_id(),
                self_id=str(self_id),
                sender_id=event.get_sender_id() or "",
                sender_name=event.get_sender_name() or event.get_sender_id() or "",
                umo=event.unified_msg_origin,
                message_type=message_type,
                group_id=group_id,
                content=content,
                image_sources=image_sources[:3],
                is_mention=is_mention,
                is_admin=bool(event.is_admin()),
                message_id=event.message_obj.message_id if event.message_obj else "",
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 事件规范化失败：%s", error)
            return None

    async def _receive_group(self, incoming: IncomingEvent) -> bool:
        assert self.service is not None
        rule = self.service.group_rule(incoming.group_id)
        if rule is None:
            return False
        gate = self.service.config.platform_gate
        if not account_enabled(gate.bot_accounts, incoming.self_id):
            return False
        if rule.response_mode == "mention-only" and not incoming.is_mention:
            return False
        story = await self.service.find_story_for_event(incoming)
        if story is None and self.service.config.runtime.auto_create:
            story = await self.service.create_story(incoming)
        if story is None or story.status != "active":
            return False
        now = datetime.now(timezone.utc)
        sender_id = normalize_account_id(incoming.sender_id)

        async def record() -> None:
            from .hdsi.types import ScriptEntryDraft

            await self.service.append_entry(story.id, ScriptEntryDraft(
                kind="group-message", actor="user", content=incoming.content,
                occurred_at=iso(now),
                metadata={
                    "groupId": incoming.group_id, "senderId": sender_id,
                    "senderName": incoming.sender_name,
                },
            ), now)
            await self.service.pause_automatic_advance_after_user_message(story.id, now)

        await self.service.queues.run(story.id, record)
        key = f"{story.id}:{normalize_group_id(incoming.group_id)}"
        turn = self.service.buffered_group_turns.get(key)
        if turn is None:
            from .hdsi.service import BufferedGroupTurn

            turn = BufferedGroupTurn(
                story_id=story.id, group_id=normalize_group_id(incoming.group_id),
                channel_id=incoming.group_id, rule=rule,
            )
        from .hdsi.types import GroupMessageContext

        turn.messages.append(GroupMessageContext(
            sender_id=sender_id, sender_name=incoming.sender_name,
            content=incoming.content, occurred_at=now, direction="user",
        ))
        turn.latest_event = incoming
        if turn.timer_task is not None:
            turn.timer_task.cancel()

        async def fire() -> None:
            await asyncio.sleep(max(0.0, float(rule.debounce_seconds)))
            await self.flush_group_turn(key)

        turn.timer_task = asyncio.get_running_loop().create_task(fire())
        self.service.buffered_group_turns[key] = turn
        self.service.report_operation("summary", "info", story, "user-message",
                                      "收到群聊消息 群=%s 发送者=%s",
                                      incoming.group_id, sender_id)
        return True

    async def flush_group_turn(self, key: str) -> None:
        assert self.service is not None
        service = self.service
        turn = service.buffered_group_turns.get(key)
        if turn is None:
            return
        service.buffered_group_turns.pop(key, None)
        batch = list(turn.messages)
        if not batch:
            return
        story_rows = await self.db.get("interlude_story", {"id": turn.story_id})
        if not story_rows:
            return
        story = InterludeStory.model_validate(story_rows[0])
        service.narrating_stories.add(story.id)
        try:
            from .hdsi.types import GroupContext

            snapshot = await service.queues.run(story.id, lambda: self._snapshot_group_turn(turn))

            async def call() -> dict[str, Any]:
                outcome = await service.try_decide(
                    snapshot["story"], None, "user-message",
                    snapshot["from"], snapshot["now"],
                    user_message=snapshot["user_message"],
                    group_context=snapshot["group_context"],
                )
                return outcome

            outcome = await call()
            result = await service.queues.run(story.id, lambda: self._persist_group_turn(
                turn, snapshot, outcome,
            ))
            # Round-4: each staged intent is delivered individually through
            # _deliver_group_outbound (two-phase: transport → finalize).
            staged_ids = result.get("staged_ids", [])
            if staged_ids:
                # Deliver only the FIRST segment immediately; subsequent ones
                # go through the wake/sweep mechanism like private splits.
                rows = await self.db.get("interlude_intent", {"id": staged_ids[0]})
                if rows:
                    from .hdsi.types import NarrativeIntent

                    intent = NarrativeIntent.model_validate(rows[0])
                    await service.queues.run(story.id, lambda: service._deliver_group_outbound(
                        story, intent, datetime.now(timezone.utc),
                    ))
                # Subsequent segments are woken via schedule_due_intent_wake
                # which was set by stage_outbound_message timing.
                if len(staged_ids) > 1:
                    next_rows = await self.db.get(
                        "interlude_intent", {"id": staged_ids[1]})
                    if next_rows:
                        next_nb = parse_date(next_rows[0]["not_before"])
                        if next_nb:
                            service.schedule_due_intent_wake(story.id, max(next_nb, datetime.now(timezone.utc)))
            service.schedule_compaction(story.id)
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 群聊主叙事失败：群=%s 错误=%s", turn.group_id, error)
        finally:
            service.narrating_stories.discard(turn.story_id)

    async def _snapshot_group_turn(self, turn):
        assert self.service is not None
        story = await self.service.get_story(turn.story_id)
        now = datetime.now(timezone.utc)
        from .hdsi.service import narrative_cursor

        limit = getattr(turn.rule, "context_limit", 20)
        rows = await self.db.get(
            "interlude_script_entry", {"story_id": story.id},
            limit=max(20, min(200, limit * 8)), order_by="occurred_at", descending=True,
        )
        entries = [
            e for e in rows
            if e.get("kind") in ("group-message", "character-group-message")
            and normalize_group_id(str((e.get("metadata") or {}).get("groupId", "")))
            == normalize_group_id(turn.group_id)
        ][:max(1, limit)]
        entries.reverse()
        messages = [
            {
                "senderId": str((e.get("metadata") or {}).get("senderId", "")),
                "senderName": str((e.get("metadata") or {}).get("senderName", "")),
                "content": e.get("content", ""),
                "occurredAt": e.get("occurred_at"),
                "direction": "character" if e.get("actor") == "character" else "user",
            }
            for e in entries
        ]
        from .hdsi.types import GroupContext, GroupMessageContext

        parsed_messages = [
            GroupMessageContext(
                sender_id=m["senderId"], sender_name=m["senderName"] or m["senderId"],
                content=m["content"], occurred_at=parse_date(m["occurredAt"]) or now,
                direction="character" if m["direction"] == "character" else "user",
            )
            for m in messages
        ]
        group_context = GroupContext(
            group_id=turn.group_id, channel_id=turn.channel_id,
            label=getattr(turn.rule, "label", ""),
            purpose=getattr(turn.rule, "purpose", ""),
            character_role=getattr(turn.rule, "character_role", ""),
            messages=parsed_messages,
        )
        user_message = "\n\n".join(
            f"[群聊连续消息 {index + 1}，发送者 {m.sender_id}]\n{m.content}"
            for index, m in enumerate(parsed_messages[-3:])
        )
        return {
            "story": story,
            "from": narrative_cursor(story, now),
            "now": now,
            "group_context": group_context,
            "user_message": user_message,
        }

    async def _persist_group_turn(self, turn, snapshot, outcome) -> dict[str, Any]:
        assert self.service is not None
        from .hdsi.service import normalize_group_reply_local

        if not outcome["succeeded"]:
            return {"content": ""}
        story = snapshot["story"]
        decision_raw = outcome["decision_raw"]
        messages = await self.service.persist_decision(
            story, None, decision_raw, snapshot["from"], outcome["effective_now"],
            permit_messages=False, phase="user-message",
        )
        content = normalize_group_reply_local(decision_raw, self.hdsi_config.runtime.max_message_characters)
        staged_ids: list[int] = []
        if content:
            # P0 (round-4): split at stage time so each intent maps to
            # EXACTLY ONE platform send — the DB outbox unit and the real
            # external side-effect are always 1:1.
            first, later = self.service.split_outgoing_message(content)
            segments = [first, *later] if first else []
            typing_started = outcome["effective_now"]
            delay_ms = 0.0
            for seg in segments:
                if seg:
                    if len(staged_ids) > 0:
                        delay_ms += self.service.typing_delay_milliseconds(seg)
                        send_at = typing_started + timedelta(milliseconds=delay_ms)
                    else:
                        send_at = typing_started
                    sid = await self.service.stage_outbound_message(
                        story.id, "", seg, send_at,
                        intent_type="outbound-group-message",
                        extra_payload={"groupId": turn.group_id,
                                       "channelId": turn.channel_id},
                    )
                    staged_ids.append(sid)
        await self.db.update("interlude_story", {"id": story.id}, {
            "cursor_at": iso(outcome["effective_now"]),
            "updated_at": iso(datetime.now(timezone.utc)),
        })
        await self.service.schedule_conversation_follow_ups_after_turn(
            story.id, outcome["effective_now"], None,
        )
        return {"staged_ids": staged_ids}

    async def _send_group(self, story: InterludeStory, channel_id: str, content: str) -> bool:
        assert self.service is not None
        session = self._session_for_story_group(story, channel_id)
        if session is None:
            logger.warning("[hdsi] 没有可用平台投递群消息 群=%s", channel_id)
            return False
        first, later = self.service.split_outgoing_message(content)
        parts = [first, *later]
        try:
            for part in parts:
                ok = await self.context.send_message(session, MessageChain().message(part))
                if not ok:
                    return False
            return True
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 群消息投递失败 群=%s 错误=%s", channel_id, error)
            return False

    def _session_for_story_group(self, story: InterludeStory, group_id: str):
        try:
            message_type = "GroupMessage"
            umo = f"{story.platform_id}:{message_type}:{group_id}"
            return MessageSession.from_str(umo)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ delivery

    async def _send_to_participant(
        self, story: InterludeStory, participant: InterludeParticipant, content: str
    ) -> bool:
        umo = participant.umo
        if not umo:
            umo = f"{participant.platform_id}:{participant.message_type}:{participant.session_key}"
        try:
            session = MessageSession.from_str(umo)
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 无法解析投递会话 %s：%s", umo, error)
            return False
        try:
            return bool(await self.context.send_message(session, MessageChain().message(content)))
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 消息投递失败 参与者=%s 错误=%s", participant.id, error)
            return False

    async def _send_to_group(self, story: InterludeStory, group_channel: str, content: str) -> bool:
        return await self._send_group(story, group_channel, content)

    async def _browser_fetch(self, url: str, timeout_ms: int = 15_000) -> tuple[str, str]:
        """Bounded read-only public page fetch returning (title, visible text).

        SSRF-safe (P1): redirects are followed MANUALLY; every hop's URL and
        all resolved IPs are re-validated before the next request.
        """
        timeout_seconds = max(1.0, timeout_ms / 1000.0)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; HDSI-AstrBot/1.0; +https://github.com/MomoiCore)",
        }
        blocked = self.hdsi_config.browser.blocked_domains
        allowed = self.hdsi_config.browser.allowed_domains
        current = url
        max_hops = 5
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=timeout_seconds, headers=headers,
        ) as client:
            for _hop in range(max_hops):
                if not agency_mod.is_safe_public_web_url(current, blocked, allowed):
                    raise RuntimeError(f"重定向目标未通过公开网页安全校验：{current}")
                await _assert_public_dns(current)
                response = await client.get(current, headers=headers)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        break
                    from urllib.parse import urljoin

                    current = urljoin(current, location.strip())
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type and "text" not in content_type and content_type:
                    raise RuntimeError(f"unsupported content-type: {content_type}")
                body = response.text[:2_000_000]
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                title = ""
                if title_match:
                    title = strip_html(title_match.group(1)).strip()
                return title, extract_visible_text(body)
        raise RuntimeError("重定向次数超出限制")

    async def _image_loader(self, source: str) -> tuple[str, str]:
        """Load one bounded picture source → (mime_type, data_uri).

        Separate adapter from web pages (P1): accepts http(s) URLs, local
        paths, base64:// and data: URIs; rejects anything over 4 MB or with a
        non-image MIME.
        """
        import base64 as _base64
        import pathlib

        value = (source or "").strip()
        raw: bytes | None = None
        mime = ""
        if value.startswith("data:image/"):
            match = re.match(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", value, re.I | re.S)
            if not match:
                raise RuntimeError("invalid data URI")
            decoded_size = len(match.group(2)) * 3 // 4
            if decoded_size > 4 * 1024 * 1024:
                raise RuntimeError("image exceeds 4MB bound")
            return match.group(1).lower(), value
        if value.startswith("base64://"):
            raw = _base64.b64decode(value[len("base64://"):])
        elif value.startswith("file://"):
            raw = pathlib.Path(value[len("file://"):]).read_bytes()
        elif re.match(r"^https?://", value, re.I):
            if not agency_mod.is_safe_public_web_url(
                value,
                self.hdsi_config.browser.blocked_domains,
                self.hdsi_config.browser.allowed_domains,
            ):
                # Vision sources come from the platform adapter, not the open
                # web; still enforce non-private destinations.
                raise RuntimeError("图片地址未通过安全校验")
            await _assert_public_dns(value)
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=max(1.0, self.hdsi_config.browser.navigation_timeout / 1000.0),
                headers={"User-Agent": "Mozilla/5.0 (compatible; HDSI-AstrBot/1.0)"},
            ) as client:
                response = await client.get(value)
                response.raise_for_status()
                raw = response.content
                mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        else:
            p = pathlib.Path(value)
            if p.exists():
                raw = p.read_bytes()
        if not raw:
            raise RuntimeError("empty image payload")
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("image exceeds 4MB bound")
        if not mime or not mime.startswith("image/"):
            mime = _guess_image_mime(raw)
        if not mime.startswith("image/"):
            raise RuntimeError(f"not an image: {mime or 'unknown'}")
        return mime, f"data:{mime};base64,{_base64.b64encode(raw).decode()}"

    # ------------------------------------------------------------ confirmations

    def _consume_confirmation(self, incoming: IncomingEvent) -> bool:
        entry = self.pending_confirmations.pop(incoming.umo, None)
        if entry is None:
            return False
        # Enforce the 60-second confirmation window (P1): stale entries are
        # dropped instead of staying valid forever.
        if datetime.now(timezone.utc) > entry[1]:
            logger.info("[hdsi] 确认已超时，操作取消：%s", entry[0])
            return True
        answer = incoming.content.strip().lower()
        action = entry[0]
        if answer in ("y", "yes", "是", "确认"):
            asyncio.get_running_loop().create_task(self._run_confirmed(action, incoming))
        return True

    def _ask_confirmation(self, umo: str, action: str) -> bool:
        now = datetime.now(timezone.utc)
        # Drop other sessions' expired entries opportunistically.
        for key in [k for k, v in self.pending_confirmations.items() if now > v[1]]:
            self.pending_confirmations.pop(key, None)
        self.pending_confirmations[umo] = (action, now + timedelta(seconds=60))
        return True

    async def _run_confirmed(self, action: str, incoming: IncomingEvent) -> None:
        assert self.service is not None
        story = await self.service.find_story_for_event(incoming)
        if story is None:
            await self._reply(incoming.umo, "未找到当前故事。请先发送 hdsi init 主角名")
            return
        try:
            if action == "reset":
                await self.service.purge_all_data(story.id)
                await self._reply(incoming.umo,
                                  "已彻底重置所有平台剧本、记忆与 Canon。")
            elif action == "clear_character":
                await self.service.clear_setting_overlay(story, "character")
                await self._reply(incoming.umo, "已清理 character overlay。")
            elif action == "clear_relationship":
                result = await self.service.clear_setting_overlay(story, "relationship")
                await self._reply(incoming.umo,
                                  f"已清理 relationship overlay（{result['participant_count']} 个参与者）。")
            elif action == "clear_world":
                await self.service.clear_setting_overlay(story, "world")
                await self._reply(incoming.umo, "已清理 world overlay。")
            elif action == "clear_all_overlay":
                await self.service.clear_setting_overlay(story, "all")
                await self._reply(incoming.umo, "已清理全部设定演化 overlay。")
            else:
                await self._reply(incoming.umo, f"未知操作：{action}")
        except Exception as error:  # noqa: BLE001
            await self._reply(incoming.umo, f"操作失败：{error}")

    async def _reply(self, umo: str, text: str) -> None:
        try:
            session = MessageSession.from_str(umo)
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as error:  # noqa: BLE001
            logger.warning("[hdsi] 回复发送失败：%s", error)

    # ------------------------------------------------------------ web APIs

    async def _require_service(self):
        if self.service is None or self.db is None:
            raise RuntimeError("service not initialized")
        return self.service

    @staticmethod
    async def _request_json_body() -> Optional[dict]:
        """Read the JSON body via the dashboard request proxy when the
        framework did not inject it as handler kwargs."""
        try:
            from astrbot.api.web import request as _plugin_request

            body = await _plugin_request.json()
            if isinstance(body, dict):
                return body
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.api.web import request as _plugin_request

            raw = await _plugin_request.body()
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            return None

    async def _api_migrate_config(self, body: Optional[dict] = None):
        from .hdsi.config import deep_merge
        from .hdsi.migration import migrate_koishi_config

        incoming = body or (await self._request_json_body()) or {}
        koishi_config = incoming.get("koishi_config") if isinstance(incoming, dict) else None
        if not isinstance(koishi_config, dict):
            return {"status": "error", "message": "请求体需包含 {\"koishi_config\": {...原始 Koishi HDS-Interlude 配置...}}"}
        try:
            patch = migrate_koishi_config(koishi_config)
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "message": f"迁移失败：{error}"}
        merged = deep_merge(self.hdsi_config.model_dump(), patch)
        self.hdsi_config = HdsiConfig.model_validate(merged)
        if self.service is not None:
            self.service.config = self.hdsi_config
            self.service.narrator.slots = self.hdsi_config.models
        save_config_file(self.config_path, self.hdsi_config)
        return {"status": "ok", "message": "Koishi 配置已导入并保存", "data": patch}

    async def _resolve_story_for_req(self, character_id: Optional[str] = None) -> tuple[InterludeService, Optional[InterludeStory]]:
        service = await self._require_service()
        story = await service.latest_active_story(character_id)
        return service, story

    async def _api_overview(self, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": {"story": None}}
        intents_rows = await self.db.get(
            "interlude_intent", {"story_id": story.id, "status": "pending"},
            order_by="not_before", limit=20,
        )
        recent = await service.recent_entries(story.id, 8)
        state = story.state
        return {
            "status": "ok",
            "data": {
                "story": {
                    "id": story.id,
                    "characterName": story.setting.character.name,
                    "status": story.status.value,
                    "cursorAt": iso(story.cursor_at),
                    "localTime": format_log_time(datetime.now(timezone.utc), story.setting.timezone),
                    "timezone": story.setting.timezone,
                    "participants": len(await service.participants(story.id)),
                    "protagonistOverlay": state.setting_overlay.model_dump(exclude_none=True),
                    "continuity": state.continuity_snapshot.model_dump() if state.continuity_snapshot else None,
                    "agencyWindow": state.agency_window.model_dump() if state.agency_window else None,
                    "alterSystem": {
                        "alterValue": state.alter_system.alter_value if state.alter_system else 0,
                        "hasOffset": bool(state.alter_system and state.alter_system.emotional_offset),
                    },
                    "automation": {
                        "nextAdvanceAt": state.automation.next_advance_at,
                        "lastAutoAdvanceAt": state.automation.last_auto_advance_at,
                        "followUps": state.automation.conversation_follow_up_at,
                    },
                    "allowProactive": self.hdsi_config.runtime.allow_proactive_messages,
                },
                "pendingIntents": [
                    {
                        "id": r["id"], "type": r["type"], "participantId": r["participant_id"],
                        "summary": r["summary"], "notBefore": r["not_before"],
                    }
                    for r in intents_rows
                ],
                "recentScript": [
                    {
                        "id": e.id, "kind": e.kind, "actor": e.actor,
                        "occurredAt": iso(e.occurred_at),
                        "content": e.content[:300],
                    }
                    for e in recent
                ],
            },
        }

    async def _api_get_config(self):
        cfg_dump = self.hdsi_config.model_dump(by_alias=False)
        return {"status": "ok", "data": cfg_dump}

    async def _api_set_config(self, payload: Optional[dict] = None, body: Optional[dict] = None):
        from .hdsi.config import deep_merge

        incoming = body or payload or (await self._request_json_body()) or {}
        if not isinstance(incoming, dict) or not incoming:
            return {"status": "error", "message": "配置必须是 JSON 对象"}
        # Global config update only — do NOT mutate character story settings here!
        merged = deep_merge(self.hdsi_config.model_dump(), incoming)
        try:
            updated = HdsiConfig.model_validate(merged)
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "message": f"配置校验失败：{error}"}
        self.hdsi_config = updated
        if self.service is not None:
            self.service.config = updated
            self.service.narrator.slots = updated.models
        save_config_file(self.config_path, updated)
        if self.raw_config is not None:
            try:
                for key in ("main_model", "compaction_model", "alter_model", "embedding_model"):
                    setattr(self.raw_config, key, getattr(updated.models, key))
                setattr(self.raw_config, "enable", updated.enable)
                self.raw_config.save_config()
            except Exception:  # noqa: BLE001
                pass
        return {"status": "ok"}

    async def _api_characters_canon_get(self, character_id: Optional[str] = None):
        service = await self._require_service()
        char_id = character_id
        if not char_id:
            def_char = await service.get_default_character()
            char_id = def_char.id if def_char else None
        if not char_id:
            return {"status": "error", "message": "未找到角色"}
        char = await service.get_character(char_id)
        if not char:
            return {"status": "error", "message": f"角色不存在：{char_id}"}
        story = await service.get_story(char.story_id)
        s = story.setting
        return {
            "status": "ok",
            "data": {
                "character_id": char.id,
                "character_name": s.character.name,
                "timezone": s.timezone or "Asia/Shanghai",
                "character_profile": s.character.profile,
                "world": s.world,
                "relationship": s.relationship,
                "prompts": {
                    "main_prompt": self.hdsi_config.prompts.main_prompt,
                    "style_prompt": self.hdsi_config.prompts.style_prompt,
                },
                "supporting_cast": s.supporting_cast,
                "location": s.location,
                "style": s.style,
            },
        }

    async def _api_characters_canon_set(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = str(incoming.get("character_id") or incoming.get("id") or "").strip()
        if not char_id:
            return {"status": "error", "message": "缺少 character_id"}
        service = await self._require_service()
        char = await service.get_character(char_id)
        if not char:
            return {"status": "error", "message": f"角色不存在：{char_id}"}
        story = await service.get_story(char.story_id)

        name = str(incoming.get("character_name") or incoming.get("name") or story.setting.character.name).strip()
        profile = str(incoming.get("character_profile") or incoming.get("profile") if "character_profile" in incoming or "profile" in incoming else story.setting.character.profile).strip()
        world = str(incoming.get("world") if "world" in incoming else story.setting.world)
        relationship = str(incoming.get("relationship") if "relationship" in incoming else story.setting.relationship)
        timezone_str = str(incoming.get("timezone") if "timezone" in incoming else story.setting.timezone)

        story.setting.character.name = name
        story.setting.character.profile = profile
        story.setting.world = world
        story.setting.relationship = relationship
        story.setting.timezone = timezone_str

        prompts = incoming.get("prompts")
        if isinstance(prompts, dict):
            if "main_prompt" in prompts:
                self.hdsi_config.prompts.main_prompt = prompts["main_prompt"]
            if "style_prompt" in prompts:
                self.hdsi_config.prompts.style_prompt = prompts["style_prompt"]
            save_config_file(self.config_path, self.hdsi_config)

        now = service.now()
        await self.db.update("interlude_story", {"id": story.id}, {
            "setting": story.setting.model_dump(mode="json"),
            "updated_at": iso(now),
        })

        await self.db.update("interlude_character", {"id": char.id}, {
            "name": name,
            "description": profile,
            "updated_at": iso(now),
        })

        return {"status": "ok", "message": f"角色【{name}】设定已保存 ✓"}

    async def _api_participants(self, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": []}
        participants = await service.participants(story.id, include_paused=True)
        return {
            "status": "ok",
            "data": [
                {
                    "id": p.id, "displayName": p.display_name,
                    "personId": p.person_id, "relationship": p.relationship,
                    "profile": p.profile, "status": p.status,
                    "state": p.state.model_dump(), "umo": p.umo,
                }
                for p in participants
            ],
        }

    async def _api_participants_update(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        part_id = str(incoming.get("id") or incoming.get("participant_id") or "").strip()
        if not part_id:
            return {"status": "error", "message": "缺少参与者 ID"}
        service = await self._require_service()
        rows = await self.db.get("interlude_participant", {"id": part_id})
        if not rows:
            return {"status": "error", "message": f"未找到参与者 {part_id}"}
        update_data = {}
        if "displayName" in incoming or "display_name" in incoming:
            update_data["display_name"] = incoming.get("displayName") if "displayName" in incoming else incoming.get("display_name")
        if "relationship" in incoming:
            update_data["relationship"] = incoming.get("relationship")
        if "profile" in incoming:
            update_data["profile"] = incoming.get("profile")
        if "status" in incoming:
            update_data["status"] = incoming.get("status")
        if update_data:
            update_data["updated_at"] = iso(service.now())
            await self.db.update("interlude_participant", {"id": part_id}, update_data)
        return {"status": "ok", "message": "参与者信息已更新"}

    async def _api_participants_clear_unread(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        part_id = str(incoming.get("id") or incoming.get("participant_id") or "").strip()
        if not part_id:
            return {"status": "error", "message": "缺少参与者 ID"}
        service = await self._require_service()
        rows = await self.db.get("interlude_participant", {"id": part_id})
        if not rows:
            return {"status": "error", "message": f"未找到参与者 {part_id}"}
        p = InterludeParticipant.model_validate(rows[0])
        p.state.unread_message_count = 0
        p.state.pending_message_count = 0
        await self.db.update("interlude_participant", {"id": part_id}, {
            "state": p.state.model_dump(mode="json"),
            "updated_at": iso(service.now()),
        })
        return {"status": "ok", "message": "参与者未读消息计数已清除"}

    async def _api_participants_reset(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        part_id = str(incoming.get("id") or incoming.get("participant_id") or "").strip()
        if not part_id:
            return {"status": "error", "message": "缺少参与者 ID"}
        service = await self._require_service()
        rows = await self.db.get("interlude_participant", {"id": part_id})
        if not rows:
            return {"status": "error", "message": f"未找到参与者 {part_id}"}
        from .hdsi.types import empty_participant_state
        await self.db.update("interlude_participant", {"id": part_id}, {
            "state": empty_participant_state().model_dump(mode="json"),
            "updated_at": iso(service.now()),
        })
        return {"status": "ok", "message": "参与者关系运行状态已重置"}

    async def _api_participants_delete(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        part_id = str(incoming.get("id") or incoming.get("participant_id") or "").strip()
        if not part_id:
            return {"status": "error", "message": "缺少参与者 ID"}
        purge = bool(incoming.get("purge_data", False))
        if purge:
            stmts = [
                ("DELETE FROM interlude_script_entry WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_memory WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_fact WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_intent WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_state_patch WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_overlay_snapshot WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_web_observation WHERE participant_id=?", (part_id,)),
                ("DELETE FROM interlude_participant WHERE id=?", (part_id,)),
            ]
            await self.db.execute_many(stmts)
            return {"status": "ok", "message": f"参与者 {part_id} 及其专属记忆已彻底清理"}
        else:
            await self.db.execute("DELETE FROM interlude_participant WHERE id=?", (part_id,))
            return {"status": "ok", "message": f"参与者 {part_id} 已移除（历史记录已保留）"}

    async def _api_memory_list(self, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": []}
        rows = await self.db.get(
            "interlude_memory", {"story_id": story.id},
            order_by="importance", descending=True, limit=100,
        )
        return {"status": "ok", "data": rows}

    async def _api_memory_create(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = incoming.get("character_id")
        service, story = await self._resolve_story_for_req(char_id)
        if story is None:
            return {"status": "error", "message": "没有活动故事"}
        content = str(incoming.get("content", "")).strip()
        if not content:
            return {"status": "error", "message": "记忆内容不能为空"}
        category = str(incoming.get("category", "fact")).strip()
        participant_id = str(incoming.get("participant_id", "")).strip()
        importance = float(incoming.get("importance", 0.5))
        now = service.now()
        mem_id = await self.db.insert_returning_id("interlude_memory", {
            "story_id": story.id,
            "participant_id": participant_id,
            "category": category,
            "content": content,
            "importance": importance,
            "status": "active",
            "source_entry_id": None,
            "created_at": iso(now),
            "updated_at": iso(now),
        })
        return {"status": "ok", "message": f"已添加长期记忆 #{mem_id}", "data": {"id": mem_id}}

    async def _api_memory_delete(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        mem_id = incoming.get("id")
        if not mem_id:
            return {"status": "error", "message": "缺少记忆 ID"}
        await self.db.execute("DELETE FROM interlude_memory WHERE id=?", (int(mem_id),))
        return {"status": "ok", "message": f"记忆 #{mem_id} 已删除"}

    async def _api_script(self, limit: int = 30, offset: int = 0, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": []}
        rows = await self.db.fetch_all(
            "SELECT * FROM interlude_script_entry WHERE story_id=? "
            "ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
            (story.id, max(1, min(int(limit), 200)), max(0, int(offset))),
        )
        return {
            "status": "ok",
            "data": [self.db.row_to_dict("interlude_script_entry", r) for r in rows],
        }

    async def _api_intents(self, include_completed: bool = False, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": []}
        query: dict[str, Any] = {"story_id": story.id}
        if not include_completed:
            query["status"] = "pending"
        rows = await self.db.get("interlude_intent", query,
                                 order_by="not_before", limit=100)
        return {"status": "ok", "data": rows}

    async def _api_maintenance(self, body: Optional[dict] = None, character_id: Optional[str] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = incoming.get("character_id") or character_id
        action = str(incoming.get("action", "")).strip()
        arg = str(incoming.get("arg", "")).strip()
        service, story = await self._resolve_story_for_req(char_id)
        if story is None:
            return {"status": "error", "message": "没有活动故事"}
        if action == "advance":
            messages = await service.advance_story(story)
            return {"status": "ok", "message": f"推进完成，可见消息 {len(messages)} 条"}
        if action == "compact":
            done = await service.compact_story(story)
            return {"status": "ok", "message": "已整理" if done else "未达到整理阈值"}
        if action == "compact_overlay":
            done = await service.compact_overlay(story)
            return {"status": "ok", "message": "overlay 已压缩" if done else "无需压缩"}
        if action == "rebuild_continuity":
            await service.ensure_continuity(story, datetime.now(timezone.utc))
            return {"status": "ok", "message": "continuity 已重建"}
        if action == "clear_overlay":
            target = arg if arg in ("character", "relationship", "world", "all") else "all"
            await service.clear_setting_overlay(story, target)
            return {"status": "ok", "message": f"{target} overlay 已清理"}
        if action == "cancel_intent" and arg.isdigit():
            await self.db.update("interlude_intent",
                                 {"id": int(arg), "story_id": story.id},
                                 {"status": "cancelled"})
            return {"status": "ok", "message": f"意图 #{arg} 已取消"}
        if action == "purge":
            await service.purge_all_data(story.id)
            return {"status": "ok", "message": "故事已重置为空白 Canon"}
        return {"status": "error", "message": f"未知维护操作：{action}"}

    async def _api_characters_list(self):
        service = await self._require_service()
        chars = await service.list_characters(include_archived=True)
        default_char = await service.get_default_character()
        return {
            "status": "ok",
            "data": {
                "characters": [c.model_dump(mode="json") for c in chars],
                "defaultCharacterId": default_char.id if default_char else None,
            },
        }

    async def _api_characters_detail(self, character_id: Optional[str] = None):
        service = await self._require_service()
        char_id = character_id
        if not char_id:
            default_char = await service.get_default_character()
            char_id = def_char.id if def_char else None
        if not char_id:
            return {"status": "error", "message": "未找到角色"}
        char = await service.get_character(char_id)
        if not char:
            return {"status": "error", "message": f"角色不存在：{char_id}"}
        story = await service.get_story(char.story_id)
        return {
            "status": "ok",
            "data": {
                "character": char.model_dump(mode="json"),
                "story": story.model_dump(mode="json"),
                "canon": story.setting.model_dump(mode="json"),
            },
        }

    async def _api_characters_create(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        name = str(incoming.get("name", "")).strip()
        if not name:
            return {"status": "error", "message": "角色名称不能为空"}
        desc = str(incoming.get("description", "")).strip()
        avatar = str(incoming.get("avatar", "")).strip()
        canon = incoming.get("canon")
        is_default = bool(incoming.get("is_default", False))
        service = await self._require_service()
        rec = await service.create_character_record(
            name=name, description=desc, avatar=avatar, canon=canon, is_default=is_default,
        )
        return {"status": "ok", "message": f"角色【{rec.name}】创建成功", "data": rec.model_dump(mode="json")}

    async def _api_characters_update(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = str(incoming.get("id") or incoming.get("character_id") or "").strip()
        if not char_id:
            return {"status": "error", "message": "缺少 character_id"}
        service = await self._require_service()
        try:
            rec = await service.update_character_record(
                character_id=char_id,
                name=incoming.get("name"),
                description=incoming.get("description"),
                avatar=incoming.get("avatar"),
                status=incoming.get("status"),
                is_default=incoming.get("is_default"),
                canon=incoming.get("canon"),
            )
            return {"status": "ok", "message": "角色更新成功", "data": rec.model_dump(mode="json")}
        except Exception as err:
            return {"status": "error", "message": f"更新失败：{err}"}

    async def _api_characters_clone(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = str(incoming.get("character_id") or incoming.get("id") or "").strip()
        new_name = str(incoming.get("name", "")).strip()
        service = await self._require_service()
        try:
            cloned = await service.clone_character_record(char_id, new_name)
            return {"status": "ok", "message": f"已成功复制为【{cloned.name}】", "data": cloned.model_dump(mode="json")}
        except Exception as err:
            return {"status": "error", "message": f"复制失败：{err}"}

    async def _api_characters_delete(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = str(incoming.get("character_id") or incoming.get("id") or "").strip()
        purge = bool(incoming.get("purge_data", False))
        service = await self._require_service()
        all_chars = await service.list_characters(include_archived=False)
        if len(all_chars) <= 1 and all_chars[0].id == char_id:
            return {"status": "error", "message": "不能删除系统中唯一的活跃角色"}
        try:
            await service.delete_or_archive_character(char_id, purge_data=purge)
            return {"status": "ok", "message": "角色已删除" if purge else "角色已归档"}
        except Exception as err:
            return {"status": "error", "message": f"删除失败：{err}"}

    async def _api_characters_set_default(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = str(incoming.get("character_id") or incoming.get("id") or "").strip()
        service = await self._require_service()
        ok = await service.set_default_character(char_id)
        if ok:
            return {"status": "ok", "message": f"已设置 {char_id} 为默认角色"}
        return {"status": "error", "message": f"未找到角色：{char_id}"}

    async def _api_characters_export(self, character_id: Optional[str] = None):
        service = await self._require_service()
        char_id = character_id
        if not char_id:
            def_char = await service.get_default_character()
            char_id = def_char.id if def_char else None
        if not char_id:
            return {"status": "error", "message": "未找到角色"}
        try:
            data = await service.export_character_config(char_id)
            return {"status": "ok", "data": data}
        except Exception as err:
            return {"status": "error", "message": str(err)}

    async def _api_characters_import(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        payload = incoming.get("payload") or incoming
        name_override = incoming.get("name_override")
        service = await self._require_service()
        try:
            rec = await service.import_character_config(payload, name_override=name_override)
            return {"status": "ok", "message": f"已成功导入角色【{rec.name}】", "data": rec.model_dump(mode="json")}
        except Exception as err:
            return {"status": "error", "message": f"导入失败：{err}"}

    async def _api_bindings_list(self):
        service = await self._require_service()
        bindings = await service.list_conversation_bindings()
        chars = await service.list_characters(include_archived=True)
        char_map = {c.id: c.name for c in chars}
        return {
            "status": "ok",
            "data": {
                "bindings": [
                    {
                        **b.model_dump(mode="json"),
                        "characterName": char_map.get(b.character_id, b.character_id),
                    }
                    for b in bindings
                ],
                "characters": [{"id": c.id, "name": c.name} for c in chars if c.status == CharacterStatus.ACTIVE],
            },
        }

    async def _api_bindings_save(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        platform_id = str(incoming.get("platform_id", "")).strip()
        self_id = str(incoming.get("self_id", "")).strip()
        conv_type = str(incoming.get("conversation_type", "all")).strip() or "all"
        conv_id = str(incoming.get("conversation_id", "")).strip()
        char_id = str(incoming.get("character_id", "")).strip()
        if not char_id:
            return {"status": "error", "message": "必须指定绑定的角色"}
        if not conv_id:
            return {"status": "error", "message": "必须指定会话 ID（或 * 通配）"}
        service = await self._require_service()
        try:
            b = await service.set_conversation_binding(platform_id, self_id, conv_id, char_id, conversation_type=conv_type)
            return {"status": "ok", "message": "绑定成功", "data": b.model_dump(mode="json")}
        except Exception as err:
            return {"status": "error", "message": f"保存绑定失败：{err}"}

    async def _api_bindings_delete(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        platform_id = str(incoming.get("platform_id", "")).strip()
        self_id = str(incoming.get("self_id", "")).strip()
        conv_type = incoming.get("conversation_type")
        conv_id = str(incoming.get("conversation_id", "")).strip()
        service = await self._require_service()
        ok = await service.delete_conversation_binding(platform_id, self_id, conv_id, conversation_type=conv_type)
        if ok:
            return {"status": "ok", "message": "已解除绑定"}
        return {"status": "error", "message": "未找到对应绑定记录"}

    async def _api_facts_list(self, character_id: Optional[str] = None):
        service, story = await self._resolve_story_for_req(character_id)
        if story is None:
            return {"status": "ok", "data": []}
        rows = await self.db.get("interlude_fact", {"story_id": story.id}, order_by="importance", descending=True, limit=100)
        return {"status": "ok", "data": rows}

    async def _api_facts_create(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        char_id = incoming.get("character_id")
        service, story = await self._resolve_story_for_req(char_id)
        if story is None:
            return {"status": "error", "message": "没有活动故事"}
        content = str(incoming.get("content", "")).strip()
        if not content:
            return {"status": "error", "message": "事实内容不能为空"}
        scope = str(incoming.get("scope", "world")).strip()
        importance = float(incoming.get("importance", 0.5))
        confidence = float(incoming.get("confidence", 0.8))
        now = service.now()
        fact_id = await self.db.insert_returning_id("interlude_fact", {
            "story_id": story.id,
            "participant_id": "",
            "scope": scope,
            "content": content,
            "importance": importance,
            "confidence": confidence,
            "unresolved": 0,
            "embedding": None,
            "status": "active",
            "source_entry_ids": "[]",
            "last_seen_at": iso(now),
            "created_at": iso(now),
            "updated_at": iso(now),
        })
        return {"status": "ok", "message": f"已添加事实 #{fact_id}", "data": {"id": fact_id}}

    async def _api_facts_delete(self, body: Optional[dict] = None):
        incoming = body or (await self._request_json_body()) or {}
        fact_id = incoming.get("id")
        if not fact_id:
            return {"status": "error", "message": "缺少事实 ID"}
        await self.db.execute("DELETE FROM interlude_fact WHERE id=?", (int(fact_id),))
        return {"status": "ok", "message": f"事实 #{fact_id} 已删除"}

    async def _api_backup(self):
        from .hdsi.database.migrations import SCHEMA_VERSION, TABLES

        service = await self._require_service()
        table_data = {}
        table_counts = {}
        for tbl in TABLES:
            try:
                rows = await self.db.get(tbl, {})
                table_data[tbl] = rows
                table_counts[tbl] = len(rows)
            except Exception as err:
                logger.warning("[hdsi] 备份表 %s 失败: %s", tbl, err)
                table_data[tbl] = []
                table_counts[tbl] = 0

        manifest = {
            "version": 2,
            "schema_version": SCHEMA_VERSION,
            "created_at": iso(service.now()),
            "table_counts": table_counts,
            "total_records": sum(table_counts.values()),
        }
        return {
            "status": "ok",
            "data": {
                "manifest": manifest,
                "config": self.hdsi_config.model_dump(mode="json"),
                "tables": table_data,
            },
        }

    async def _api_restore(self, body: Optional[dict] = None):
        from .hdsi.database.migrations import TABLES

        incoming = body or (await self._request_json_body()) or {}
        data = incoming.get("data") or incoming
        if not isinstance(data, dict):
            return {"status": "error", "message": "备份数据格式错误"}
        service = await self._require_service()

        tables = data.get("tables")
        if not tables and ("characters" in data or "stories" in data):
            # Compatibility fallback for legacy v1 partial backup
            tables = {
                "interlude_character": data.get("characters", []),
                "interlude_conversation_binding": data.get("bindings", []),
                "interlude_story": data.get("stories", []),
                "interlude_participant": data.get("participants", []),
                "interlude_fact": data.get("facts", []),
            }
        if not isinstance(tables, dict):
            return {"status": "error", "message": "缺少表格数据 (tables)"}

        # Clear existing rows in reverse dependency order
        clear_stmts = [(f"DELETE FROM {tbl}", ()) for tbl in reversed(TABLES)]
        await self.db.execute_many(clear_stmts)

        restored_counts = {}
        for tbl in TABLES:
            rows = tables.get(tbl) or []
            count = 0
            for r in rows:
                if isinstance(r, dict):
                    await self.db.insert(tbl, r)
                    count += 1
            restored_counts[tbl] = count

        cfg_raw = data.get("config")
        if isinstance(cfg_raw, dict):
            try:
                self.hdsi_config = HdsiConfig.model_validate(cfg_raw)
                service.config = self.hdsi_config
                service.narrator.slots = self.hdsi_config.models
                save_config_file(self.config_path, self.hdsi_config)
            except Exception as err:
                logger.warning("[hdsi] 恢复配置失败: %s", err)

        return {
            "status": "ok",
            "message": f"系统已成功恢复（共恢复 {sum(restored_counts.values())} 条记录）",
            "data": restored_counts,
        }

    # ------------------------------------------------------------ helpers

    def _looks_like_command(self, content: str) -> bool:
        if not self.hdsi_config.runtime.ignore_command_messages:
            return False
        stripped = content.strip().lstrip("/!.点")
        lowered = stripped.lower()
        return any(lowered == prefix or lowered.startswith(prefix + " ") for prefix in COMMAND_PREFIXES)


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    import html as html_mod

    return html_mod.unescape(_TAG_RE.sub("", value or ""))


_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.I | re.S)
_BLOCK_RE = re.compile(r"</?(?:p|div|br|li|tr|h[1-6]|section|article|table|ul|ol)[^>]*>", re.I)


def extract_visible_text(html: str) -> str:
    body_match = re.search(r"<body\b.*?</body>", html, re.I | re.S)
    body = body_match.group(0) if body_match else html
    body = _SCRIPT_RE.sub("", body)
    body = _STYLE_RE.sub("", body)
    body = _BLOCK_RE.sub("\n", body)
    text = strip_html(body)
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    collapsed: list[str] = []
    for line in non_empty:
        if collapsed and collapsed[-1] == line:
            continue
        collapsed.append(line)
    return "\n".join(collapsed)[:50_000]


# ------------------------------------------------------------------ admin commands

def _require_manager(plugin: "HdsiInterludePlugin", event: AstrMessageEvent) -> str | None:
    assert plugin.service is not None
    incoming = plugin.normalize_event(event)
    if incoming is None or not plugin.service.can_manage_event(incoming):
        return "无权限：当前账号不是 HDSI 管理员。"
    return None


async def _find_or_hint(plugin: "HdsiInterludePlugin", event: AstrMessageEvent):
    """Returns (incoming, story); either may be None."""
    if plugin.service is None:
        return None, None
    incoming = plugin.normalize_event(event)
    story = await plugin.service.find_story_for_event(incoming) if incoming else None
    return incoming, story


@filter.command_group("hdsi")
async def hdsi(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    """HDS Interlude 管理命令组"""
    if self.service is None:
        yield event.plain_result("服务尚未初始化完成，请稍后再试。")
        return
    story = await self.service.latest_active_story()
    if story is None:
        yield event.plain_result(
            "HDS Interlude 持续叙事运行时。\n"
            "常用命令：hdsi init 主角名 / hdsi status / hdsi advance / hdsi timeline\n"
            "完整管理面板请访问 WebUI 插件页面。"
        )
        return
    state = story.state
    lines = [
        f"主角：{story.setting.character.name}",
        f"故事状态：{story.status.value}",
        f"已写到：{iso(story.cursor_at)}（本地 {format_log_time(story.cursor_at, story.setting.timezone)}）",
        f"参与者：{len(await self.service.participants(story.id))}",
        f"自动推进：{'开启' if self.hdsi_config.runtime.auto_advance_enabled else '关闭'}"
        f"，下次 {state.automation.next_advance_at or '未安排'}",
        f"主动联系：{'开启' if self.hdsi_config.runtime.allow_proactive_messages else '关闭'}",
        f"Agency Window：{state.agency_window.activity_load if state.agency_window else '尚未建立'}",
    ]
    yield event.plain_result("\n".join(lines))


@hdsi.command("init")
async def hdsi_init(self: "HdsiInterludePlugin", event: AstrMessageEvent, name: str = ""):
    if self.service is None:
        yield event.plain_result("服务尚未初始化完成。")
        return
    incoming = self.normalize_event(event)
    if incoming is None or not self.service.can_handle_event(incoming):
        yield event.plain_result("当前账号未获 HDSI 互动授权。请在管理面板检查平台与用户白名单。")
        return
    existing = await self.service.find_story_for_event(incoming)
    if existing is not None:
        participant = await self.service.ensure_participant(existing, incoming)
        yield event.plain_result(
            f"已把 {participant.display_name} 加入 {existing.setting.character.name} 的共享主剧本。"
        )
        return
    story = await self.service.create_story(incoming, name or None)
    participant = await self.service.find_participant_for_event(incoming, story)
    yield event.plain_result(
        f"已创建 {story.setting.character.name} 的共享主剧本，并加入 "
        f"{participant.display_name if participant else incoming.sender_name}。"
    )


@hdsi.command("status")
async def hdsi_status(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    async for result in _status_impl(self, event):
        yield result


async def _status_impl(plugin: "HdsiInterludePlugin", event: AstrMessageEvent):
    if plugin.service is None:
        yield event.plain_result("服务尚未初始化完成。")
        return
    incoming = plugin.normalize_event(event)
    if incoming is None:
        yield event.plain_result("无法解析当前会话。")
        return
    story = await plugin.service.find_story_for_event(incoming)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    participant = await plugin.service.find_participant_for_event(incoming, story)
    scene = await plugin.service.active_scene(story.id)
    arc = await plugin.service.active_arc(story.id)
    facts = await plugin.service.facts(story.id, 8, "", participant.id if participant else None)
    lines = [
        f"主角：{story.setting.character.name}",
        f"故事状态：{story.status.value}；游标：{iso(story.cursor_at)}",
        f"关系分支：{participant.display_name if participant else '未加入'}"
        f"（{participant.relationship[:60] if participant else '-'}）",
        f"场景引子：{(scene.hook[:80] if scene and scene.hook else '尚未整理')}",
        f"剧情弧线：{arc.title if arc else '开场'}",
        f"长期事实：{len(facts)} 条可检索",
        f"主体行动窗口：{story.state.agency_window.activity_load if story.state.agency_window else '尚未建立'}",
        f"Alter 累计：{story.state.alter_system.alter_value if story.state.alter_system else 0}",
    ]
    yield event.plain_result("\n".join(lines))


@hdsi.command("pause")
async def hdsi_pause(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    await self.db.update("interlude_story", {"id": story.id},
                         {"status": "paused", "updated_at": iso(datetime.now(timezone.utc))})
    yield event.plain_result("故事已暂停自动处理；已有记录不会删除。")


@hdsi.command("resume")
async def hdsi_resume(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    await self.db.update("interlude_story", {"id": story.id},
                         {"status": "active", "updated_at": iso(datetime.now(timezone.utc))})
    yield event.plain_result("故事已恢复自动处理。")


@hdsi.command("advance")
async def hdsi_advance(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    messages = await self.service.advance_story(story)
    text = ("剧本已补写到现在，并已投递其中已经发生的可见角色消息。" if messages
            else "剧本已补写到现在；这次没有发生可见角色消息。")
    yield event.plain_result(text)


@hdsi.command("timeline")
async def hdsi_timeline(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                        limit: int = 10):
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    participant = await self.service.find_participant_for_event(incoming, story) \
        if self.service and incoming else None
    entries = await self.service.recent_entries(story.id, max(1, min(limit * 3, 90)))
    pid = participant.id if participant else None
    visible = [e for e in entries if not e.participant_id or e.participant_id == pid]
    visible = visible[-max(1, min(limit, 30)):]
    if not visible:
        yield event.plain_result("当前故事还没有剧本记录。")
        return
    body = "\n".join(
        f"[{iso(e.occurred_at)}] {e.actor}/{e.kind}: {e.content}" for e in visible
    )
    yield event.plain_result(body)


@hdsi.command("context")
async def hdsi_context(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    scene = await self.service.active_scene(story.id)
    arc = await self.service.active_arc(story.id)
    overlay = story.state.setting_overlay.model_dump(exclude_none=True)
    agency = story.state.agency_window.model_dump() if story.state.agency_window else None
    continuity = story.state.continuity_snapshot.model_dump() if story.state.continuity_snapshot else None
    body = "\n".join([
        f"场景引子：{scene.hook[:120] if scene and scene.hook else '尚未整理'}",
        f"场景摘要：{scene.summary[:200] if scene and scene.summary else '尚未整理'}",
        f"剧情弧线：{arc.title if arc else '开场'} — {(arc.summary[:160] if arc and arc.summary else '尚未整理')}",
        f"continuity：{json.dumps(continuity, ensure_ascii=False)[:400] if continuity else '尚无'}",
        f"主角全局 overlay：{json.dumps(overlay, ensure_ascii=False)[:300] or '{}'}",
        f"Agency Window：{json.dumps(agency, ensure_ascii=False)[:300] if agency else 'null'}",
    ])
    yield event.plain_result(body)


@hdsi.command("script")
async def hdsi_script(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                      limit: int = 20):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    entries = await self.service.recent_entries(story.id, max(1, min(limit, 50)))
    if not entries:
        yield event.plain_result("当前主剧本还没有原始条目。")
        return
    body = "\n\n".join(
        f"#{e.id} [{iso(e.occurred_at)}] {e.actor}/{e.kind}"
        f"{('/' + e.participant_id) if e.participant_id else ''}\n{e.content}"
        for e in entries
    )
    yield event.plain_result(body[:4000])


@hdsi.command("note")
async def hdsi_note(self: "HdsiInterludePlugin", event: AstrMessageEvent, content: str = ""):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    from .hdsi.types import ScriptEntryDraft

    if not content.strip():
        yield event.plain_result("注记为空，未写入。用法：hdsi note <内容>")
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    now = datetime.now(timezone.utc)
    await self.service.append_entry(story.id, ScriptEntryDraft(
        kind="admin-note", actor="system",
        content=f"[管理员注记] {content.strip()}",
        occurred_at=iso(now), metadata={"source": "administrator"},
    ), now)
    self.service.schedule_compaction(story.id)
    yield event.plain_result("已写入管理员注记，后续压缩会将其纳入连续性。")


@hdsi.command("memory")
async def hdsi_memory(self: "HdsiInterludePlugin", event: AstrMessageEvent, limit: int = 10):
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    participant = await self.service.find_participant_for_event(incoming, story)
    memories = await self.service.memories(
        story.id, max(1, min(limit, 30)), participant.id if participant else None
    )
    if not memories:
        yield event.plain_result("暂时还没有提取出耐久记忆；多进行一些对话并等待后台整理后再看。")
        return
    body = "\n".join(f"[{m.category}/{m.importance:.2f}] {m.content}" for m in memories)
    yield event.plain_result(body[:4000])


@hdsi.command("facts")
async def hdsi_facts(self: "HdsiInterludePlugin", event: AstrMessageEvent, limit: int = 20):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    rows = await self.db.get("interlude_fact",
                             {"story_id": story.id, "status": "active"},
                             order_by="updated_at", descending=True,
                             limit=max(1, min(limit, 100)))
    if not rows:
        yield event.plain_result("当前没有有效的长期事实。")
        return
    body = "\n\n".join(
        f"#{r['id']} [{r['scope']}] 重要度={r['importance']:.2f} 置信度={r['confidence']:.2f}"
        f" 未解决={'是' if r.get('unresolved') else '否'}\n{r['content']}"
        for r in rows
    )
    yield event.plain_result(body[:4000])


@hdsi.command("addfact")
async def hdsi_addfact(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                       scope: str = "", content: str = ""):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    valid_scopes = ("character", "world", "relationship", "event", "promise")
    if scope not in valid_scopes or not content.strip():
        yield event.plain_result(
            "用法：hdsi addfact <character|world|relationship|event|promise> <内容>"
        )
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    now = datetime.now(timezone.utc)
    embedding = await self.service.embed_text(content.strip())
    from .hdsi.types import FactScope

    await self.db.insert("interlude_fact", {
        "story_id": story.id, "participant_id": "", "scope": scope,
        "content": content.strip()[:self.hdsi_config.memory.fact_content_characters],
        "importance": 0.8, "confidence": 1.0, "unresolved": 0,
        "embedding": embedding, "status": "active", "source_entry_ids": [],
        "last_seen_at": iso(now), "created_at": iso(now), "updated_at": iso(now),
    })
    yield event.plain_result("已添加高置信度长期事实。")


@hdsi.command("forgetfact")
async def hdsi_forgetfact(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                          fact_id: int = 0):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    rows = await self.db.get("interlude_fact",
                             {"id": fact_id, "story_id": story.id, "status": "active"})
    if not rows:
        yield event.plain_result(f"未找到有效的长期事实 #{fact_id}。")
        return
    await self.db.update("interlude_fact", {"id": fact_id},
                         {"status": "superseded", "updated_at": iso(datetime.now(timezone.utc))})
    yield event.plain_result(f"长期事实 #{fact_id} 已标记为失效。")


@hdsi.command("intents")
async def hdsi_intents(self: "HdsiInterludePlugin", event: AstrMessageEvent, limit: int = 20):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    rows = await self.db.get("interlude_intent",
                             {"story_id": story.id, "status": "pending"},
                             order_by="not_before", limit=max(1, min(limit, 100)))
    if not rows:
        yield event.plain_result("当前没有等待中的计划、提醒、承诺或剧情余波。")
        return
    parts = []
    for r in rows:
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        active = r["type"] == "active-consequence" and payload.get("lifecycle") == "active"
        timing = (f"持续影响至={payload.get('expiresAt', '未设置')}" if active
                  else f"最早执行={r['not_before']}")
        parts.append(f"#{r['id']} [{r['type']}] 参与者={r['participant_id'] or '全局'} {timing}\n{r['summary']}")
    yield event.plain_result("\n\n".join(parts)[:4000])


@hdsi.command("cancelintent")
async def hdsi_cancelintent(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                            intent_id: int = 0):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming, story = await _find_or_hint(self, event)
    if story is None:
        yield event.plain_result("当前还没有故事。请先发送：hdsi init 主角名")
        return
    rows = await self.db.get("interlude_intent",
                             {"id": intent_id, "story_id": story.id, "status": "pending"})
    if not rows:
        yield event.plain_result(f"未找到等待中的意图 #{intent_id}。")
        return
    await self.db.update("interlude_intent", {"id": intent_id},
                         {"status": "cancelled", "updated_at": iso(datetime.now(timezone.utc))})
    yield event.plain_result(f"意图 #{intent_id} 已取消。")


@hdsi.command("overlayclear")
async def hdsi_overlayclear(self: "HdsiInterludePlugin", event: AstrMessageEvent,
                            target: str = ""):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    normalized = (target or "").strip().lower()
    mapping = {
        "character": "clear_character", "relationship": "clear_relationship",
        "world": "clear_world", "all": "clear_all_overlay",
    }
    if normalized not in mapping:
        yield event.plain_result("target 必须是 character、relationship、world 或 all。")
        return
    self._ask_confirmation(event.unified_msg_origin, mapping[normalized])
    yield event.plain_result(
        f"即将清理 {normalized} overlay；剧本和记忆不会删除。确认请回复 y，取消回复其他内容。"
    )


@hdsi.command("reset")
async def hdsi_reset(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    self._ask_confirmation(event.unified_msg_origin, "reset")
    yield event.plain_result(
        "即将删除所有平台的剧本、记忆、事实、意图和状态，并按当前配置重建空白 Canon。"
        "确认请回复 y，取消回复其他内容。"
    )


@hdsi.command("chars")
async def hdsi_chars(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    if self.service is None:
        yield event.plain_result("服务尚未初始化完成。")
        return
    chars = await self.service.list_characters(include_archived=False)
    def_char = await self.service.get_default_character()
    lines = ["【HDSI 角色列表】"]
    for c in chars:
        tag = " [默认]" if (def_char and def_char.id == c.id) else ""
        lines.append(f"- {c.name} (ID: {c.id}){tag} - {c.description[:30] or '无简介'}")
    yield event.plain_result("\n".join(lines))


@hdsi.command("bind")
async def hdsi_bind(self: "HdsiInterludePlugin", event: AstrMessageEvent, char_identifier: str = ""):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming = self.normalize_event(event)
    if incoming is None:
        yield event.plain_result("无法解析当前会话。")
        return
    if not char_identifier.strip():
        yield event.plain_result("用法：hdsi bind <角色名称或ID>")
        return
    target = char_identifier.strip()
    chars = await self.service.list_characters(include_archived=False)
    matched = next((c for c in chars if c.id == target or c.name == target), None)
    if not matched:
        yield event.plain_result(f"未找到角色：{target}。请先通过 hdsi chars 查看可用角色。")
        return
    conv_id = incoming.group_id if incoming.message_type == "GroupMessage" else incoming.sender_id
    await self.service.set_conversation_binding(incoming.platform_id, incoming.self_id, conv_id, matched.id)
    yield event.plain_result(f"已将当前会话绑定至角色【{matched.name}】({matched.id})。")


@hdsi.command("unbind")
async def hdsi_unbind(self: "HdsiInterludePlugin", event: AstrMessageEvent):
    denied = _require_manager(self, event)
    if denied:
        yield event.plain_result(denied)
        return
    incoming = self.normalize_event(event)
    if incoming is None:
        yield event.plain_result("无法解析当前会话。")
        return
    conv_id = incoming.group_id if incoming.message_type == "GroupMessage" else incoming.sender_id
    ok = await self.service.delete_conversation_binding(incoming.platform_id, incoming.self_id, conv_id)
    if ok:
        yield event.plain_result("已解除当前会话的角色绑定，恢复默认角色。")
    else:
        yield event.plain_result("当前会话未设置独立角色绑定。")


def _guess_image_mime(raw: bytes) -> str:
    if len(raw) >= 3 and raw[0] == 0xFF and raw[1] == 0xD8 and raw[2] == 0xFF:
        return "image/jpeg"
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


async def _assert_public_dns(url: str) -> None:
    """Resolve every hostname to IPs and reject private/loopback targets."""
    import socket as _socket

    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise RuntimeError("URL 缺少主机名")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError as error:
        raise RuntimeError(f"DNS 解析失败：{host}") from error
    for info in infos:
        ip = str(info[4][0])
        if agency_mod.is_private_host(ip) or ip.startswith("127.") or ip == "::1":
            raise RuntimeError(f"目标解析到内网地址，已拦截：{host} → {ip}")
