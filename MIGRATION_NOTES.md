# MIGRATION_NOTES — Koishi 0.1.3-beta1 → AstrBot v1.0.0

本文件记录移植过程中所有语义差异。除下列条目外，行为、阈值、数据生命周期、
Prompt 语义与边界条件均与原版保持一致；名称按 Python 惯例改为 snake_case。

## 1. 平台身份模型

| Koishi | AstrBot |
| --- | --- |
| `platform`（适配器类型）+ `selfId` + `userId` + `channelId` | `platform_id`（实例 id）+ `self_id` + `sender_id` + `umo` |
| story id: `character:{platform}:{selfId}` | 相同：`character:{platform_id}:{self_id}` |
| participant 端点匹配 (platform,selfId,userId) | (platform_id, self_id, session_key)，另以 umo 兜底 |

- 参与者新增 `umo` 字段作为投递目标（AstrBot 统一会话串）。
- OneBot 平台别名归一化逻辑保留（private:/user:/onebot:/napcat:/qq: 前缀剥离）。
- **差异**：OneBot/NapCat 白名单推广为通用 `platform_gate`（bot_accounts /
  user_accounts / group_chats），字段含义与原 onebot 配置一一对应；
  非 OneBot 平台在原版直接放行，本版统一走白名单（空白名单 = 拒绝，
  与原版"启用过滤时空表拒绝所有"的显式白名单哲学一致）。

## 2. Canonical Story 范围

- 原版：整个 Koishi 进程只保留一个活动主剧本（跨平台归档其他故事）。
- 本版：canonical 守卫按 `(platform_id, self_id)` 即机器人身份隔离。
  同一身份仍只允许一个活动剧本（多余的活动行自动归档）；
  不同机器人身份的故事并行运行，互不干扰。
- 动机：AstrBot 单进程常驻多个平台实例；全局唯一会把多账号部署锁死成一个故事。

## 3. 模型接入

| 原版 | 本版 |
| --- | --- |
| 自建 ProviderConfig（endpoint/apiKey/model/temperature…）+ failover | 复用 AstrBot Provider；槽位表达式 `inherit` / `provider_id` / `provider_id:model`，逗号分隔为故障切换链 |
| providers/models 两级目录 | 直接引用 AstrBot 已配置的 Provider |
| OpenAI 兼容 HTTP + response_format json-object | `provider.text_chat()` + JSON 提取/修复层 |
| Embedding 独立 endpoint | AstrBot EmbeddingProvider（`get_embedding`） |

- 不再保存任何 API Key。
- 冷却、priority/round-robin、每服务商尝试次数语义保留（ProviderRouter）。
- 主叙事默认温度/top_p/max_tokens/timeout 数值不变。

## 4. 数据库

- minato/Koishi 表 → 插件独立 SQLite（aiosqlite, WAL）：
  `data/plugin_data/astrbot_plugin_hdsi/interlude.db`
- 11 张表名与列名保持一致（列名 snake_case）；JSON 列存 TEXT。
- 新增 `hdsi_meta.schema_version` 迁移机制。
- 全局写队列 + 瞬时错误退避重试 + "INSERT 后查重"防重复语义保留。
- 日期规范化：minato 的 Date 对象 ↔ SQLite ISO 文本，读取统一 parse_date。
- 清空数据库失败时的"逻辑清空降级"保留。

## 5. 定时器与调度

- `ctx.setTimeout/setInterval` → asyncio task；到期唤醒计时器语义保留
  （最早唤醒优先、split-message 专用直达投递、忙时 1 秒重试）。
- **差异**：flush 遇 story 忙时的重试等待由固定 0.25s 改为可配置
  `story_busy_retry_delay_seconds`（默认 0.25 不变），供虚拟时钟测试环境收缩。
- sweep 跳过路径新增一次 `asyncio.sleep(0)` 协作让步：保证仅以 sweep 为驱动
  的宿主不会饿死 debounce 任务（对真实部署无可见影响）。

## 6. 群聊

- OneBot 群白名单 → platform_gate.group_chats；mention-only / always /
  context_limit / debounce / cooldown 语义保留。
- @检测从消息文本/CQ码改为消息链 At 组件比对 self_id。
- 群回复经 `context.send_message(umo)` 投递。

## 7. 图片 / Vision

- 原生识图：消息链 Image 组件 → base64 → `text_chat(image_urls=...)`。
- 图片源提取保留"只信任本轮事件"的边界；QQ CDN 受信域名列表不再写死
  （AstrBot MediaResolver 已处理下载），任意 http(s) URL 经有界下载。
- 动态图抽帧（Puppeteer）未移植：AstrBot 无内置 Puppeteer；动图原样传入，
  由模型自行取代表观感（记录于 CHANGELOG）。

## 8. Web 观察（Browser）

- koishi-plugin-puppeteer → httpx 只读抓取 + HTML 可见文本抽取。
- 安全边界完整保留：协议白名单、localhost/私网拒绝、用户名密码拒绝、
  域名黑白名单、超时、正文/节选字符上限、缓存 TTL、每轮观察数上限、
  失败/拦截也落库为观察（不阻塞叙事）。
- 差异：无浏览器渲染（JS 页面可能拿不到完整内容）；waitUntil 配置项移除。

## 9. 命令

- `interlude.*` → `hdsi ...`（AstrBot command_group）。
- y/n 确认改为"下一条消息确认"模式：危险操作登记待确认动作，
  下一条来自同一会话的消息为 y/yes/是/确认 时执行，其余视为取消。
- `purge.platform/range`、`database.clear`、`script.note`、`memory.*`、
  `overlay.*` 语义一一对应；`setup <json>` 由管理面板配置编辑取代
  （等价能力，面板更安全）。

## 10. 日志

- layered 彩色任务时间线完整移植（颜文字/树形字段/明暗主题/动作识别）。
- 输出目标从 Koishi Logger 改为 AstrBot per-plugin logger。
- verbosity（summary/standard/diagnostic）过滤逻辑不变。

## 11. 其他

- `meta.ts HDS_INTERLUDE_VERSION` → hdsi/__init__.py __version__。
- continuity.ts / relationship.ts 在原仓库中已是被回滚架构的死代码
  （未 import），未移植；其中 diceCoefficient 类重复检测属于回滚前路线。
- Koishi Service 生命周期 → Star.initialize()/terminate()；
  terminate 取消全部后台任务与缓冲计时器，initialize 从数据库恢复。
- 插件页路由需带插件名前缀注册（bridge full-match 要求），
  同时注册无前缀形式兼容 `/api/plug/<name>/<route>` 直连。

## 12. 测试对应关系

原仓库 7 个测试文件中的纯函数断言全部移植并扩展：
agency.test.ts → tests 内 agency 断言（容量矩阵逐条对应）、
alter-system.test.ts → test_overlay_alter_memory.py::28/29、
time-context / narrative-prompts / configuration / conversation-interruption /
logging 断言并入相应模块。40 个验收场景 + 三档长程模拟见 tests/。
