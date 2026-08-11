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

```json
{
  "title": "提交报销单",
  "dueAt": "2026-08-13T09:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "note": "整理发票后提交",
  "scene": "general",
  "sourceMomentId": null,
  "idempotencyKey": "agent-run-id:reminder:expense"
}
```

`dueAt` 必须是带时区的未来时间。日期、时间或时区不明确时必须先询问用户；相同业务意图重试时
复用同一个 `idempotencyKey`。

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
  "dueAt": "2026-08-13T09:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

修改、完成、取消和删除分别使用：

```text
PATCH /v1/reminders/{reminderId}
POST  /v1/reminders/{reminderId}/complete
POST  /v1/reminders/{reminderId}/cancel
POST  /v1/reminders/{reminderId}/delete-preview
POST  /v1/reminders/delete-confirm
```

修改必须携带 `expectedRevision`；删除必须走 Preview + Confirm。

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
