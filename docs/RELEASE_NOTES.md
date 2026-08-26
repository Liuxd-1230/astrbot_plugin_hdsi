# Release Notes — astrbot_plugin_hdsi v1.0.0

发布日期：2026-08-26

## 一句话

HDS Interlude for AstrBot 是持续叙事运行时，而不是 Persona 插件。角色拥有
独立于聊天窗口持续发展的生活；回复、沉默、延迟和主动联系都是同一段生活剧情
的结果。

## 亮点

- **完整移植**：Koishi 版 0.1.3-beta1 的全部能力——无 MVP 裁剪。消息合并与
  抢占边界、延迟回复重新裁决、Agency Window、Alter System、Overlay 证据门槛
  逐一对应，语义差异全部记录在 MIGRATION_NOTES.md。
- **零 Key 配置**：所有模型调用复用 AstrBot Provider；四个槽位即可绑定主叙事/
  压缩/Alter/Embedding，支持 inherit 与故障切换链。
- **隐私优先**：多参与者共享一个主剧本时，原始私聊默认不跨分支泄漏
  （有专门测试守护）；主动联系必须由 Agency 容量矩阵放行。
- **重启无忧**：pending intent、自动推进时钟、分段消息全部从 SQLite 恢复；
  过期模型结果永不落库。
- **管理面板**：WebUI 插件页覆盖总览、Canon、参与者、模型、运行时、记忆、
  意图、Overlay·Alter·Agency、剧本查看与维护操作；危险操作二次确认。
- **平滑迁移**：Koishi 配置一键导入 + 数据库逐行导入，长跑故事无缝续写。

## 验证

- 43 项自动化测试（40 个验收场景）
- 7 / 30 / 90 天模拟：人格漂移、循环剧情、记忆膨胀、调度重复等全部受检通过
- 本地 Docker AstrBot v4.27.4 真实对话端到端验证

## 已知限制

- 网页观察使用 httpx 抓取静态 HTML，不含浏览器渲染（原版为 Puppeteer）。
- 动态图（GIF/WebP/APNG）不再抽帧，原图直接进入视觉模型。
- 多进程部署仍需外部锁（与上游一致）。

## 升级 / 迁移

从 Koishi HDS-Interlude 迁移见 `docs/MIGRATION.md`；
配置字段说明见 `docs/CONFIGURATION.md`。

## License

AGPL-3.0。基于 MomoiCore/HDS-Interlude 移植，感谢原项目的架构与验证工作。
