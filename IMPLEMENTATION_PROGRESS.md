# MomentOneServer — 实现进度

> 文档状态：Current  |  更新日期：2026-08-09
>
> 本文件记录**当前已实现的代码、表和 API**，是高频更新的"现状快照"。
> 目标设计见 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md)，
> 当本文件与目标设计存在差异时，以目标设计为准。

## 当前阶段

Phase 0-1：基础 Moment CRUD + Casdoor 认证 + PostgreSQL 持久化。
Phase 2 部分：设备绑定（Device Binding）+ OAuth 2.1 Token 端点（眼镜端鉴权）。
**Phase MCP：MCP Server（Streamable HTTP + Bearer 鉴权）+ MCP OAuth 代理 + 记账工具 + MCP Apps UI（第一版，见 [docs/roadmap/MCP_APPS_PLAN.md](../docs/roadmap/MCP_APPS_PLAN.md)）。**

## 已实现的表

### users

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | 本地唯一用户 ID |
| casdoor_sub | VARCHAR(255), UNIQUE | Casdoor JWT 的 sub 字段 (owner/name) |
| casdoor_user_id | VARCHAR(64), INDEX | Casdoor 用户 UUID |
| display_name | VARCHAR(100) | 显示名称（从 Casdoor 同步） |
| email | VARCHAR(255) | 邮箱（从 Casdoor 同步） |
| status | VARCHAR(16), INDEX | `active` / `disabled`，所有认证通道统一检查 |
| revision | INTEGER | 管理操作乐观锁版本 |
| last_active_at | TIMESTAMPTZ | 最近业务访问时间（5 分钟节流更新） |
| disabled_at | TIMESTAMPTZ | 停用时间 |
| disable_reason | VARCHAR(240) | 脱敏的停用原因 |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

> **目标差异**：`status` 已实现；`casdoor_sub` / `casdoor_user_id` 仍待迁移到独立的 `user_identities` 表。

### moments

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | Moment 唯一 ID |
| user_id | UUID, INDEX | 关联 users.id |
| title | VARCHAR(20) | 标题（≤20 字） |
| description | TEXT | 描述（≤240 字，可选） |
| voice_input | TEXT | 语音输入原文（可选） |
| ai_summary | TEXT | AI 摘要（≤80 字，可选） |
| category | VARCHAR(20) | 分类 |
| tags | TEXT[] | 标签数组（≤5） |
| persons | TEXT[] | 通用描述维度（ADR-0019）：人物（≤10 个，每项 ≤20 字，可空） |
| event_name | VARCHAR(50) | 通用描述维度（ADR-0019）：所属事件（可空） |
| occurred_at | TIMESTAMPTZ | 发生时间 |
| timezone | VARCHAR(50) | 时区 |
| location_name | VARCHAR(200) | 位置名称（可选） |
| location_latitude | FLOAT | 纬度（可选） |
| location_longitude | FLOAT | 经度（可选） |
| location_source | VARCHAR(20) | 位置来源 |
| emotion_label | VARCHAR(50) | 情绪标签（可选） |
| emotion_score | FLOAT | 情绪效价（可选） |
| provenance | JSONB | v1 正式字段：来源链（source + 可选 deviceId/clientId/mcpServerId/mcpToolName/externalId），创建后不可篡改 |
| revision | INTEGER | 乐观锁版本号（默认 1） |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |
| deleted_at | TIMESTAMPTZ | 软删除时间 (NULL = 未删除) |

> **目标差异**：
> - `location` 和 `emotion` 将合并为 `jsonb` 列
> - `emotion` 需增加 `source` 和 `arousal`
> - 需增加 `normalized_search_text`
> - `revision` 目标 `CHECK (revision >= 1)`

### devices

| 字段 | 类型 | 说明 |
|---|---|---|
| id | VARCHAR(64), PK | 设备唯一 ID（眼镜端生成，建议 UUID） |
| device_type | VARCHAR(48) | 设备类型（可选，如 `rokid-glasses`） |
| device_name | VARCHAR(120) | 设备名称（可选，用户可读） |
| created_at | TIMESTAMPTZ | 创建时间 |

