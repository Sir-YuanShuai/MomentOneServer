# MomentOne 数据库设计

> 文档状态：Current  |  更新日期：2026-08-03

## 概述

MomentOne 使用 PostgreSQL 作为持久化存储，通过 SQLAlchemy 2.0 (async) 作为 ORM，
Alembic 管理数据库迁移。

> **权威设计文档**：[PostgreSQL 与 MinIO 存储数据模型](./docs/data/STORAGE_DATA_MODEL.md)
> 定义了完整的分阶段表结构、约束、索引和事务边界。
>
> **当前实现快照**：[IMPLEMENTATION_PROGRESS.md](./IMPLEMENTATION_PROGRESS.md)
> 记录当前已实现的表、API 和模块，高频更新。
>
> 本文件只记录**稳定的参考信息**（认证链路、迁移命令、配置），不重复表结构细节。

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

## 迁移管理

```bash
# 创建新迁移
.venv/bin/python -m alembic revision --autogenerate -m "description"

# 执行迁移
.venv/bin/python -m alembic upgrade head

# 回滚
.venv/bin/python -m alembic downgrade -1
```

或使用 Makefile：

```bash
make migrate                          # alembic upgrade head
make migrate-new name="add xxx table" # 创建新迁移
```

## 配置

`.env` 文件中需要配置：

```
MOMENT_ONE_DATABASE_URL=postgresql+psycopg://user:password@host:port/dbname
MOMENT_ONE_CASDOOR_ISSUER=https://account.example.com
MOMENT_ONE_CASDOOR_AUDIENCE=<client_id>
MOMENT_ONE_CASDOOR_JWKS_URL=https://account.example.com/.well-known/jwks
```

## 相关文档

- [PostgreSQL 与 MinIO 存储数据模型](./docs/data/STORAGE_DATA_MODEL.md) — 权威目标设计
- [实现进度](./IMPLEMENTATION_PROGRESS.md) — 当前已实现的表和 API
- [Moment One 领域模型](./docs/domain/MOMENT_DOMAIN_MODEL.md) — 概念模型
- [Moment Domain v1 Schema](./contracts/schemas/moment.v1.json) — JSON Schema 权威定义
