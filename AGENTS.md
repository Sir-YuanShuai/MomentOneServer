# MomentOneServer — Agent 开发规范

> 本文件规范 AI Agent 在 `MomentOneServer` 仓库中的开发行为。
> 业务领域规则见根目录 `../AGENTS.md`，不在本文重复。

## 权威文档优先级

修改代码或数据库前，先确认以下文档的约束：

1. `contracts/schemas/moment.v1.json` — Moment 领域 JSON Schema（字段冻结）
2. `docs/data/STORAGE_DATA_MODEL.md` — 数据库与存储设计（目标表结构）
3. `docs/domain/MOMENT_DOMAIN_MODEL.md` — 领域模型详解
4. `docs/domain/DEVICE_BINDING.md` — 设备绑定与 OAuth Token 契约（三端共享）
5. `IMPLEMENTATION_PROGRESS.md` — 当前实现进度（高频更新）
6. `DATABASE.md` — 数据库稳定参考（认证、配置、迁移命令）

当 `IMPLEMENTATION_PROGRESS.md` 与 `STORAGE_DATA_MODEL.md` 存在差异时，以 `STORAGE_DATA_MODEL.md` 为目标设计。

## 常用命令

```bash
# 环境初始化
make install                          # 安装依赖
make db-up                            # 启动 PostgreSQL

# 开发
make dev                              # 启动开发服务器 (127.0.0.1:8000)

# 质量检查（提交前必须全部通过）
make check                            # = lint + type + test
make lint                             # ruff format --check + ruff check
make type                             # pyright
make test                             # pytest (非集成测试)
make test-integration                 # pytest (集成测试，需 PostgreSQL)

# 格式化
make format                           # ruff format + ruff check --fix

# 数据库迁移
make migrate                          # alembic upgrade head
make migrate-new name="add xxx table" # 创建新迁移

# Docker
make compose-up                       # 构建并启动 api + postgres
make compose-migrate                  # 容器内执行迁移
```

## 提交前检查清单

每次提交前必须运行 `make check` 并全部通过。如果 CI 失败：

1. 先运行 `make format` 修复格式
2. 再运行 `make lint` / `make type` / `make test` 逐项排查
3. 不要修改 CI 配置来绕过检查

## 数据库表维护规则

### 新增表或字段

1. **先更新设计文档**：在 `docs/data/STORAGE_DATA_MODEL.md` 中添加表定义（列、类型、约束、索引）
2. **创建 Alembic 迁移**：`make migrate-new name="add xxx table"`
3. **实现 ORM 模型**：在 `app/infrastructure/database/models/` 中添加或更新模型
4. **更新实现进度**：在 `IMPLEMENTATION_PROGRESS.md` 中标记表状态为"已实现"
5. **添加测试**：在 `tests/` 中添加对应的模型和 Repository 测试

### 修改已有表

1. **不允许直接修改已有迁移文件**——必须新建迁移
2. **provenance 字段创建后不可篡改**——不允许添加修改 provenance 的迁移
3. **删除字段需要两阶段**：先标记废弃（保留列），确认无依赖后再删除

### 迁移文件命名

```
NNNN_description.py
```

- `NNNN`：Alembic 自动生成的序号
- `description`：简短描述，snake_case，如 `add_device_bindings_table`

### Fixture 同步

修改 `moment.v1.json` Schema 后，必须同步更新 `contracts/fixtures/` 下的所有 fixture 文件，并确保 fixture 通过 Schema 验证。

## 代码结构约定

- **领域层** (`app/domain/`)：纯领域逻辑，不依赖基础设施
- **Repository 接口** (`app/domain/repositories/`)：定义接口，不实现
- **基础设施** (`app/infrastructure/`)：数据库、对象存储、外部服务适配
- **API 层** (`app/api/`)：FastAPI 路由和请求/响应模型
- **配置** (`app/config.py`)：环境变量和配置加载

## 禁止事项

- 禁止在领域层引入 SQLAlchemy、FastAPI 或其他框架依赖
- 禁止在 API 层直接操作数据库——必须通过 Repository 接口
- 禁止提交 `.env` 文件或包含真实密钥的配置
- 禁止跳过 `make check` 直接提交
- 禁止修改 `moment.v1.json` 的已有字段定义（Schema 已冻结，只能新增可选字段）

## AI / Agent 添加通知

AI 不得直接写通知表或调用 Push Provider。定时提醒使用 REST `POST /v1/reminders` 或 MCP
`reminder_create`；账号安全事件通过通知模块的事务内服务接入。完整规则与示例见
`docs/NOTIFICATION_INTEGRATION_FOR_AGENTS.md`。
