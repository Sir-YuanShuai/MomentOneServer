# MomentOneServer — 实现进度

> 文档状态：Current  |  更新日期：2026-08-04
>
> 本文件记录**当前已实现的代码、表和 API**，是高频更新的"现状快照"。
> 目标设计见 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md)，
> 当本文件与目标设计存在差异时，以目标设计为准。

## 当前阶段

Phase 0-1：基础 Moment CRUD + Casdoor 认证 + PostgreSQL 持久化。
Phase 2 部分：设备绑定（Device Binding）+ OAuth 2.1 Token 端点（眼镜端鉴权）。
详见 [docs/domain/DEVICE_BINDING.md](./docs/domain/DEVICE_BINDING.md)。

## 已实现的表

### users

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | 本地唯一用户 ID |
| casdoor_sub | VARCHAR(255), UNIQUE | Casdoor JWT 的 sub 字段 (owner/name) |
| casdoor_user_id | VARCHAR(64), INDEX | Casdoor 用户 UUID |
| display_name | VARCHAR(100) | 显示名称（从 Casdoor 同步） |
| email | VARCHAR(255) | 邮箱（从 Casdoor 同步） |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

> **目标差异**：目标设计中 `casdoor_sub` / `casdoor_user_id` 将迁移到独立的 `user_identities` 表，`users` 表增加 `status` 列。

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
| GET | `/v1/moments` | 已实现 | 列表查询（cursor 分页，响应含 media 数组） |
| GET | `/v1/moments/{id}` | 已实现 | 详情（含 media 数组 + downloadUrl） |
| PATCH | `/v1/moments/{id}` | 已实现 | 修改（乐观锁） |
| DELETE | `/v1/moments/{id}` | 已实现 | 软删除（两阶段确认） |
| POST | `/v1/assets/upload-intents` | 已实现 | 创建 Asset + 返回 Presigned PUT URL |
| POST | `/v1/assets/{assetId}/complete` | 已实现 | head_object 校验 + 状态机 ready |
| GET | `/v1/assets/{assetId}` | 已实现 | Asset 元数据查询 |
| POST | `/v1/assets/{assetId}/download-url` | 已实现 | 短期 GET Presigned URL（仅 READY） |
| POST | `/v1/device/bindings` | 已实现 | 创建绑定会话（Web 端，Casdoor Bearer） |
| GET | `/v1/device/bindings` | 已实现 | 列出当前用户已绑定设备 |
| DELETE | `/v1/device/bindings/{id}` | 已实现 | 撤销绑定（吊销 token） |
| PATCH | `/v1/device/bindings/{id}` | 已实现 | 调整 scope |
| POST | `/oauth/token` | 已实现 | OAuth 2.1 Token 端点（眼镜端，无 Casdoor 鉴权） |

## 已实现的迁移

| 迁移 | 说明 |
|---|---|
| `0001_create_users_and_moments` | 创建 users 和 moments 表 |
| `0002_create_devices_and_bindings` | 创建 devices / device_bindings / binding_codes 表 |
| `0003_add_provenance_to_moments` | 给 moments 表增加 provenance jsonb 列 |
| `0004_create_pending_confirmations` | 创建 pending_confirmations 表（替代内存态两阶段删除） |
| `0005_create_idempotency_keys` | 创建 idempotency_keys 表（写操作去重） |
| `0006_create_phase1_identity_revision_audit` | 创建 user_identities / moment_revisions / audit_events 表 |

## 已实现的模块

| 模块 | 路径 | 状态 |
|---|---|---|
| Moment 领域 | `app/modules/moments/` | 已实现 |
| Identity 认证 | `app/modules/identity/` + `app/infrastructure/identity/casdoor.py` | 已实现 |
| Device Binding | `app/modules/devices/` + `app/infrastructure/jwt/issuer.py` + `app/infrastructure/binding_codes/generator.py` + `app/infrastructure/database/repositories/device_repository.py` | 已实现 |
| Search | `app/modules/search/` | 骨架 |
| Audit | `app/modules/audit/` | 骨架 |
| Confirmations | `app/modules/confirmations/` | 已实现（持久化，集成在 moments 路由） |
| Media / Assets | `app/modules/assets/` + `app/infrastructure/storage/object_storage.py` + `app/infrastructure/database/repositories/asset_repository.py` + `app/api/routes/assets.py` | 已实现（S3/MinIO 适配 + Asset 状态机 + Moment 媒体关联） |

## 未实现的目标表

按 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md) 分阶段：

| 阶段 | 表 | 状态 |
|---|---|---|
| Phase 1 | `user_identities`, `moment_revisions`, `idempotency_keys`, `audit_events` | 已实现（表 + ORM + repository；moments 路由已接入 revisions + audit） |
| Phase 2 | `assets`, `moment_assets`, `user_configs` | `assets` + `moment_assets` 已实现（迁移 0007 + ORM + repository + API）；`user_configs` 待实现 |
| Phase 3 | `pending_confirmations` | 已实现（替代内存态） |
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
| Moments API 测试 | `tests/api/test_moments_api.py` | 已实现（含 assetIds 关联 + media 响应） |
| Assets API 测试 | `tests/api/test_assets_api.py` | 已实现（upload-intents / complete / get / download-url + Moment 媒体集成） |
