# Moment One 服务端实施方案

> 文档状态：Draft 1.0  
> 创建日期：2026-08-01  
> 适用项目：`MomentOneServer`  
> 当前阶段：从 0 初始化 / 服务端 Phase 0

## 1. 文档目的

本文档定义 Moment One 服务端的首期范围、技术选型、系统边界、模块设计、数据模型、安全要求、交付阶段和验收标准，作为后续初始化仓库、编写 ADR、数据库迁移和 API 契约的基础。

服务端领域与数据模型详见：

- [Moment One 领域模型](./docs/domain/MOMENT_DOMAIN_MODEL.md)；
- [PostgreSQL 与 MinIO 存储数据模型](./docs/data/STORAGE_DATA_MODEL.md)；
- [ADR-0001：领域模型与存储边界](./docs/decisions/0001_DOMAIN_AND_STORAGE_BOUNDARIES.md)。

相关产品与平台规划：

- [跨平台实施路线图](../MomentOne/docs/roadmap/PLATFORM_ROADMAP.md)
- [本地 MVP 范围](../MomentOne/docs/mvp/LOCAL_MVP_SCOPE.md)
- [跨平台总体架构](../MomentOne/docs/architecture/CROSS_PLATFORM_ARCHITECTURE.md)
- [Moment MCP Server 契约](../MomentOne/docs/contracts/MCP_SERVER_CONTRACT.md)
- [身份、同步与安全](../MomentOne/docs/security/IDENTITY_SYNC_SECURITY.md)
- [ADR-0001：存储与 MCP 边界](../MomentOne/docs/decisions/0001_STORAGE_AND_MCP_BOUNDARIES.md)

## 2. 当前已确认条件

当前基础设施和技术方向如下：

| 项目 | 当前决定 |
|---|---|
| 服务端语言 | Python 3.14（兼容基线 >= 3.13） |
| HTTP 框架 | FastAPI |
| 身份服务 | 已部署 Casdoor，通过 OIDC/OAuth 接入 |
| 对象存储 | 已部署 MinIO，通过 S3-compatible API 接入 |
| 主数据库 | PostgreSQL，连接信息待提供 |
| Web 前端 | 独立静态 SPA，不在本项目渲染 |
| Redis | 首期不引入，出现明确缓存、分布式限流或任务队列需求后再评估 |
| 部署形态 | Docker 化模块化单体 |
| MCP | 不进入首期运行范围，保留清晰适配边界 |

## 3. 项目边界

### 3.1 本项目负责

`MomentOneServer` 只负责服务端能力：

- Casdoor Access Token 验证；
- 用户与设备身份映射；
- Moment 创建、查询、详情、修改和删除确认；
- Revision 乐观锁；
- Idempotency 幂等处理；
- Cursor 分页；
- PostgreSQL 持久化；
- MinIO 媒体上传意图、上传确认和短期下载地址；
- 搜索、审计、限流、日志和健康检查；
- 后续眼镜同步和 MCP 的领域服务基础。

### 3.2 本项目不负责

- Web 页面渲染；
- SSR；
- Jinja2 页面；
- 客户端静态资源托管；
- Casdoor 内部账号密码实现；
- MinIO 管理后台；
- PostgreSQL 管理后台；
- 当前 Rokid AIUI 本地 MVP；
- 首期 MCP Server、MCP Apps、外部 MCP Client；
- 首期独立向量数据库或搜索集群。

### 3.3 前端形态

Web 前端建议建立独立项目，例如：

```text
MomentOneWeb/
├── Vite
├── React 或 Vue
├── TypeScript
├── Casdoor OIDC Authorization Code + PKCE
└── REST API Client
```

构建后只产生静态文件，可部署到 CDN、Nginx 或静态对象存储：

```text
Static Web SPA
      ↓ Bearer Access Token
MomentOneServer REST API
      ├── Casdoor
      ├── PostgreSQL
      └── MinIO
```