### device_bindings

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | 绑定记录 ID |
| user_id | UUID, INDEX | 关联 users.id |
| device_id | VARCHAR(64), INDEX | 关联 devices.id |
| scope | TEXT[] | 授权范围（如 `moments:read,moments:write`） |
| status | VARCHAR(16) | `active` / `revoked` |
| access_token_jti | VARCHAR(64), UNIQUE | 当前 access_token 的 JTI（防重放） |
| refresh_token_jti | VARCHAR(64), UNIQUE | 当前 refresh_token 的 JTI |
| access_token_expires_at | TIMESTAMPTZ | access_token 过期时间 |
| refresh_token_expires_at | TIMESTAMPTZ | refresh_token 过期时间 |
| last_active_at | TIMESTAMPTZ | 最近一次 token 使用时间 |
| bound_at | TIMESTAMPTZ | 绑定完成时间 |
| revoked_at | TIMESTAMPTZ | 撤销时间（NULL = 未撤销） |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

> **约束**：`(user_id, device_id)` 唯一（一个用户对同一设备只能有一份有效绑定）。

### binding_codes

| 字段 | 类型 | 说明 |
|---|---|---|
| code | VARCHAR(32), PK | 一次性绑定码（`secrets.token_urlsafe(16)`） |
| user_id | UUID, INDEX | 发起绑定的用户 |
| scope | TEXT[] | 预授权范围 |
| device_name | VARCHAR(120) | 预填设备名（可选） |
| status | VARCHAR(16) | `pending` / `used` / `expired` |
| expires_at | TIMESTAMPTZ | 过期时间（默认 5 分钟） |
| used_at | TIMESTAMPTZ | 使用时间（NULL = 未使用） |
| created_at | TIMESTAMPTZ | 创建时间 |

## 已实现的 API

| 方法 | 路径 | 状态 | 备注 |
|---|---|---|---|
| GET | `/v1/system/health` | 已实现 | 健康检查 |
| GET | `/v1/system/version` | 已实现 | 版本信息 |
| POST | `/v1/moments` | 已实现 | 创建 Moment（支持 assetIds 关联媒体） |
| GET | `/v1/moments` | 已实现 | 列表查询（cursor 分页，支持 type/category/tag/goalId 过滤，响应含 media 数组） |
| GET | `/v1/moments/{id}` | 已实现 | 详情（含 media 数组 + downloadUrl） |
| PATCH | `/v1/moments/{id}` | 已实现 | 修改（乐观锁） |
| DELETE | `/v1/moments/{id}` | 已实现 | 软删除（两阶段确认） |
| POST | `/v1/assets/upload-intents` | 已实现 | 创建 Asset + 返回 Presigned PUT URL |
| POST | `/v1/assets/{assetId}/complete` | 已实现 | head_object 校验 + 状态机 ready；image 类同步生成 WebP 缩略图，失败降级不影响上传 |
| GET | `/v1/assets/{assetId}` | 已实现 | Asset 元数据查询 |
| POST | `/v1/assets/{assetId}/download-url` | 已实现 | 短期 GET Presigned URL（仅 READY） |
| POST | `/v1/device/bindings` | 已实现 | 创建绑定会话（Web 端，Casdoor Bearer） |
| GET | `/v1/device/bindings` | 已实现 | 列出当前用户已绑定设备 |
| DELETE | `/v1/device/bindings/{id}` | 已实现 | 撤销绑定（吊销 token） |
| PATCH | `/v1/device/bindings/{id}` | 已实现 | 调整 scope |
| POST | `/oauth/token` | 已实现 | OAuth 2.1 Token 端点（QR Binding / glasses refresh / MCP authorization_code + PKCE / MCP refresh） |
| GET | `/oauth/authorize` | 已实现 | MCP OAuth 授权端点（PKCE 校验 → 302 跳转 Casdoor 登录） |
| GET | `/oauth/callback` | 已实现 | Casdoor 回调 → 换 Casdoor token → 签发我方 RS256 token 的授权码 → 302 回客户端 |
| POST | `/oauth/register` | 已实现 | MCP OAuth 动态客户端注册（RFC 7591） |
| GET | `/.well-known/oauth-protected-resource` | 已实现 | RFC 9728（根路径 + `/mcp` 子路径） |
| GET | `/.well-known/oauth-authorization-server` | 已实现 | RFC 8414（根路径 + `/mcp` 子路径） |
| POST | `/mcp` | 已实现 | MCP Streamable HTTP 端点（官方 Python `mcp` SDK，Bearer 鉴权，401 带 WWW-Authenticate） |
| POST | `/v1/habit-goals` | 已实现 | 创建习惯目标（ADR-0018） |
| GET | `/v1/habit-goals` | 已实现 | 习惯目标列表 |
| GET | `/v1/habit-goals/{id}` | 已实现 | 习惯目标详情 |
| PATCH | `/v1/habit-goals/{id}` | 已实现 | 修改习惯目标（乐观锁） |
| POST | `/v1/habit-goals/{id}/delete-preview` | 已实现 | 删除预览（两阶段） |
| POST | `/v1/habit-goals/delete-confirm` | 已实现 | 删除确认 |

