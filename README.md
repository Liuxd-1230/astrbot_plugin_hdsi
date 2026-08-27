# HDS Interlude for AstrBot

> 聊天在幕前发生，生活在幕间继续。

HDS Interlude for AstrBot（`astrbot_plugin_hdsi`）是持续叙事运行时，而不是 Persona 插件。角色拥有独立于聊天窗口持续发展的生活；回复、沉默、延迟和主动联系都是同一段生活剧情的结果。

这是 MomoiCore/HDS-Interlude（Koishi 版 `0.1.3-beta1`）的完整 AstrBot 移植版 v1.0.0。所有已验证的语义——消息合并与抢占边界、延迟回复的重新裁决、Agency Window 的行动容量矩阵、Alter 情绪惯性、Overlay 证据门槛——全部保留，未做简化。

## 它解决什么问题

HDSI 以主角为中心维护持续剧情状态：角色拥有日程、关系、待办、情绪、配角和未完成事件。用户消息作为进入这段现实的一项**事件**参与角色当下的判断与后续生活：

```text
用户消息 / 图片
→ 写入真实 ScriptEntry
→ 短时合并（debounce）
→ Story 串行队列
→ 读取 Canon / Participant / recentScript / continuity
→ Facts / Consequences / Overlay / Alter / Agency
→ 计算真实时间差
→ 一次主叙事模型调用
→ 补写已经发生的生活 → 处理当前事件
→ 决定 seen / silent / immediate / delayed / proactive
→ 保存所有状态 → 最后才投递真实消息
```

绝对不是「收到消息 → 拼 Persona Prompt → 生成回复」。

## 核心能力

- **Multi-Character Registry (多角色注册表与独立世界线)**：支持在一个 AstrBot 实例中创建、管理并并行运行多个独立角色。每个角色拥有独立的 `character_id`、主故事（Story）、独立 Canon 设定、参与者列表、长期记忆（Memory）、事实库（Facts）、演化层（Overlay）、情绪（Alter）与意图账本（Intents）。
- **Authoritative Conversation Routing (权威会话路由)**：支持按平台（`platform_id`）、机器人账号（`self_id`）、会话类型（`friend` 私聊 / `group` 群聊 / `all` 全部）以及会话 ID 精准绑定角色。显式绑定的会话具有**绝对终止语义**（目标角色暂停或归档时直接停止响应，绝不会发生“身份跳人”串线切换至其他角色）；未绑定的会话默认走默认角色。
- **Decoupled Canon & Global Config (设定与全局配置彻底解耦)**：角色 Canon（人设、背景、世界观、关系、时区、文风）直接绑定至单角色故事，全局配置仅管理模型与引擎参数，新建角色默认模板绝不再反向污染现有角色设定。
- **Consistent Full-System Backup & Restore (一致性系统级备份与恢复)**：提供涵盖全部 13 张核心数据库表、完整剧本历史、所有角色设定及配置的一致性快照备份与事务级原子恢复。
- **Participant Lifecycle & Cascade Purge (参与者生命周期与级联清理)**：支持参与者状态调整（active/paused/archived）、单独清空未读计数、重置运行状态，以及彻底级联销毁私有记忆与历史数据（Cascade Purge）。
- **活跃场景 recentScript + continuitySnapshot**：近期上下文以原始剧本为主，低频快照负责跨时间衔接。
- **回复模式**：immediate / silent（看见不回）/ unseen / delayed（到期后重新裁决，绝不发送预写台词）。
- **首条回复提交边界**：首条气泡未提交前新输入作废旧生成并合并重写；提交后取消未发送的 `<sep/>` 后续气泡，未完成文字作为"被打断的念头"进入下一次写作。
- **Auto Advance**：无对话时按真实时间补写角色生活，绝不伪造用户消息；休息窗口自动切换低频节奏。
- **Agency Window**：日程负荷 × 隐私 × 设备可用性的容量矩阵 + willingness 门槛 + 最小间隔。
- **Alter System**：单维度氛围评分（-5..+5）、动态阈值、方向权重、后台侧端分析、自然衰减。
- **Overlay**：设定演化先提案后应用，证据门槛（独立回合数 × 跨日期 × 置信度），分层压缩归档，可单独清理。
- **Narrative Facts & Memory**：长期情景记忆与事实库带来源条目（可审计）、置信度排序、可选 Embedding 语义检索。
- **并发安全与调度防饥饿**：直接 JOIN 活跃角色与故事，彻底防止孤儿故事挤占调度配额；SQLite 全局写队列保证事务原子性。

## 安装

1. 在 AstrBot WebUI → 插件市场 → 搜索 `astrbot_plugin_hdsi` 安装；
   或从 GitHub 仓库克隆到 `data/plugins/astrbot_plugin_hdsi/`。