浏览器中不得保存 Casdoor Client Secret、MinIO 永久凭据或数据库凭据。

## 4. 总体架构

首期采用模块化单体：

```text
                       Client Layer
        ┌──────────────────┼──────────────────┐
        │                  │                  │
    Rokid AIUI         Static Web         Future Mobile
        └──────────────────┼──────────────────┘
                           │ HTTPS / JSON
                           ▼
                  MomentOneServer
┌─────────────────────────────────────────────────────────────┐
│ FastAPI Transport                                           │
│ - REST Routes                                               │
│ - Request Validation                                        │
│ - Authentication Context                                    │
│ - Error Mapping                                             │
├─────────────────────────────────────────────────────────────┤
│ Application / Domain                                        │
│ - IdentityService                                           │
│ - MomentService                                             │
│ - AssetService                                              │
│ - ConfirmationService                                       │
│ - AuditService                                              │
│ - SearchService                                             │
├─────────────────────────────────────────────────────────────┤
│ Infrastructure                                              │
│ - CasdoorOidcProvider                                       │
│ - PostgresRepositories                                      │
│ - MinioObjectStorage                                        │
│ - Structured Logging                                        │
└──────────────────┬──────────────────┬───────────────────────┘
                   │                  │
             PostgreSQL             MinIO
```

依赖方向必须保持：

```text
Transport -> Application -> Domain -> Repository Interface
                                      ↓
                               Infrastructure
```

禁止：

```text
FastAPI Route -> 直接执行 SQL
FastAPI Route -> 直接拼接 MinIO Object Key
MCP Tool -> 直接执行 SQL
客户端 -> 直接访问 PostgreSQL
客户端 -> 持有 MinIO 永久密钥
```

## 5. 技术选型

### 5.1 核心技术栈

```text
Python 3.14（兼容基线 >= 3.13）
FastAPI
Pydantic v2
SQLAlchemy 2
psycopg 3
Alembic
PostgreSQL
Casdoor OIDC
MinIO / S3-compatible API
Uvicorn
```

### 5.2 工程工具

```text
uv                 依赖和虚拟环境管理
Ruff               格式化和静态检查
Pyright            类型检查
pytest             测试框架
pytest-asyncio     异步测试
httpx              API 测试和外部 HTTP 调用
Testcontainers     PostgreSQL 集成测试（条件允许时）
Docker             构建和运行
Docker Compose     本地依赖编排
```

### 5.3 首期主要 Python 依赖

```text
fastapi
uvicorn
pydantic
pydantic-settings
sqlalchemy
psycopg
alembic
pyjwt
cryptography
httpx
boto3
structlog
```

具体版本在初始化工程时锁定到当时验证通过的兼容版本，不在本文档中绑定补丁版本。

### 5.4 暂不引入

```text
Redis
Celery / Dramatiq
Kafka / RabbitMQ
ElasticSearch / OpenSearch
pgvector
独立向量数据库
Kubernetes
GraphQL
微服务框架
服务端模板引擎
```

## 6. 推荐目录结构

```text
MomentOneServer/
├── app/
│   ├── main.py
│   ├── application.py
│   │
│   ├── api/
│   │   ├── dependencies.py
│   │   ├── error_handlers.py
│   │   └── router.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── logging.py
│   │   ├── pagination.py
│   │   ├── request_context.py
│   │   └── security.py
│   │
│   ├── modules/
│   │   ├── identity/
│   │   │   ├── domain.py
│   │   │   ├── schemas.py
│   │   │   ├── repository.py
│   │   │   ├── service.py
│   │   │   └── routes.py
│   │   ├── moments/
│   │   │   ├── domain.py
│   │   │   ├── schemas.py
│   │   │   ├── repository.py
│   │   │   ├── service.py
│   │   │   └── routes.py
│   │   ├── media/
│   │   │   ├── domain.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── routes.py
│   │   ├── confirmations/
│   │   ├── audit/
│   │   └── search/
│   │
│   └── infrastructure/
│       ├── database/
│       │   ├── session.py
│       │   ├── models/
│       │   └── repositories/
│       ├── identity/
│       │   └── casdoor.py
│       └── storage/
│           └── minio.py
│
├── contracts/
│   ├── schemas/
│   └── fixtures/
│
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── api/
│
├── docs/
│   └── adr/
│
├── alembic.ini
├── compose.yaml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

## 7. 身份与 Casdoor 集成

### 7.1 身份流程

```text
Client
  -> Casdoor Login
  -> Access Token
  -> MomentOneServer Authorization: Bearer <token>
  -> 验证签名、issuer、audience、exp
  -> 使用 issuer + sub 定位内部用户
