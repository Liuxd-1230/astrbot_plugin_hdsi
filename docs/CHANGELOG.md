# 版本记录

## v1.0.0（2026-08-26）

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
