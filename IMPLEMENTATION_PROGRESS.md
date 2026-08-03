# MomentOneServer — 实现进度

> 文档状态：Current  |  更新日期：2026-08-03
>
> 本文件记录**当前已实现的代码、表和 API**，是高频更新的"现状快照"。
> 目标设计见 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md)，
> 当本文件与目标设计存在差异时，以目标设计为准。

## 当前阶段

Phase 0-1：基础 Moment CRUD + Casdoor 认证 + PostgreSQL 持久化。

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
| revision | INTEGER | 乐观锁版本号（默认 1） |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |
| deleted_at | TIMESTAMPTZ | 软删除时间 (NULL = 未删除) |

> **目标差异**：
> - `location` 和 `emotion` 将合并为 `jsonb` 列
> - `emotion` 需增加 `source` 和 `arousal`
> - 需增加 `provenance`（jsonb，v1 正式字段，创建后不可篡改）
> - 需增加 `normalized_search_text`
> - `revision` 目标 `CHECK (revision >= 1)`

## 已实现的 API

| 方法 | 路径 | 状态 | 备注 |
|---|---|---|---|
| GET | `/v1/system/health` | 已实现 | 健康检查 |
| GET | `/v1/system/version` | 已实现 | 版本信息 |
| POST | `/v1/moments` | 已实现 | 创建 Moment |
| GET | `/v1/moments` | 已实现 | 列表查询（cursor 分页） |
| GET | `/v1/moments/{id}` | 已实现 | 详情 |
| PATCH | `/v1/moments/{id}` | 已实现 | 修改（乐观锁） |
| DELETE | `/v1/moments/{id}` | 已实现 | 软删除（两阶段确认） |

## 已实现的迁移

| 迁移 | 说明 |
|---|---|
| `0001_create_users_and_moments` | 创建 users 和 moments 表 |

## 已实现的模块

| 模块 | 路径 | 状态 |
|---|---|---|
| Moment 领域 | `app/modules/moments/` | 已实现 |
| Identity 认证 | `app/modules/identity/` + `app/infrastructure/identity/casdoor.py` | 已实现 |
| Search | `app/modules/search/` | 骨架 |
| Audit | `app/modules/audit/` | 骨架 |
| Confirmations | `app/modules/confirmations/` | 骨架（内存态） |
| Media | `app/modules/media/` | 骨架 |

## 未实现的目标表

按 [STORAGE_DATA_MODEL.md](./docs/data/STORAGE_DATA_MODEL.md) 分阶段：

| 阶段 | 表 | 状态 |
|---|---|---|
| Phase 1 | `user_identities`, `moment_revisions`, `idempotency_keys`, `audit_events` | 待实现 |
| Phase 2 | `assets`, `moment_assets`, `user_configs`, `devices`, `device_bindings` | 待实现 |
| Phase 3 | `pending_confirmations` | 待实现（当前内存态） |
| Phase 4+ | `sync_cursors`, `sync_change_log`, `oauth_clients`, `access_grants`, `agent_connections`, `ai_artifacts`, `search_embeddings` | 待实现 |

## 测试覆盖

| 测试 | 路径 | 状态 |
|---|---|---|
| Moment Service 单元测试 | `tests/unit/test_moment_service.py` | 已实现 |
| Config 单元测试 | `tests/unit/test_config.py` | 已实现 |
| Database 集成测试 | `tests/integration/test_database.py` | 已实现 |
| System API 测试 | `tests/api/test_system.py` | 已实现 |