```

服务端禁止接受客户端自由提交目标 `userId`。所有数据操作的用户身份必须来自验证后的 Token。

### 7.2 内部用户映射

建议用户表包含：

```text
users
├── id
├── identity_issuer
├── identity_subject
├── email
├── display_name
├── status
├── created_at
└── updated_at
```

唯一约束：

```text
UNIQUE(identity_issuer, identity_subject)
```

邮箱只作为资料字段，不作为跨系统身份主键。

### 7.3 Casdoor 待配置项

- Application；
- Client ID；
- Client Secret（仅机密客户端需要，静态 Web 不保存）；
- Issuer URL；
- Discovery URL；
- JWKS URL；
- Audience；
- Redirect URI；
- Logout Redirect URI；
- Allowed Origins；
- Access Token 有效期；
- Web Public Client 的 PKCE 设置。

## 8. 对象存储与 MinIO 集成

### 8.1 上传流程

```text
Client
  -> POST /v1/assets/upload-intents
  -> Server 校验用户、媒体类型和大小
  -> Server 创建 Asset 记录和 Object Key
  -> Server 返回短期 Presigned Upload URL
  -> Client 直传 MinIO
  -> POST /v1/assets/{assetId}/complete
  -> Server 验证对象并标记 ready
  -> Client 创建或更新 Moment，引用 assetId
```

### 8.2 安全规则

- Bucket 默认私有；
- 客户端不持有 MinIO Access Key / Secret Key；
- API 服务使用独立最小权限 Service Account；
- Object Key 只能由服务端生成；
- Presigned URL 必须短期有效；
- 限制 Content-Type、文件大小和支持的媒体类型；
- 日志不得记录签名 URL 中的敏感参数；
- 未完成上传需要生命周期清理；
- 媒体下载默认使用短期签名 URL；
- 删除 Moment 时通过引用计数或延迟清理处理媒体。

### 8.3 推荐对象路径

```text
users/{userId}/assets/{assetId}/original
users/{userId}/assets/{assetId}/thumbnail
```

数据库只保存对象元数据和引用关系，不保存图片或音频二进制大字段。

## 9. PostgreSQL 设计

### 9.1 首期核心表

```text
users
user_identities
devices
moments
moment_revisions
assets
moment_assets
idempotency_keys
pending_confirmations
user_configs
audit_events
```

后续阶段再增加：

```text
sync_cursors
sync_change_log
oauth_clients
access_grants
agent_connections
search_embeddings
```

### 9.2 Moment Domain v1 草案

```python
class MomentCategory(str, Enum):
    EXPERIENCE = "experience"
    HABIT = "habit"
    TRAVEL = "travel"
    FOOD = "food"
    GROWTH = "growth"
    EMOTION = "emotion"


class Moment:
    id: UUID
    user_id: UUID

    title: str
    description: str | None
    voice_input: str | None
    ai_summary: str | None

    category: MomentCategory
    tags: list[str]

    occurred_at: datetime
    timezone: str
    location: MomentLocation | None
    emotion: MomentEmotion | None

    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
