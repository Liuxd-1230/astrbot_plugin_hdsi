# 架构

适用版本：`v1.0.0`（移植自 Koishi `0.1.3-beta1`）

## 核心原则

HDS Interlude 使用自然语言连续性路线：近期上下文以原始剧本为主，低频
continuitySnapshot 和长期事实负责跨时间衔接。用户消息是进入主角生活的真实
外部事件。主模型在一个回合内补写从故事游标到现在已经发生的生活，并决定看见、
沉默、立即回复、延迟回复或产生其它受限行动。自动推进没有当前用户事件，
不能伪造新消息。

收到被 HDSI 管理的消息后，插件调用 `event.should_call_llm(False)` 并
`stop_event()` 禁止 AstrBot 默认 LLM 回复链，然后由 HDSI 自己完成完整叙事运行时。

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `main.py` | AstrBot Star 入口：事件桥接、Provider 适配、投递、命令、WebUI API |
| `hdsi/types.py` | 跨模块数据协议（Pydantic），读时规范化（camelCase/snake_case 双兼容） |
| `hdsi/service.py` | 故事与参与者、串行队列、持久化、调度、投递、Alter、Agency |
| `hdsi/narrator.py` | Provider 路由：槽位解析、冷却、failover、round-robin |
| `hdsi/prompt_builder.py` | 系统提示词合约 + 主叙事/压缩/Overlay/Alter payload |
| `hdsi/normalize.py` | 模型输出规范化：先规范化再写库，不信任模型时间与结构 |
| `hdsi/json_repair.py` | JSON 提取（原文→代码栅栏→平衡扫描）与有限修复 |
| `hdsi/database/` | aiosqlite 连接、11 张表 DDL、迁移版本、全局写队列 |
| `hdsi/concurrency.py` | per-story 串行队列、写队列、浏览器并发槽 |
| `hdsi/scheduler.py` | 自动推进间隔、休息窗口、对话后续的纯函数 |
| `hdsi/alter.py` | Alter 状态机：动态阈值、权重生命周期（纯函数） |
| `hdsi/agency.py` | Agency Window：容量矩阵、候选验证、去重、重查时间（纯函数） |
| `hdsi/memory.py` / intent / participants / delivery / browser | 记忆检索、意图分组、隐私过滤、分段与打字模拟、只读网页观察 |
| `pages/dashboard/` | WebUI 插件页 SPA（经 page-bridge 调用注册的 Web API） |

## 主叙事数据流

1. 私聊或群聊事件先写入 `interlude_script_entry`（事实来源）。
2. 同一参与者的连续消息短时合并；过期模型结果不落库。
3. 服务读取近期原始剧本、continuity、参与者状态、事实、意图、Overlay、
   网页观察和当前事件。
4. 主模型返回 `script`、互动决定和允许的结构化副产物；Alter 开启时同时返回
   本轮净变化分数。
5. 服务规范化所有字段，保存剧本和状态，再执行受权限和时间约束的消息投递。
6. 后台 sweep 处理自动生活推进、到期意图、记忆压缩、Overlay 压缩、
   向量补齐；到期唤醒计时器让 `<sep/>` 分段与延迟回复不必等下一个 sweep。

## 连续性分层

| 层 | 作用 |
| --- | --- |
| Canon | 主角、世界、地点、配角、初始关系和风格的起点 |
| recentScript | 最近原始剧本和真实收发记录（带 ownership 标签） |
| continuitySnapshot | 每 15 次成功叙事或首次自动推进时刷新 |
| 长期事实 | 可检索的承诺、事件、世界与关系事实（带来源条目） |
| active consequence | 已发生事件的短期余波，有界生命周期 |
| Overlay | 达到证据门槛后的稳定演化（提案→应用→分层压缩） |
| Alter System | 当前氛围的临时惯性；不替代以上任何一层 |
| Agency Window | 日程×隐私×设备的外部行动容量；只约束联系行动 |

## 并发和持久化边界

- 同一故事的消息、到期意图、自动推进和压缩在单进程内经 per-story asyncio
  队列严格串行；不同身份的故事并行。
- SQLite 写入走全局写队列（单写者）；读写均有瞬时错误退避重试；
  INSERT 失败前查重防止重复意图/条目。
- LLM 网络请求在故事锁外执行；新输入可同步标记打断并作废未提交请求。
- 多实例部署需外部锁（与原版一致，不做分布式）。

## 与 AstrBot 的边界

HDSI 负责：叙事状态机、提示词、并发、持久化、调度、安全边界。
AstrBot 负责：平台适配、消息收发、Provider 管理、插件生命周期、仪表盘。
插件关闭默认 LLM 链路，但自身通过 Provider 完成全部模型调用。
