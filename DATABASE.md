# MomentOne 数据库设计

## 概述

MomentOne 使用 PostgreSQL 作为持久化存储，通过 SQLAlchemy 2.0 (async) 作为 ORM，
Alembic 管理数据库迁移。

## 认证与用户模型

### 设计原则

- **Casdoor 管理公共身份信息**：头像、手机号、第三方绑定等由 Casdoor 统一管理
- **本地 users 表只存本项目特有字段**：通过 `casdoor_sub`（JWT 的 `sub` 字段，格式为 `owner/name`）与 Casdoor 关联
- **UUID 主键**：用户改名不影响本地数据关联

### 认证链路

```
浏览器 (react-oidc-context)
  → Authorization: Bearer <JWT> (RS256 签名)
  → 服务端 CORS 白名单校验
  → CasdoorTokenVerifier: JWKS 公钥验签 + 校验 iss/aud/exp
  → 从 JWT sub 提取 Casdoor 用户标识 (owner/name)
  → 查本地 users 表 (casdoor_sub)
    → 存在: 返回本地 UUID
    → 不存在: 调 Casdoor /api/userinfo 获取 UUID + profile, 写入 users 表
  → 用本地 UUID 作为 user_id 查 moments 表
```

### 安全措施

- JWT RS256 签名验证（PyJWT + JWKS 公钥，缓存 10 分钟）
- `iss`（签发方）、`aud`（受众）、`exp`（过期时间）校验
- CORS: `allow_credentials=False`，白名单 origin
- 数据库 session 每请求自动 commit/rollback

## 表结构

### users

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | 本地唯一用户 ID |
| casdoor_sub | VARCHAR(255), UNIQUE | Casdoor JWT 的 sub 字段 (owner/name) |
| casdoor_user_id | VARCHAR(255), INDEX | Casdoor 用户 UUID |
| display_name | VARCHAR(100) | 显示名称（从 Casdoor 同步） |
| email | VARCHAR(255) | 邮箱（从 Casdoor 同步） |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

### moments

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID (PK) | Moment 唯一 ID |
| user_id | UUID, INDEX | 关联 users.id |
| title | VARCHAR(20) | 标题 |
| description | TEXT | 描述 |
| voice_input | TEXT | 语音输入原文 |
| ai_summary | TEXT | AI 摘要 |
| category | VARCHAR(20) | 分类 (experience/habit/travel/food/growth/emotion) |
| tags | TEXT[] | 标签数组 |
| occurred_at | TIMESTAMPTZ | 发生时间 |
| timezone | VARCHAR(50) | 时区 |
| location_name | VARCHAR(200) | 位置名称 |
| location_latitude | FLOAT | 纬度 |
| location_longitude | FLOAT | 经度 |
| location_source | VARCHAR(20) | 位置来源 (device/user/mcp/unknown) |
| emotion_label | VARCHAR(50) | 情绪标签 |
| emotion_score | FLOAT | 情绪评分 |
| revision | INTEGER | 乐观锁版本号 |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |
| deleted_at | TIMESTAMPTZ | 软删除时间 (NULL = 未删除) |

## 迁移管理

```bash
# 创建新迁移
.venv/bin/python -m alembic revision --autogenerate -m "description"

# 执行迁移
.venv/bin/python -m alembic upgrade head

# 回滚
.venv/bin/python -m alembic downgrade -1
```

## 配置

`.env` 文件中需要配置：

```
MOMENT_ONE_DATABASE_URL=postgresql+psycopg://user:password@host:port/dbname
MOMENT_ONE_CASDOOR_ISSUER=https://account.example.com
MOMENT_ONE_CASDOOR_AUDIENCE=<client_id>
MOMENT_ONE_CASDOOR_JWKS_URL=https://account.example.com/.well-known/jwks
```