```

正式字段需要通过 Phase 0 契约评审后冻结。

### 9.3 Revision

修改请求必须包含：

```text
expectedRevision
```

服务端执行条件更新：

```sql
UPDATE moments
SET
  revision = revision + 1,
  updated_at = now()
WHERE
  id = :moment_id
  AND user_id = :user_id
  AND revision = :expected_revision
  AND deleted_at IS NULL
RETURNING *;
```

Revision 不一致时返回：

```text
REVISION_CONFLICT
```

不得静默覆盖。

### 9.4 Idempotency

创建、修改、删除确认等写操作使用：

```text
Idempotency-Key
```

幂等记录存入 PostgreSQL，并通过唯一约束和事务防止重复执行。Redis 不作为幂等真相来源。

### 9.5 删除

删除使用 Tombstone：

```text
deleted_at
revision + 1
```

高风险删除采用两阶段操作：

```text
delete preview
  -> confirmationId + expiresAt + revision snapshot

delete confirm
  -> 校验用户、权限、过期、是否使用、revision
  -> 执行删除
```

确认状态存 PostgreSQL，以支持审计和防重放。

### 9.6 搜索

首期使用：

```text
结构化过滤
+ occurred_at 时间范围
+ category / tags
+ normalized_search_text
+ pg_trgm
```

中文内容不依赖默认英文分词。数据规模和搜索质量出现明确瓶颈后，再评估 OpenSearch、中文分词扩展或 pgvector。

## 10. REST API 草案

### 10.1 系统

```text
GET /healthz
GET /readyz
GET /version
```

### 10.2 当前用户

```text
GET /v1/me
GET /v1/me/config
PATCH /v1/me/config
```

### 10.3 Moment

```text
POST   /v1/moments
GET    /v1/moments
GET    /v1/moments/{momentId}
PATCH  /v1/moments/{momentId}
POST   /v1/moments/{momentId}/delete-preview
POST   /v1/moments/delete-confirm
```

查询参数包括：

```text
from
to
limit
cursor
category
tag
query
includeDeleted
```

普通客户端不允许设置 `includeDeleted=true`，该能力只供受控同步接口或管理流程使用。

### 10.4 媒体

```text
POST /v1/assets/upload-intents
POST /v1/assets/{assetId}/complete
GET  /v1/assets/{assetId}
POST /v1/assets/{assetId}/download-url
```

### 10.5 审计

```text
GET /v1/audit-events
```

首期可以先实现内部写入，读取接口在管理页面需要时开放。

## 11. 错误模型

稳定错误码：

```text
AUTH_REQUIRED
TOKEN_INVALID
SCOPE_DENIED
MOMENT_NOT_FOUND
TARGET_AMBIGUOUS
REVISION_CONFLICT
CONFIRMATION_REQUIRED
CONFIRMATION_EXPIRED
CONFIRMATION_USED
INVALID_ARGUMENTS
IDEMPOTENCY_CONFLICT
RATE_LIMITED
MEDIA_NOT_READY
MEDIA_TYPE_NOT_ALLOWED
MEDIA_TOO_LARGE
INTERNAL_ERROR
```

统一响应示例：

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "Moment 已被其他操作修改，请刷新后重试。",
    "requestId": "request-123",
    "details": {
      "expectedRevision": 2,
      "actualRevision": 3
    }
  }
}
```

生产环境不得返回 SQL、Token、密钥、内部文件路径或调用栈。

## 12. Redis 决策

### 12.1 首期不引入

Redis 不是首期阻塞依赖。以下数据必须保存在 PostgreSQL：

- Moment；
- Revision；
- Idempotency Key；
- Pending Confirmation；
- Audit Event；
- Sync Cursor；
- 用户配置；
- 媒体元数据。

### 12.2 引入条件

出现以下需求后再评估 Redis：

