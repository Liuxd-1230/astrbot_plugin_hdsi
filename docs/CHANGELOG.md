# 版本记录

## v1.0.0（2026-08-26）

### 独立审计修复（发布阻断项全数解决）

- **P0-1 投递边界（Outbox）**：immediate 回复改为"staged outbound intent →
  真实发送 → 成功才写入 character-message 与 lastCharacterMessageAt；
  失败/打断取消 staged 且不产生已说出口条目"。群聊同路径修复。
  首条回复 commit 边界移至传输开始时刻。重启后未完成的 outbound-message
  按 split-message 同路径续投。
- **P0-2 批量写事务**：execute_many 显式 BEGIN/COMMIT + 失败 ROLLBACK，
  单条 execute 失败同样回滚；彻底消除"半途失败被后续无关提交诈尸"。
  故障注入钩子覆盖批量与 insert_returning_id 路径。
- **P0-3 生成 ID 竞争**：新增 insert_returning_id（INSERT+rowid 同一写队列任务），
  append_entry / web_observation / state_patch 三处不再跨 Story 取错 ID。
- **P0-4 canonical 归档作用域**：归档守卫仅在显式 (platform_id, self_id)
  作用域内执行；无作用域调用非破坏性。启动恢复遍历全部活动故事；
  WebUI 改用 latest_active_story 视图。
- **P0-5 Continuity 隐私边界**：拆分 Global（仅由无人推进刷新，天然无私聊）
  与 Participant 私有快照；participant 刷新只写本分支；prompt 经
  select_continuity_snapshot 只合并 global+自身。隐私测试升级为整 payload 扫描。

### P1/P2 修复

- Vision 适配器拆分：image_loader（http/path/base64→data_uri，4MB 上界、
  MIME 校验）与 browser_fetch 彻底分离；网页观察改手动逐跳重定向，
  每跳复检 URL 安全并做 DNS 解析内网拦截（SSRF 缺口闭合）。
- invalidate_buffered_narratives 改同步函数：clear/purge 同一事件循环步生效，
  消除 coroutine never awaited 警告及其隐患。
- 叙事调用透传 top_p/max_tokens，超时语义修正（0=交给 Provider 自身重试策略）；
  inherit 解析三级回退（会话→全局默认→任一可用 Provider）。
- JSON repair 真正接入 decide_raw：malformed 时携带修复指令重试一次再失败。
- WebUI：table() 默认全转义（显式 {html} 才放行），Pending Intent 等 XSS 点
  封死；表单保存经 reconcile 还原数组结构，Runtime 页可安全 round-trip。
- 管理员默认收紧：manager_ids 为空时仅 AstrBot admin 可用危险命令。
- 二次确认实现真正的 60 秒过期。
- requirements 补齐 aiosqlite/httpx；metadata 版本纯 semver、
  astrbot_version 下限收敛到实测的 >=4.27.0。
- 模拟器 proactive 密度检查从硬编码 True 改为真实统计
  （90 天 artifact: tests/artifacts/sim-90d.json，165 提议/3478 回合）。

AstrBot 移植版首个完整版本。以 MomoiCore/HDS-Interlude `0.1.3-beta1`
的行为与架构为唯一语义基准完成等价移植，未做 MVP 裁剪。

### 运行时（全部保留自原版）

- Canonical Story / Character·World·Location·Supporting Cast
- Participant 独立资料与关系；多参与者共享主剧本；隐私隔离（自动化测试覆盖）
- Active Scene + recentScript + continuitySnapshot（15 回合低频刷新）
- Narrative Facts（来源可审计）+ Active Consequences + 长期记忆
- Scene / Arc 压缩；Overlay 证据门槛与分层压缩；State Patch 提案制
- Intent：immediate / silent / unseen / delayed；延迟到期重新裁决，
  绝不发送预写台词；延迟取消；first-reply commit boundary；
  `<sep/>` 多气泡与中途打断（未发送文字转 interruptedOutgoingDrafts）
- Auto Advance（真实时间差 → 四档叙事尺度；休息窗口降频）
- Conversation Follow-up（10/20 分钟）；Reminder
- Agency Window：容量矩阵、willingness、最小间隔、proactive-check 重查、去重
- Alter System：动态阈值、方向权重、后台侧端分析、自然衰减
- Vision（多模态主模型）；Web Observation（有界只读 + 安全边界）
- Provider failover（冷却/轮换/重试）；SQLite 全局写队列与瞬时错误重试
- Story recovery：重启后从数据库恢复 pending 任务，不重复发送

### AstrBot 集成

- `should_call_llm(False)` + `stop_event()` 接管被管理会话
- 模型槽位 inherit / provider_id:model / failover 链；不保存 API Key
- `hdsi` 命令组（init/status/pause/resume/advance/timeline/context/script/
  note/memory/facts/addfact/forgetfact/intents/cancelintent/overlayclear/reset）
- WebUI Plugin Page 管理面板（Overview/Canon/Participants/Models/Runtime/
  Memory/Facts/Intents/Overlay·Alter·Agency/Script Viewer/Maintenance）
- Koishi 配置导入 + SQLite 数据导入工具

### 质量

- 43 项 pytest：40 个验收场景全覆盖（debounce、抢占、提交边界、sep 打断、
  三种回复模式、意图重估、自动推进、长离线、休息窗、follow-up、提醒、
  主动候选、Agency 矩阵、重查、意愿门槛、重载恢复、到期恢复、防重复投递、
  游标单调性、参与者隔离、共享剧本、隐私不泄漏、Overlay 门槛/清理、Alter
  累积/衰减、压缩保留、事实溯源、Embedding 关闭回退、浏览器关闭回退、
  malformed JSON、Provider 超时→重试、插件重载、调度去重、SQLite 故障降级、
  同故事串行、跨故事并行）
- 7 / 30 / 90 天模拟通过：人格漂移、循环剧情、重复台词、记忆膨胀、
  Overlay 过度变化、角色独立生活、主动密度、回复模式分布、intent 完成、
  调度重复触发全部受检
- 本地 Docker AstrBot v4.27.4 实测：webchat 真实对话端到端通过
  （拦截默认 LLM → 叙事模型 → 时区正确的剧本 → immediate 投递 → 多轮连续性）

### 与原版的差异

见 MIGRATION_NOTES.md。要点：canonical 故事按机器人身份隔离；白名单推广为
platform_gate；模型接入改走 AstrBot Provider；网页观察以 httpx 取代 Puppeteer。

## 上游 0.1.3-beta1（2025）

Agency Window 预发布版（详见上游 CHANGELOG）：新增轻量行动窗口、
proactiveContact 三态结果、proactive-check 复用 intent、来源验证与去重、
参与者摘要修复、Agency/Alter 隔离。
