# AI / Agent 通知接入指南

> 状态：Current | 更新日期：2026-08-12

## 原则

AI 不直接创建 `notifications` 记录，也不直接调用 Web Push。AI 创建业务事实，Server 再统一处理
幂等、时区、状态复核、免打扰、锁屏隐私和多终端投递。

- 用户要求在指定时间提醒时，创建 `Reminder`；
- 新登录、MCP 授权和设备绑定由相应业务服务产生安全通知；
- 普通数据写入不自动打扰用户，除非该业务具有明确的提醒规则。

## MCP Agent 创建提醒

使用 MCP 工具 `reminder_create`，需要 `moments.write` Scope：

推荐让 Server 做确定性换算，不要由模型手算 UTC offset。时间输入三选一：

- `afterMinutes`：用户说“30 分钟后提醒我”；以 Server 接收工具调用的时刻为基准；
- `localDateTime` + `timezone`：用户说“明天上午 9 点”且日期、IANA 时区已经明确；
- `remindAt`：上游已经持有可靠的带 offset RFC3339 绝对时间。

例如用户在上海明确要求 2026-08-14 上午 9 点提醒：

```json
{
  "title": "提交报销单",
  "localDateTime": "2026-08-14T09:00:00",
  "timezone": "Asia/Shanghai",
  "dueAt": "2026-08-14T18:00:00+08:00",
  "note": "整理发票后提交",
  "scene": "general",
  "idempotencyKey": "agent-run-id:reminder:expense"
}
```

旧客户端仍可直接传绝对时间：

```json
{
  "title": "提交报销单",
  "remindAt": "2026-08-13T09:00:00+08:00",
  "dueAt": "2026-08-13T18:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "note": "整理发票后提交",
  "scene": "general",
  "sourceMomentId": null,
  "idempotencyKey": "agent-run-id:reminder:expense"
}
```

三个时间输入必须且只能提供一个。`remindAt` 必须带 offset；`localDateTime` 不带
offset，由 `timezone` 的 IANA 规则换算；`timezone` 省略时使用账号时区。夏令时空档或
重叠时间会被 Server 拒绝，Agent 必须请用户换一个明确时间。`dueAt` 是可选截止时间，
不能早于提醒时间。日期、时间或时区不明确时必须先询问用户；相同业务意图重试时复用
同一个 `idempotencyKey`。

## 三类时间的字段所有权

| 时间 | 字段 | 写入方 | 规则 |
|---|---|---|---|
| 提醒触发时间 | `remindAt` | Server 归一化 | Agent 提交三选一输入，Server 返回最终绝对时刻 |
| 记录创建时间 | `createdAt` | 仅 Server | 记录首次进入云端的时间，Agent 不得提交或覆盖 |
| 事件发生时间 | `occurredAt` | 用户语义 + Server 归一化 | 未提时间时取 Server 接收时刻；补录使用 `occurredLocalDateTime + timezone`；绝对时间必须带 offset |

`dueAt` 是事项截止时间，不是 `createdAt`，也不是默认提醒时间；新 MCP 客户端不得混用。

## REST 客户端创建提醒

```http
POST /v1/reminders
Authorization: Bearer <Casdoor access token>
Idempotency-Key: <stable operation key>
Content-Type: application/json
```

```json
{
  "title": "提交报销单",
  "note": "整理发票后提交",
  "scene": "general",
  "remindAt": "2026-08-13T09:00:00+08:00",
  "dueAt": "2026-08-13T18:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

修改、完成、取消和删除分别使用：

```text
PATCH /v1/reminders/{reminderId}
POST  /v1/reminders/{reminderId}/complete
POST  /v1/reminders/{reminderId}/cancel
POST  /v1/reminders/{reminderId}/snooze
POST  /v1/reminders/{reminderId}/delete-preview
POST  /v1/reminders/delete-confirm
```

修改、稍后提醒和状态变更必须携带 `expectedRevision`；删除必须走 Preview + Confirm。稍后提醒只更新
`remindAt`，不会改动业务截止时间 `dueAt`。

## 服务端业务代码

- 定时事项调用 `ReminderService` 并写 Outbox；
- 账号安全事件调用 `enqueue_security_notification(...)`，使用稳定 `event_key` 去重；
- 普通产品状态默认只创建站内通知；若需要系统 Push，必须先在 `policy.py` 登记类别策略；
- API Route 不得直接操作通知表，不接受客户端指定任意 `userId`、外部 URL 或 Push endpoint。

## 渠道策略

| 场景 | 站内通知 | 系统 Push | 免打扰 |
|---|---|---|---|
| Reminder 到期 | 是 | 是，用户可关闭 | 遵守 |
| 习惯到点 | 是 | 逐习惯启用后发送 | 遵守 |
| 新登录、MCP/设备授权 | 是 | 是，用户可关闭安全通知 | 不延迟 |
| 普通产品更新、导入结果 | 是 | 默认否 | 不适用 |
| 重要系统公告 | 是 | 经审批且用户允许时发送 | 遵守 |

Push 失败不能删除站内通知。AI 可以告诉用户“提醒已创建；已授权系统通知的设备也会收到 Push”，
但不能承诺某个设备一定展示通知，也不能在接口失败时声称提醒已经创建。
