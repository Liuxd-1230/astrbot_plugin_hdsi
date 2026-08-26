# 管理员命令

主命令为 `hdsi`（对应原 Koishi 版 `interlude`）。危险操作使用"下一条消息确认"：
执行后按提示在 60 秒内回复 `y` / `yes` / `是` / `确认` 执行，其他内容视为取消。

## 权限模型

- 普通命令：任何已进入白名单的账号可用。
- 管理命令（标注 🔒）：需要 `shared_story.manager_ids` 包含该账号；
  留空时所有已授权账号皆可管理。

## 入门

### `hdsi init [主角名]`
为当前私聊创建故事；已存在时把当前账号加入共享主剧本。

### `hdsi status`
当前故事、状态、游标、参与者数、自动推进、主动联系、Agency/Alter 摘要。

### `hdsi pause` / `hdsi resume` 🔒
暂停/恢复自动处理，不删除记录。

## 剧本与上下文

### `hdsi timeline [条数=10]`
最近剧本时间线（仅本参与者的分支 + 全局事件）。

### `hdsi context`
场景引子/摘要、剧情弧线、continuity 快照、主角 overlay、Agency Window。

### `hdsi script [条数=20]` 🔒
原始 ScriptEntry 条目（含编号，可追溯事实来源）。

### `hdsi note <内容>` 🔒
写入管理员注记，不伪装成模型输出；纳入后续压缩连续性。

### `hdsi advance` 🔒
手动补写到现在并投递其中已发生的可见消息。游标距离现在不足
minimum_advance_minutes 且无到期计划时跳过。

## 记忆

### `hdsi memory [条数=10]`
主模型提取的耐久记忆（按重要度）。

### `hdsi facts [条数=20]` 🔒
长期事实列表（scope/重要度/置信度/未解决 + 编号）。

### `hdsi addfact <scope> <内容>` 🔒
手动添加高置信度长期事实。scope ∈ character/world/relationship/event/promise。

### `hdsi forgetfact <编号>` 🔒
将事实标记失效（可审计，不物理删除）。

## 意图

### `hdsi intents [条数=20]` 🔒
等待中的延迟回复、提醒、承诺、proactive-check 与剧情余波。

### `hdsi cancelintent <编号>` 🔒
取消指定等待中意图或延迟计划。

## Overlay

### `hdsi overlayclear <character|relationship|world|all>` 🔒
只清理指定演化层，保留 Canon、剧本和记忆；同时使相关提案与快照失效。
需 y 确认。

## 危险操作

### `hdsi reset` 🔒
删除**所有平台**的剧本、记忆、事实、意图与状态，按当前配置重建空白 Canon。
需 y 确认。执行前务必备份 interlude.db。

## WebUI 面板维护项

面板 Maintenance 页提供等价按钮：force advance / compact /
compact overlay / rebuild continuity / clear overlay(各层) / cancel intent /
reset story（双重确认）。Script Viewer 支持分页浏览全部真实条目。
