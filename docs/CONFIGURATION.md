# 配置说明

配置分两层：

1. **插件配置**（WebUI → 插件 → astrbot_plugin_hdsi）：仅四个模型槽位绑定与总开关。
2. **完整配置**（WebUI → 插件页 HDS Interlude，或 `hdsi` 面板 API）：其余全部字段，
   持久化于 `data/plugin_data/astrbot_plugin_hdsi/config.json`。

## 1. 插件配置（_conf_schema.json）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| enable | true | 总开关；关闭后不拦截消息、后台暂停 |
| main_model | inherit | 主叙事模型槽。`inherit`=当前会话 Provider；`provider_id`；`provider_id:model`；逗号分隔为 failover 链 |
| compaction_model | inherit | 压缩/整理模型槽 |
| alter_model | inherit | Alter 侧端分析模型槽 |
| embedding_model | (空) | Embedding Provider id；留空禁用语义检索 |

## 2. story_defaults：剧本起点

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| character_name | Unnamed character | 主角名；`hdsi init 名字` 可覆盖一次 |
| character_profile | (空) | 背景、性格、日程、说话方式。大幅修改后建议清理 character overlay |
| user_profile / relationship | (空) | 未单独配置参与者时的默认值 |
| world / supporting_cast / location | (空) | 世界、配角、地点 |
| style | 现实主义日常叙事… | 故事文风，优先级高于全局 style_prompt |
| timezone | Asia/Shanghai | IANA 时区；驱动自动推进、休息窗口、日照预期 |

## 3. models：采样与故障切换

- `main_temperature` 0.8 / `main_top_p` 1.0
- `main_max_tokens` 0=用 Provider 默认；`main_timeout_ms` 0=默认
- `failover_enabled` true；`max_attempts_per_provider` 1；`cooldown_minutes` 5
- `vision_enabled` false：开启后私聊图片进入多模态主模型
- `embedding_live_query` false：实时查询是否额外做向量检索
- `embedding_backfill_batch_size` 5：每轮后台补齐的旧事实数

## 4. prompts

| 字段 | 说明 |
| --- | --- |
| main_prompt | 主叙事行为指令 |
| format_prompt | 结构化输出补充（只能扩展固定协议） |
| fixed_prompt | 全局长期约束 |
| style_prompt | 全局文风（故事级 style 可覆盖） |
| compaction_prompt / compaction_fixed_prompt / compaction_style_prompt | 压缩器指令 |

## 5. runtime：对话与时间

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| capture_direct_messages | true | 拦截并处理私聊 |
| auto_create | false | 无剧本时是否自动创建；关闭需 `hdsi init` |
| ignore_command_messages | true | 跳过 hdsi 命令 |
| allow_proactive_messages | false | 允许无新消息时主动发送可见消息 |
| proactive_willingness_threshold | 0.65 | 主动联系意愿门槛 |
| sweep_interval_minutes | 5 | 后台扫描周期（只发现到期任务） |
| minimum_advance_minutes | 30 | 手动 advance 的最小补写间隔 |
| context_entry_limit | 30 | 主模型读取的最近条目数 |
| memory_limit | 20 | 长期事实数量 |
| max_script_characters | 8000 | 单次剧本文本上限 |
| max_message_characters | 2000 | 单条可见消息上限 |
| minimum_delayed_reply_seconds | 10 | 最短延迟秒数 |
| maximum_delayed_reply_minutes | 1440 | 最长延迟分钟数 |
| cancel_delayed_replies_on_user_message | true | 新消息取消普通延迟计划 |
| narrative_retry_delay_seconds / max_attempts | 60 / 6 | 失败重试节奏 |
| split_reply_messages + message_separator | true, `<sep/>` | 分段气泡 |
| typing_base_delay_seconds / characters_per_second / max | 1 / 8 / 12 | 打字模拟 |
| user_message_debounce_seconds | 2 | 连续消息合并窗口 |
| auto_advance_enabled / interval / jitter | true / 40 / 5 | 自动推进节奏 |
| conversation_follow_up_minutes / jitter | [10,20] / 1 | 对话后短期补写 |
| rest_windows | [night sleep] | 低频窗口 HH:mm 跨午夜，min/max 分钟间隔 |

## 6. memory：连续性与记忆

阈值与权重沿用原版默认：场景整理 12 条/8000 字触发；事实排序
importance .5 / confidence .35 / recency .15 / semantic .55 / unresolved .2；
Overlay 证据门槛 confidence .82（major .95）、≥3 独立回合、≥2 个日历日、
冷却 72 小时；剧情余波上限 7 天、默认强度 0.55、每回合最多 6 条；
overlayRecentDays 2 / weekly 5 天窗 / monthly 10 天窗。

## 7. agency：主体行动窗口

enabled true；max_window_minutes 240；
minimum_proactive_interval_minutes 60（承诺型可绕过）；max_candidate_hours 24。

## 8. alter_system：情绪偏移

enabled true；base_threshold 10；density_factor 0.3（阈值随对话密度 10→7）；
same_direction_boost 0.05；opposite_decay 0.15；min_weight 0.2；max_intensity 2；
temperature 0.3；top_p 1；max_tokens 400；timeout 30000ms。

## 9. browser：网页观察

enabled false（可选能力）；mode deferred-only；search/visit 开关；
DuckDuckGo HTML 模板；域名黑白名单；并发页 1；每轮研究 1；
超时 15000ms；正文 12000 字符；节选 3000；进 prompt 4 条；缓存 30 分钟。

## 10. platform_gate：平台白名单

| 列表 | 字段 |
| --- | --- |
| bot_accounts | id（如 QQ 号或 webchat）、label、enabled |
| user_accounts | id、label、person_id、profile、relationship、enabled |
| group_chats | id、purpose、character_role、response_mode(mention-only/always)、context_limit、debounce_seconds、cooldown_seconds、enabled |
| ignore_self_messages | true |

空白名单 = 对应入口拒绝所有。Console 编辑会同步到已加入的参与者资料。

## 11. shared_story：共享剧本

auto_enroll_participants true；allow_cross_conversation_messages true；
share_participant_details **false**（涉及隐私，谨慎开启）；
max_cross_conversation_actions 1；participant_context_limit 6；
manager_ids []（空 = 所有已授权用户可用管理命令）。

## 12. logging

level info；verbosity standard；format layered；colors true；
color_theme dark；kaomoji true；log_script_preview / log_message_content
false（隐私）；preview_length 500。