2. 在插件配置中绑定模型槽位：
   - `main_model`：留空 `inherit` 使用当前会话 Provider；或填 `provider_id:model`
   - 可选：`compaction_model` / `alter_model` / `embedding_model`
3. 在管理面板（WebUI 插件页 → HDS Interlude）完成：
   - **Canon**：主角名、背景、世界、地点、时区、文风
   - **平台白名单**：机器人账号 ID 与用户 ID（空白名单 = 关闭入口）
4. 在已授权账号的聊天中执行 `hdsi init 主角名` 创建主剧本。
5. 发送一条普通消息，观察日志中的 `[用户消息] 收到参与者私聊消息 → 模型调用 → 消息投递`。

详细步骤见 [docs/BEGINNER_GUIDE.md](docs/BEGINNER_GUIDE.md)。

## 管理命令

在聊天中直接使用（需要管理员权限的命令会校验白名单/manager_ids）：

| 命令 | 作用 |
| --- | --- |
| `hdsi init [主角名]` | 创建共享主剧本并把当前账号加入 |
| `hdsi status` | 当前故事、游标、参与者、Agency/Alter 状态 |
| `hdsi pause` / `hdsi resume` | 暂停 / 恢复自动处理 |
| `hdsi advance` | 手动补写到现在 |
| `hdsi timeline [N]` | 最近剧本时间线 |
| `hdsi context` | 场景摘要、弧线、overlay、continuity |
| `hdsi script [N]` | 原始剧本条目（管理员） |
| `hdsi note <内容>` | 写入管理员注记 |
| `hdsi memory [N]` | 耐久记忆 |
| `hdsi facts [N]` / `addfact` / `forgetfact` | 长期事实管理 |
| `hdsi intents [N]` / `cancelintent <id>` | 意图账本管理 |
| `hdsi overlayclear <target>` | 清理 overlay（y/n 确认） |
| `hdsi reset` | 重置全部剧情数据（y/n 确认） |

完整说明见 [docs/COMMANDS.md](docs/COMMANDS.md)。

## WebUI 管理面板

AstrBot WebUI → 插件页 → HDS Interlude：

Overview / Canon / Participants / Models / Runtime / Memory / Intents /
Overlay·Alter·Agency / Script Viewer / Maintenance。

支持 force advance、手动压缩、重建 continuity、清理各层 overlay、取消意图、
重置故事等维护操作；危险操作均需二次确认。

## 从 Koishi 版迁移

支持两层迁移（详见 [docs/MIGRATION.md](docs/MIGRATION.md)）：

1. **配置导入**：管理面板粘贴原 HDS-Interlude Console 配置 JSON，
   自动映射 storyDefaults / prompts / runtime / memory / alterSystem /
   agency / onebot 白名单 / browser 等全部字段。
2. **数据库导入**：提供 Koishi SQLite 只读读取工具，11 张表逐行迁移，
   保留 id、时间戳与 participant 归属，长跑故事无缝续写。

## 文档导航

- 新手部署教程：[docs/BEGINNER_GUIDE.md](docs/BEGINNER_GUIDE.md)
- 逐项配置说明：[docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- 管理员命令：[docs/COMMANDS.md](docs/COMMANDS.md)
- 当前架构：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 迁移差异记录：[MIGRATION_NOTES.md](MIGRATION_NOTES.md)
- Koishi 迁移指南：[docs/MIGRATION.md](docs/MIGRATION.md)
- Alter System：[docs/ALTER_SYSTEM.md](docs/ALTER_SYSTEM.md)
- Agency Window：[docs/AGENCY_WINDOW.md](docs/AGENCY_WINDOW.md)
- 版本记录：[docs/CHANGELOG.md](docs/CHANGELOG.md)

## 使用边界

- HDSI 依赖模型的写作与结构化输出能力。较小或不稳定的模型更容易出现格式失败、过度重复或关系跳跃。
- 自动推进基于已记录状态补写角色生活，适合叙事陪伴与角色互动；医疗、紧急救助、法律及其他高风险场景请使用相应的专业服务。
- 主动联系需显式开启 `runtime.allow_proactive_messages`，并始终受白名单、willingness 阈值、最小间隔与 Agency 容量矩阵约束。
- 清空数据库、删除范围剧本和清除 Overlay 均可能不可逆；执行前请先备份 `data/plugin_data/astrbot_plugin_hdsi/interlude.db`。

## 开发与验证

```bash
python -m pytest tests/ -q          # 40+ 场景测试
python -m tests.simulation --days 7   # 7 天模拟
python -m tests.simulation --days 30  # 30 天模拟
python -m tests.simulation --days 90  # 90 天模拟
```

## License

AGPL-3.0（继承原项目）。基于 MomoiCore/HDS-Interlude 移植。
