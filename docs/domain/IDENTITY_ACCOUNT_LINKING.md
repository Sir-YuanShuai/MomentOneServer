# 外部身份关联与账号合并设计

> 文档状态：Draft 1.0  |  更新日期：2026-08-09
>
> 跨项目业务边界见 `../../../docs/domain/IDENTITY_AND_ACCOUNT_LINKING.md`。本文规定 MomentOneServer 的目标表、认证解析和合并事务。

## 1. 当前差异

当前认证主要使用 `users.casdoor_sub` 查找本地用户；`user_identities` 表已经存在，但尚未成为所有认证路径的唯一映射事实源，也没有账号绑定、解绑和合并 API。

目标：

```text
OIDC issuer + subject
-> user_identities
-> users.id
-> Moments / Assets / Devices / MCP / Entitlements / Quotas
```

`users.casdoor_sub` 和 `casdoor_user_id` 进入兼容迁移期，最终不再作为认证主键。

## 2. `user_identities` 目标字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 身份记录 |
| `user_id` | UUID FK | 内部用户 |
| `issuer` | TEXT | 规范化 OIDC Issuer |
| `subject` | TEXT | OIDC `sub` |
| `provider` | VARCHAR(32) | `casdoor/google/apple/github/wechat/phone/email/...` |
| `provider_account_id` | TEXT NULL | Provider 稳定账号 ID，若可用 |
| `display_name` | TEXT NULL | 只读资料快照 |
| `email` | TEXT NULL | 资料快照，不能作为合并依据 |
| `email_verified` | BOOLEAN | 来自已验证 Claim |
| `status` | VARCHAR(16) | `active/unlinked/disabled` |
| `linked_at` | TIMESTAMPTZ | 关联时间 |
| `last_seen_at` | TIMESTAMPTZ | 最近认证时间 |
| `unlinked_at` | TIMESTAMPTZ NULL | 解绑时间 |
| `revision` | INTEGER | 乐观锁 |

约束：

```text
UNIQUE (issuer, subject)
CHECK revision >= 1
INDEX (user_id, status)
```

## 3. Link Session

新增 `identity_link_sessions`：

```text
id UUID PK
user_id UUID FK
state_hash TEXT UNIQUE
pkce_verifier_ciphertext TEXT
provider TEXT
status pending/completed/expired/failed
expires_at TIMESTAMPTZ
created_at TIMESTAMPTZ
completed_at TIMESTAMPTZ NULL
metadata JSONB
```

安全要求：

- 原始 state 不落明文；
- verifier 使用服务端密钥加密或短期安全存储；
- session 绑定当前 user、redirect URI 和 provider；
- 默认 5 分钟过期、一次性使用；
- callback 只接受配置中的 Issuer；
- 完成后立即清理 verifier。

## 4. 认证迁移

迁移顺序：

1. 为现有 `users.casdoor_sub` 回填 `user_identities`；
2. 双读：先查 `user_identities`，未命中再查旧列并自动补写；
3. 所有新登录只写 `user_identities`；
4. 观察无旧路径命中后停止 fallback；
5. 旧列标记废弃，后续 migration 再删除。

认证成功必须：

- 检查 User `status`；
- 更新 Identity `last_seen_at`；
- 节流更新 User `last_active_at`；
- 不因为 profile email 变化而创建新 User。

## 5. 绑定 API

```http
GET  /v1/account/identities
POST /v1/account/link-sessions
GET  /v1/account/link-callback
POST /v1/account/identities/{id}/unlink-preview
POST /v1/account/identities/unlink-confirm
```

Link callback 结果：

```text
linked
already_linked
merge_required
expired
failed
```

未关联身份可以直接绑定当前 User；已属于另一个 User 时只能创建 Merge Preview。

## 6. Merge 数据模型

新增 `user_merge_operations`：

| 字段 | 说明 |
|---|---|
| `id` | 操作 ID |
| `source_user_id` | 被合并用户 |
| `surviving_user_id` | 保留用户 |
| `status` | `previewed/confirmed/running/completed/failed` |
| `preview` | 脱敏数量、冲突和权益说明 |
| `confirmation_id` | 二次确认票据 |
| `idempotency_key` | 幂等引用 |
| `requested_by_user_id` | 发起人 |
| `approved_by_user_id` | 管理员审核人，可空 |
| `error_code` | 稳定失败码 |
| `created_at/updated_at/completed_at` | 时间 |

新增 `user_merge_redirects`：

```text
merged_user_id PK
surviving_user_id FK
merge_operation_id FK
created_at
```

用于旧 Token、异步事件和外部订单投影在过渡期定位 surviving User。重定向不能绕过 Token 和 User 状态校验。

## 7. Merge 执行

大用户合并使用后台 Job：

1. 锁定两个 User，确认 Revision 和状态；
2. 将 source User 设为 `merging`，停止新写入；
3. 合并 External Identity；
4. 分批迁移 Moments、Assets、HabitGoals 等所有权；
5. 处理 DeviceBinding 和 MCP Authorization 冲突；
6. 合并 Entitlement/Quota，去掉重复默认免费 Grant；
7. 订单 Grant 按外部 Order ID 去重；
8. 重算存储 used/reserved/quota；
9. 写 Audit 和 Redirect；
10. source User 设为 `merged`，surviving User Revision +1；
11. 失败可从阶段 checkpoint 重试。

禁止修改历史 Moment `provenance`。AuditEvent 原 actor 不改写，只调整数据主体引用或通过 Merge Redirect 解析。

## 8. 权益合并

```text
default free grant: 每个 surviving User 最多一份
admin grant: 默认保留，Preview 明示
order grant: 按 provider + externalOrderId 唯一
subscription grant: 按 provider + subscriptionId 唯一
usage quota: 合并当前周期已使用值，不能取较小值
storage used: 对迁移后的 Asset 全量重算
cash balance: 不在本地合并，由 Casdoor 处理
```

该规则防止用户通过多渠道注册后合并来重复领取免费存储和 MCP 调用额度。

## 9. 管理后台

用户详情增加：

- 外部身份列表；
- Provider、Issuer、Subject 摘要和最近使用；
- 当前是否只有一个身份；
- 待处理 Link/Merge；
- Merge Preview；
- 双账号验证状态；
- 审核、重试和取消操作。

管理员不能直接输入 email 强制绑定。恢复场景需独立流程、近期认证或人工安全核验，并写入高风险审计。

## 10. 测试要求

- 同一 `(issuer, subject)` 不能关联两个 User；
- 同 email 不自动合并；
- 最后一个身份不能解绑；
- Link Session 过期、state 重放、PKCE 不匹配均失败；
- 合并过程中写请求被稳定拒绝或路由到 surviving User；
- Merge Job 重试不重复迁移、不重复权益；
- 免费 Grant 和订单 Grant 去重正确；
- 设备、MCP Token、存储和额度合并后仍归属同一 User；
- provenance 不变；
- 所有绑定、解绑、合并均有 AuditEvent。

## 相关文档

- [设备绑定](./DEVICE_BINDING.md)
- [Moment 领域模型](./MOMENT_DOMAIN_MODEL.md)
- [存储数据模型](../data/STORAGE_DATA_MODEL.md)
- 跨项目身份契约：`../../../docs/domain/IDENTITY_AND_ACCOUNT_LINKING.md`
- 跨项目权益契约：`../../../docs/contracts/ENTITLEMENTS_AND_LIMITS.md`