- 多个 API 实例需要共享限流状态；
- 高频热点数据缓存有明确收益；
- 需要可靠的异步媒体处理任务；
- 需要延迟任务、失败重试或任务队列；
- 单纯依赖 PostgreSQL 后台任务已成为可测量瓶颈。

即使引入 Redis，它也只能作为缓存、队列或短期协调组件，不能成为 Moment 的事实源。

## 13. 安全要求

### 13.1 网络

建议只公网暴露：

```text
HTTPS API
Casdoor HTTPS 登录入口
受控的 MinIO S3 API 入口
```

禁止直接公网暴露：

```text
PostgreSQL 5432
MinIO 管理控制台
Redis（未来如引入）
```

PostgreSQL 应通过私有网络、VPN、SSH Tunnel 或受控堡垒机管理。

### 13.2 Secret

以下信息不得提交 Git：

```text
DATABASE_URL
CASDOOR_CLIENT_SECRET
MINIO_ACCESS_KEY
MINIO_SECRET_KEY
JWT / OIDC 私密配置
```

仓库只提交 `.env.example`，生产 Secret 通过部署环境或 Secret Manager 注入。

### 13.3 日志

日志必须包含：

```text
requestId
route
method
statusCode
durationMs
internalUserId
errorCode
```

日志不得包含：

```text
Authorization Header
Access Token / Refresh Token
MinIO Secret
完整 Presigned URL 查询参数
图片和音频二进制
数据库密码
完整敏感请求体
```

### 13.4 数据权利

架构需为以下能力保留边界：

- 导出用户全部 Moment；
- 删除账号和云端数据；
- 清理媒体及派生资源；
- 查看访问审计；
- 撤销设备或 Agent 授权；
- 配置数据保留策略。

## 14. 测试策略

### 14.1 单元测试

覆盖不依赖数据库和 HTTP 的领域规则：

- Moment 字段校验；
- Revision；
- Idempotency；
- Cursor；
- 删除确认；
- 媒体状态；
- 权限策略；
- 错误映射。

### 14.2 集成测试

使用真实 PostgreSQL 测试：

- Alembic migration；
- Repository；
- 唯一约束；
- 事务；
- 乐观锁；
- 用户数据隔离；
- Tombstone；
- 搜索索引。

### 14.3 API 测试

使用 `httpx` 测试 FastAPI：

- Casdoor Token 验证适配；
- 当前用户解析；
- 请求校验；
- 状态码和错误码；
- Cursor 分页；
- 媒体上传流程。

### 14.4 契约测试

服务端与现有 Rokid 本地实现使用同一批 Fixture 验证：

```text
Moment Domain v1
Tool Contract v1
错误码
Revision
Cursor
Idempotency
ViewModel
```

建议 Fixture：

```text
contracts/fixtures/
├── moment-basic.json
├── moment-with-location.json
├── moment-with-media.json
├── moment-revision-conflict.json
├── moment-deleted.json
├── moments-list-page.json
└── error-cases.json
```

## 15. 可观测性和运行维护

首期至少实现：

- JSON 结构化日志；
- Request ID；
- `/healthz`；
- `/readyz`；
- 数据库连接检查；
- MinIO 连接检查；
- Casdoor Discovery/JWKS 可用性检查；
- API 延迟和错误计数；
- 数据库定期备份；
- MinIO 对象备份或版本策略；
- 从备份恢复到独立环境的演练。

生产容器建议一个 Uvicorn 进程对应一个容器，横向扩展由部署平台负责。每个实例独立配置数据库连接池上限，避免实例数增加时耗尽 PostgreSQL 连接。

## 16. 实施阶段

### Phase 0：工程和契约

交付：

- 初始化 Git 和 Python 工程；
- `pyproject.toml`；
- Ruff、Pyright、pytest；
- FastAPI 应用骨架；
- 配置管理；
- 统一错误模型；
- Dockerfile 和 Compose；
- Moment Domain v1；
- 契约 Fixture；
- 第一批 ADR。

完成标准：

