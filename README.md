# Moment One Server

Moment One 的云端后端，采用 Python、FastAPI、PostgreSQL、Casdoor OIDC 和
MinIO/S3-compatible object storage。

完整方案见 [BACKEND_IMPLEMENTATION_PLAN.md](./BACKEND_IMPLEMENTATION_PLAN.md)。

核心设计文档：

- [文档索引](./docs/README.md)
- [Moment One 领域模型](./docs/domain/MOMENT_DOMAIN_MODEL.md)
- [PostgreSQL 与 MinIO 存储数据模型](./docs/data/STORAGE_DATA_MODEL.md)
- [ADR-0001：领域模型与存储边界](./docs/decisions/0001_DOMAIN_AND_STORAGE_BOUNDARIES.md)

## 当前状态

项目目前处于 Phase 0 工程骨架阶段，已经包含：

- FastAPI 应用工厂；
- `/healthz`、`/readyz`、`/version`；
- 配置与结构化日志；
- 统一应用错误模型；
- SQLAlchemy / Alembic 基础；
- Casdoor 和 MinIO 适配边界；
- Moment Domain 和 Repository 接口示例；
- Ruff、Pyright、pytest；
- Dockerfile、Docker Compose 和 GitHub Actions CI。

本地 PostgreSQL 由 Docker Compose 提供。Casdoor 和 MinIO/S3 使用远程服务，真实连接信息只写入本地 `.env` 或部署平台 Secret。

## 环境要求

- Python 3.13 或 3.14，推荐使用仓库 `.python-version` 中的版本；
- Docker Engine 或 Docker Desktop；
- Docker Compose。仓库 Makefile 会自动检测 `docker compose` 或 `docker-compose`。

## 首次初始化

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
make install
cp .env.example .env
```

然后编辑 `.env`，填写远程 Casdoor 和 MinIO/S3 配置。不要把 `.env` 提交到 Git。

## 本地开发

### 1. 启动 PostgreSQL

```bash
make db-up
```

默认在 `127.0.0.1:5432` 启动 PostgreSQL，并将数据保存在 Docker volume 中。

查看数据库日志：

```bash
make db-logs
```

### 2. 执行数据库迁移

```bash
make migrate
```

新增模型后生成迁移：

```bash
make migrate-new name="add moments table"
make migrate
```

### 3. 启动 API 热更新服务

```bash
make dev
```

常用地址：

```text
Swagger UI: http://127.0.0.1:8000/docs
Health:     http://127.0.0.1:8000/healthz
Readiness:  http://127.0.0.1:8000/readyz
Version:    http://127.0.0.1:8000/version
```

当前 `/readyz` 只检查应用自身。接入真实业务能力后，应增加 PostgreSQL、Casdoor JWKS 和 MinIO/S3 检查。

## 测试与质量检查

```bash
make format             # 格式化并自动修复
make lint               # Ruff 格式和 lint 检查
make type               # Pyright strict 类型检查
make test               # 不依赖外部设施的单元/API 测试
make test-integration   # 需要本地 PostgreSQL
make check              # lint + type + 非集成测试
```

推荐提交前运行：

```bash
make check
make db-up
make migrate
make test-integration
```

## 使用完整 Compose 环境

构建并启动 API 和 PostgreSQL：

```bash
make compose-up
make compose-migrate
```

查看日志或停止：

```bash
make compose-logs
make compose-down
```

重建本地数据库并删除原有数据：

```bash
make db-reset
```

`db-reset` 会删除 PostgreSQL Docker volume，属于破坏性操作。

## CI

GitHub Actions 配置位于 `.github/workflows/ci.yml`，在 Pull Request 和推送到 `main` 时运行两个 Job：

1. 启动临时 PostgreSQL，执行 Ruff、Pyright、单元/API 测试、Alembic migration 和数据库集成测试；
2. 构建生产 Docker 镜像，启动容器并调用 `/healthz` 做冒烟测试。

CI 当前不调用远程 Casdoor 或 MinIO/S3，也不需要相关真实 Secret。后续增加远程集成测试时，建议使用独立的测试租户、测试 Bucket 和受保护的 GitHub Environment Secrets，且不要让来自 Fork 的 Pull Request 获得这些凭据。

## Secret 规则

以下信息不能提交到 Git：

```text
数据库生产连接串
Casdoor 凭据或私密配置
MinIO/S3 Access Key 和 Secret Key
Token、JWT 私钥、Presigned URL
```

本地真实配置放在被忽略的 `.env` 中；测试、Staging 和生产配置由 CI/CD 或部署平台 Secret 注入。
