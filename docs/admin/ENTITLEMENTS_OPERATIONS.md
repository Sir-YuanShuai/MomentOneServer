# 管理后台：存储与权益运维

> 状态：第一、二批已实现（2026-08-09）
> 权限：读取需要 `admin.read`，写操作需要 `admin.operations`

## 能力范围

- 查看 Free / Plus / Pro 计划定义；
- 查看全站 used / reserved / effective quota 和超额账户数；
- 按用户查看当前套餐、有效权益和全部存储额度来源；
- 管理员变更用户套餐；
- 发放有到期时间或长期有效的额外存储额度；
- 撤销非套餐型额度；
- 按 PostgreSQL `assets` 元数据重新计算用户 used / reserved；
- 所有管理写操作要求 `Idempotency-Key`、`expectedRevision` 和审计记录。

## 套餐与额度规则

1. 套餐基线与额外 Grant 分开保存；有效额度等于所有有效 Grant 之和。
2. 套餐变更会撤销旧套餐来源，再发放目标套餐的计划权益、能力权益和基础存储 Grant。
3. 套餐降级后如果 `used + reserved > effectiveQuota`，账户进入 `overQuota`。
4. `overQuota` 不会自动删除对象；用户仍可读取、下载和清理，但不能创建新的 Upload Intent。
5. Upload Intent 先增加 `reserved_bytes`；Complete 后减去预留并按对象实际大小增加 `used_bytes`。
6. 单文件上限同时受服务端安全上限和当前套餐 `max_upload_bytes` 限制，取较小值。

## 管理 API

```text
GET   /v1/admin/plans
GET   /v1/admin/storage/summary
GET   /v1/admin/storage/accounts
GET   /v1/admin/users/{userId}/entitlements
PATCH /v1/admin/users/{userId}/plan
POST  /v1/admin/users/{userId}/storage-grants
POST  /v1/admin/storage-grants/{grantId}/revoke
POST  /v1/admin/users/{userId}/storage/reconcile
```

## 后续批次

本批次不提前创建 MCP/眼镜计量、账号合并和 Casdoor 订单表。下一批按依赖继续实现：

1. `quota_accounts` / `quota_usage_events` 和 Tool / Planner 计量；
2. `tools/list`、`agent_plan` 权益感知与设备/MCP 用量后台；
3. 身份 Link / Unlink / Merge；
4. Casdoor Product/Order/Payment/Subscription 投影和对账。


## 第二批：用量与动态订阅

- 管理员可创建、编辑、停用订阅计划；计划修改会同步当前订阅者的能力和基础存储额度；
- 管理概览提供今日活跃、月活、API 请求/错误、MCP/写 Tool/Planner/AI Token 趋势；
- 用户详情提供订阅、存储、近 30 日活跃天数和用量来源；
- 审计支持 eventType、actorType、allowed 和关键字组合过滤；
- 普通用户通过 `/v1/account` 查看自己的套餐、存储和调用额度。

## 账号页与注销

普通用户通过 `GET /v1/account` 查看自己的头像、套餐、存储和额度，不显示数据库、容器或服务依赖概念。头像和密码修改进入 Casdoor `/account`。

永久注销使用：

```text
POST /v1/account/delete-preview
POST /v1/account/delete-confirm
```

Confirm 要求输入“永久注销”、`Idempotency-Key` 和最近 5 分钟内重新验证的 Casdoor Token。