## 已实现的迁移

| 迁移 | 说明 |
|---|---|
| `0001_create_users_and_moments` | 创建 users 和 moments 表 |
| `0002_create_devices_and_bindings` | 创建 devices / device_bindings / binding_codes 表 |
| `0003_add_provenance_to_moments` | 给 moments 表增加 provenance jsonb 列 |
| `0004_create_pending_confirmations` | 创建 pending_confirmations 表（替代内存态两阶段删除） |
| `0005_create_idempotency_keys` | 创建 idempotency_keys 表（写操作去重） |
| `0006_create_phase1_identity_revision_audit` | 创建 user_identities / moment_revisions / audit_events 表 |
| `0007_create_assets_and_moment_assets` | 创建 assets / moment_assets 表 + moments 推荐约束 |
| `0008_add_moment_type_payload` | moments 表新增 moment_type / payload 两列（内置记录类型，ADR-0017） |
| `0009_create_habit_goals` | 创建 habit_goals 表（习惯目标实体，ADR-0018） |
| `0010_add_persons_and_event` | moments 表新增 persons / event_name 两列（通用描述维度，ADR-0019） |
| `0011_add_frequency_and_color_to_habit_goals` | habit_goals 表新增 frequency / times_per_week / color 列（对标习惯打卡 App，ADR-0020） |
| `0012_create_mcp_oauth_tables` | 创建 mcp_oauth_clients / mcp_oauth_codes 表（MCP OAuth DCR 客户端 + 授权码/事务状态） |
| `0018_add_admin_operations_fields` | users 增加状态/版本/活动时间，device_bindings 与 mcp_authorizations 增加 revision |

## 已实现的模块