```text
服务可启动
静态检查通过
测试框架可运行
契约 Fixture 可校验
```

### Phase 1：身份与 Moment Core

交付：

- Casdoor OIDC 验证；
- 用户自动映射；
- PostgreSQL migration；
- Moment 创建、详情、修改和列表；
- Revision；
- Idempotency；
- Cursor；
- Audit Event。

完成标准：

```text
用户只能访问自己的 Moment
创建可幂等重试
修改发生 Revision 冲突时不会覆盖
列表支持稳定分页
```

### Phase 2：媒体与搜索

交付：

- MinIO Upload Intent；
- 上传完成确认；
- Moment 与 Asset 关联；
- 短期下载地址；
- PostgreSQL 结构化搜索；
- `pg_trgm` 模糊搜索；
- 媒体清理策略。

完成标准：

```text
客户端不持有 MinIO 永久凭据
媒体默认私有
Moment 可安全关联图片和音频
中文查询达到首期可用标准
```

### Phase 3：删除、安全和运行能力

交付：

- Delete Preview / Confirm；
- Tombstone；
- 基础限流；
- 日志和健康检查；
- 数据备份与恢复演练；
- 数据导出和账号删除设计。

### Phase 4：眼镜同步

在本地 MVP 和领域契约稳定后实现：

- Device；
- Sync Cursor；
- Change Log；
- Tombstone 下发；
- Outbox 接口；
- Revision 冲突；
- 网络不可用恢复。

### Phase 5：只读 MCP

在身份、审计、Scope 和查询契约稳定后实现：

```text
moments_search
moments_list
moments_get
moments_count
reviews_daily
```

MCP Transport 只调用现有 Domain Service，不重复实现业务规则。

## 17. 首期验收标准

首期 Cloud Core 至少满足：

- 未认证请求无法读取 Moment；
- 用户 A 无法读取、修改或推断用户 B 的 Moment；
- 通过 API 创建的 Moment 可以查询、修改和分页展示；
- 同一 Idempotency Key 重试不会重复创建；
- Revision 不一致返回稳定冲突错误；
- 删除必须经过 Preview 和 Confirm；
- 媒体 Bucket 私有，客户端只使用短期签名 URL；
- PostgreSQL、MinIO 和 Casdoor 凭据不进入客户端；
- 日志不记录 Token 和媒体内容；
- 数据库 migration 可在空数据库完整执行；
- 单元、集成、契约和 API 测试通过；
- PostgreSQL 和 MinIO 数据存在备份及恢复说明。

## 18. 初始化前待确认信息

开始编写代码前需要提供或确认：

### PostgreSQL

```text
Host
Port
Database
Application Username
Migration Username（可选）
TLS Mode
是否允许创建 pg_trgm / pgcrypto extension
```

### Casdoor

```text
Issuer URL
Application / Client ID
Audience
JWKS 或 Discovery URL
开发环境 Redirect URI
Access Token Claims 示例（删除敏感值）
```

### MinIO

```text
Endpoint
Region（如有）
Bucket
Service Account 权限范围
是否启用 TLS
Web CORS Origin
单文件大小上限
允许的媒体类型
```

所有真实密钥只通过本地 `.env` 或部署 Secret 提供，不写入方案文档或 Git。

## 19. 当前架构决策摘要

1. `MomentOneServer` 是纯后端项目；
2. Web 前端使用独立静态 SPA；
3. 服务端采用 Python + FastAPI；
4. PostgreSQL 是唯一业务事实源；
5. Casdoor 是身份提供方，内部用户以 `issuer + subject` 映射；
6. MinIO 是私有媒体对象存储，客户端只使用短期签名 URL；
7. 首期不引入 Redis；
8. 采用模块化单体，不拆微服务；
9. REST、未来同步和 MCP 共用同一套领域服务；
10. 契约、权限、Revision、幂等、审计和删除安全优先于功能数量。
