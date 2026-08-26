# Agency Window

适用版本：`v1.0.0`（语义与 Koishi `0.1.3-beta1` 一致）

## 定位

Agency Window 只负责外部联系行动的现实容量：日程负荷、隐私和设备可用性。
它不描述情绪、不计算关系阶段或联系风格，也不读取 Alter 数值。

主动联系遵循：

```text
先写主角自己的生活
→ 生活产生真实联系理由
→ 检查日程、隐私、设备、意愿和安全间隔
→ 立即联系 / proactive-check 稍后重查 / 自然放下
```

**用户长时间沉默本身不能成为联系理由。**

## 状态

`story.state.agency_window` 保存：

- activity_load: free | occupied | overloaded
- privacy: private | shared | public
- device_access: available | limited | unavailable
- next_opportunity_at / valid_until / basis / source_entry_ids / updated_at

状态必须引用真实剧本条目，并被 maxWindowMinutes 限制。过期状态不会进入主模型。

## 联系候选

proactiveContact 支持：

- 来源 origin：生活事件 life-event / 承诺 promise / 实际安排 practical-update /
  关系后续 relationship-follow-up
- 内容敏感度 disclosure：ordinary | personal
- 目标参与者（必须通过当前白名单）
- 具体 motive；真实来源条目 sourceEntryIds（模型伪造的 id 直接拒绝）
- willingness 0..1
- outcome：send-now | recheck-later | let-go
- notBefore / expiresAt（有界）

当前新剧本产生的理由由宿主自动绑定到该剧本条目（fallback source）。

## 容量矩阵

| 条件 | 结果 |
| --- | --- |
| 设备 unavailable / limited | 不立即发送 |
| 日程 overloaded | 不立即发送 |
| personal 内容且环境非 private | 不立即发送 |
| occupied 且非 promise/practical-update | 不立即发送 |
| 普通联系距上次角色发言 < minimumProactiveIntervalMinutes | 不立即发送（promise 绕过） |
| willingness < runtime.proactiveWillingnessThreshold | 不发送 |

同一 participantId + origin + sourceEntryIds 的 pending 候选自动去重（指纹）。

## 延后重查

recheck-later 复用 interlude_intent 创建 proactive-check，只保存 motive、
来源和约束，**绝不保存预写消息**。到期后单独成批处理，重新读取当前生活和
Agency Window：

- send-now：经 interaction.reply.immediate 发送
- recheck-later：创建新的未来检查
- let-go 或候选过期：不发送并结束

## 与其它系统的边界

- Alter：只影响剧情氛围和表达，不参与联系容量。
- Memory/Overlay：提供已发生事实和稳定变化，不直接触发发送。
- Active consequence / promise：可以成为生活来源，但最终仍通过 Agency 判断。
- 原始私聊：后台不把其它参与者的原始对话加入请求；仅提供受控的姓名、资料与关系摘要。