| 模块 | 路径 | 状态 |
|---|---|---|
| Moment 领域 | `app/modules/moments/` | 已实现 |
| 内置记录类型注册表 | `app/modules/moment_types/` + `contracts/types/*.schema.json` | 已实现（bookkeeping / habit / general；validate(type, payload) 按 JSON Schema 校验） |
| 习惯目标（HabitGoal） | `app/modules/habit_goals/` + `app/infrastructure/database/repositories/habit_goal_repository.py` + `app/api/routes/habit_goals.py` | 已实现（CRUD + 两阶段删除；打卡 payload.goalId 归属校验） |
| Identity 认证 | `app/modules/identity/` + `app/infrastructure/identity/casdoor.py` | 已实现（Casdoor isAdmin / 应用角色 / 权限归一化；暂停账号全通道阻断） |
| Device Binding | `app/modules/devices/` + `app/infrastructure/jwt/issuer.py` + `app/infrastructure/binding_codes/generator.py` + `app/infrastructure/database/repositories/device_repository.py` | 已实现 |
| Search | `app/modules/search/` | 骨架 |
| MCP Server | `app/modules/mcp/`（server/tools/a2ui/token_verifier/deps/endpoint）+ `app/api/routes/mcp_discovery.py` | 已实现（记账；通用 Moment 创建/列表/搜索/计数/详情；每日回顾；习惯目标/打卡/进度；`agent_plan`；`a2ui_action`；工具级 Scope + 审计；A2UI v0.9 自动化测试与 MomentOneGlasses 实际客户端连接 standalone 联调已通过，待真机 AIUI 页面联调后提交/部署） |
| MCP OAuth 代理 | `app/modules/mcp_oauth/` + `app/api/routes/mcp_oauth.py`（authorize/callback/register）+ `app/api/routes/oauth.py` token 扩展 | 已实现（DCR RFC 7591 + PKCE + Casdoor 代理跳转；token 复用 JwtIssuer RS256） |
| MCP Apps UI | `mcp_apps/bookkeeping/`（Vite 多入口单文件构建，`@modelcontextprotocol/ext-apps`） | 已实现 3 个紧凑结果卡：`bookkeeping` / `timeline` / `habits`；卡片只渲染本次 tool 的 structuredContent，不在 UI 内提供搜索表单或主动拉取数据 |
| A2UI over MCP | `app/modules/mcp/a2ui.py` + `contracts/a2ui/` | 已实现 Server 侧 v0.9 capability negotiation、官方 Schema/Catalog 固定、9 个紧凑结果卡 builder、校验失败文本降级与标准 Tool Result fixture；实际眼镜客户端代码 standalone 联调已通过，未提交/未部署，等待真机 AIUI 页面联调 |
| Admin Operations | `app/modules/admin/` + `app/api/routes/admin.py` | 已实现（概览、用户、设备/MCP 授权、审计、过期记录 Preview + Confirm） |
| Audit | `app/modules/audit/` | 已实现只追加写入与管理员只读查询 |
| Confirmations | `app/modules/confirmations/` | 已实现（持久化，集成在 moments 路由） |
| Media / Assets | `app/modules/assets/` + `app/infrastructure/storage/object_storage.py` + `app/infrastructure/database/repositories/asset_repository.py` + `app/api/routes/assets.py` | 已实现（S3/MinIO 适配 + Asset 状态机 + Moment 创建/更新媒体关联 + 稳定版本快照 + 缩略图生成（迁移 0017，仅 image，400px WebP，失败降级）+ Upload Intent 存储额度预留/完成结算（迁移 0019）） |
| Entitlements / Storage Quota | `app/modules/entitlements/` + `app/modules/admin/entitlements.py` | 第一批已实现（Free/Plus/Pro 计划、用户权益、存储账户、额度 Grant、overQuota、管理员套餐/额度/对账 API） |
| Quota Metering / Usage Analytics | `app/modules/quotas/` + `app/modules/mcp/quota_middleware.py` + `app/modules/admin/analytics.py` | 第二批已实现（MCP Tool/写 Tool/Planner/API 请求计量、Scope+Entitlement+Quota 工具过滤、设备/MCP客户端上限、用户与管理员用量视图） |
| Account Self-service | `app/api/routes/account.py` + `app/modules/account_deletion/` | 已实现（头像/套餐/额度查询、Casdoor 资料同步、注销 Preview+近期重认证+Confirm、对象和业务数据永久删除） |

## 未实现的目标表

按 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md) 分阶段：

