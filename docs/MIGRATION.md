# 从 Koishi 版 HDS-Interlude 迁移

支持两层迁移，可独立使用也可组合：

## 一、配置导入（Console 配置 → 本插件）

1. 打开原 Koishi 实例的 Console，把 `hds-interlude` 插件配置导出为 JSON
   （或手工复制整段 YAML 转 JSON）。
2. AstrBot WebUI → 插件页 HDS Interlude → Maintenance → 「Koishi 配置导入」，
   粘贴 JSON 并提交；或直接调用 Web API：

```bash
curl -X POST "$ASTRBOT/api/plug/astrbot_plugin_hdsi/hdsi/migrate_config" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"koishi_config": { ...原始配置... }}'
```

### 字段映射

| 原字段 | 目标 | 说明 |
| --- | --- | --- |
| storyDefaults.* | story_defaults.* | characterName/characterProfile/userProfile/relationship/world/supportingCast/location/style/timezone 全部无损 |
| model.mainPrompt / stylePrompt / fixedPrompt / formatPrompt | prompts.* | 无损 |
| model.compaction.*Prompt | prompts.compaction_* | 无损 |
| model.vision.enabled / embedding.* | models.vision_enabled 等 | Embedding 的 provider/endpoint/key 不迁移（由 AstrBot Provider 承担） |
| runtime.* | runtime.* | 全字段映射（含 restWindows） |
| memory.* / agency.* / sharedStory.* / browser.* | 同名节 | camelCase → snake_case |
| alterSystem.providerId+model / modelId | alter_system.model_slot | 合并为槽位表达式 |
| onebot.botAccounts / userAccounts / groupChats | platform_gate.* | 白名单行的 profile/relationship/personId 保留 |
| sharedStory.managerAccounts | shared_story.manager_ids | 保留 |

不迁移：providers/models 目录与 API Key（AstrBot Provider 体系接管）、
logging.colorTheme（按新宿主重选）。

## 二、数据库导入（长跑故事无缝续写）

前提：能拿到原 Koishi 实例的 SQLite 文件（如 `koishi.data.db` 或对应
database 驱动的文件）。在部署机执行：

```python
# scripts/import_koishi_db.py 亦可参考手写
import asyncio
from hdsi.database.connection import Database
from hdsi.migration import import_koishi_database

async def main():
    db = Database("data/plugin_data/astrbot_plugin_hdsi/interlude.db")
    await db.connect()
    counts = import_koishi_database("/path/to/koishi.db", db, overwrite=False)
    print(counts)
    await db.close()

asyncio.run(main())
```

行为说明：

- 11 张表逐行读取；列名 camelCase → snake_case；JSON 列解析后入库。
- 时间戳全部保留（ISO-8601 UTC），游标、场景检查点、意图到期时间不变。
- participant_id 归属原样保留 → **隐私边界跨库保持**：A 的私聊不会因此
  出现在 B 的上下文里。
- 旧版 per-account 故事 id（`platform:selfId:userId`）自动改写为
  `character:platform:selfId`，并在 state.migratedFromStoryId 记录原 id。
- 默认跳过已存在的 id（幂等）；`overwrite=True` 覆盖。
- 导入后重启 AstrBot 插件：pending intent 与自动推进时钟会按第 19/20 号
  测试验证过的恢复路径重新调度。

## 三、验证清单

- [ ] 管理面板 Overview 显示主角与游标（时间应为原故事最后时间）
- [ ] `hdsi timeline 20` 能看到旧剧情
- [ ] `hdsi context` 场景摘要/事实完整
- [ ] 发送一条消息，模型引用了导入前的记忆
- [ ] 白名单中的老用户自动回到原关系分支

## 注意

- 迁移前备份两边数据库。
- Koishi sql.js（纯 WASM）实例导出的可能是内存库——先在 Koishi 侧确保
  落盘为标准 SQLite 文件。
- 若原库表名带前缀差异（如自定义 minato 前缀），先改名再导入。