| 阶段 | 表 | 状态 |
|---|---|---|
| Phase 1 | `user_identities`, `moment_revisions`, `idempotency_keys`, `audit_events` | 已实现（表 + ORM + repository；moments 路由已接入 revisions + audit） |
| Phase 2 | `assets`, `moment_assets`, `user_configs` | `assets` + `moment_assets` 已实现（迁移 0007 + ORM + repository + API）；`user_configs` 待实现 |
| Phase 3 | `pending_confirmations` | 已实现（替代内存态） |
| Subscription Phase 1 | `plan_definitions`, `user_entitlements`, `user_storage_accounts`, `storage_quota_grants` | 已实现（迁移 0019 + ORM + Upload Reservation + 管理 API） |
| Subscription Phase 2 | `quota_accounts`, `quota_usage_events`, `api_usage_buckets` | 已实现（迁移 0020 + MCP/API 计量 + 管理分析） |
| Subscription Phase 3+ | `quota_usage_buckets`, `product_entitlement_mappings`, `external_orders`, `billing_events` | 待后续批次实现 |
| Phase 4+ | `sync_cursors`, `sync_change_log`, `oauth_clients`, `access_grants`, `agent_connections`, `ai_artifacts`, `search_embeddings` | 待实现 |

## 测试覆盖

| 测试 | 路径 | 状态 |
|---|---|---|
| Moment Service 单元测试 | `tests/unit/test_moment_service.py` | 已实现 |
| Config 单元测试 | `tests/unit/test_config.py` | 已实现 |
| Database 集成测试 | `tests/integration/test_database.py` | 已实现 |
| System API 测试 | `tests/api/test_system.py` | 已实现 |
| JwtIssuer 单元测试 | `tests/unit/test_jwt_issuer.py` | 已实现 |
| Binding Code Generator 单元测试 | `tests/unit/test_binding_code_generator.py` | 已实现 |
| DeviceBindingService 单元测试 | `tests/unit/test_device_binding_service.py` | 已实现 |
| OAuth Token API 测试 | `tests/api/test_oauth_token.py` | 已实现 |
| Moments API 测试 | `tests/api/test_moments_api.py` | 已实现（含 assetIds 关联 + media 响应 + type/payload 校验与过滤） |
| 内置记录类型单测 | `tests/unit/test_moment_types.py` | 已实现（bookkeeping / habit / general 校验） |
| A2UI / Planner 测试 | `tests/unit/test_a2ui.py` + `tests/api/test_mcp_server.py` | 已实现（capability namespace/session、Schema、fixture、降级、Tool Result、`agent_plan`、`a2ui_action`、Scope 与 MCP Apps 回归） |
| 习惯目标 API 测试 | `tests/api/test_habit_goals_api.py` | 已实现（CRUD + revision 冲突 + 两阶段删除） |
| 打卡 goalId 关联测试 | `tests/api/test_moments_api.py` | 已实现（合法 / 未知 / 非法格式 goalId） |
| Assets API 测试 | `tests/api/test_assets_api.py` | 已实现（upload-intents / complete / get / download-url + Moment 创建/更新媒体集成 + 附件替换/保留/清空 + 缩略图生成/降级/URL 签发） |

## 订阅/身份阶段进展（2026-08-09）

- **第一批已实现**：`plan_definitions`、`user_entitlements`、`user_storage_accounts`、`storage_quota_grants`、Upload Reservation、`overQuota` 和管理后台 API；
- 订阅权益边界：Casdoor 管余额/订单/支付/订阅，Server 管 Entitlement、Quota 和执行；
- 建议 Free/Plus/Pro 存储为 1/10/50 GiB，MCP/眼镜共享 Tool、写 Tool、Planner、设备和客户端额度；
- 存储引入 used/reserved/effectiveQuota、Upload Reservation 和 Bucket 对账；
- MCP `tools/list` 与 `agent_plan` 已按 Scope + Entitlement + Quota 过滤；A2UI/Text/structuredContent 只计一次；
- 账号关联目标让 `user_identities` 接管认证，增加 Link Session、解绑 Preview/Confirm、User Merge；
- 账号合并时迁移数据、设备、MCP、存储和权益，默认免费 Grant 不能重复领取；
- 详细文档：`docs/domain/IDENTITY_ACCOUNT_LINKING.md`、`docs/data/STORAGE_DATA_MODEL.md`、根目录 `docs/contracts/ENTITLEMENTS_AND_LIMITS.md`。
