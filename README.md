# Moment One Server

Moment One 的云端后端，采用 Python、FastAPI、PostgreSQL、Casdoor OIDC 和
MinIO/S3-compatible object storage。

完整方案见 [BACKEND_IMPLEMENTATION_PLAN.md](./BACKEND_IMPLEMENTATION_PLAN.md)。

核心设计文档：

- [文档索引](./docs/README.md)
- [Moment One 领域模型](./docs/domain/MOMENT_DOMAIN_MODEL.md)
- [设备绑定（Device Binding）设计](./docs/domain/DEVICE_BINDING.md)
- [PostgreSQL 与 MinIO 存储数据模型](./docs/data/STORAGE_DATA_MODEL.md)
- [ADR-0001：领域模型与存储边界](./docs/decisions/0001_DOMAIN_AND_STORAGE_BOUNDARIES.md)

## 当前状态

项目目前处于 Phase 0-2 阶段，已经包含：

- FastAPI 应用工厂；
- `/healthz`、`/readyz`、`/version`；
- 配置与结构化日志；
- 统一应用错误模型；
- SQLAlchemy / Alembic 基础；
- Casdoor 和 MinIO 适配边界；
- Moment Domain 和 Repository 接口示例；
- **Moment CRUD**（创建/查询/修改/软删除，乐观锁 + idempotencyKey）；
- **设备绑定**（Web 端发起 + 眼镜扫码 + OAuth 2.1 Token 端点 + RS256 JWT 双 token）；
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

## 分支策略

```text
main  ← 受保护的生产分支，只接受 Pull Request 合并；合并后自动触发 CICD 部署生产
dev   ← 开发集成分支，日常开发与 PR 的目标分支
```

工作流：

1. 本地在 `dev` 分支开发（或从 `dev` 切出 `feat/*` 特性分支）；
2. 开发完成后提 Pull Request 到 `dev`，CI 跑测试通过后合并；
3. `dev` 稳定后提 Pull Request `dev → main`，CI 再次跑测试通过后合并；
4. 合并到 `main` 后自动触发：构建镜像 → 推送 GHCR → SSH 部署生产 → 健康检查。

`main` 分支建议开启分支保护：要求 PR、要求 CI 通过、禁止直接 push、禁止 force push。

## CI/CD

GitHub Actions 配置位于 `.github/workflows/ci.yml`，按分支触发不同 Job：

| Job | 触发条件 | 作用 |
|---|---|---|
| `quality-and-database` | PR + push main/dev | Ruff、Pyright、单元/API 测试、Alembic 迁移验证、PostgreSQL 集成测试 |
| `container` | PR + push main/dev | 构建生产 Docker 镜像，启动容器并调用 `/healthz` 做冒烟测试 |
| `publish` | 仅 push main | 构建镜像并推送到 GHCR（`ghcr.io/sir-yuanshuai/momentoneserver`），打 `latest` + `sha-xxxx` + 分支名 tag |
| `deploy` | 仅 push main | SSH 登录生产服务器，拉取最新镜像、自动跑数据库迁移、重启 api 容器、健康检查 |

数据库迁移在 `deploy` 阶段自动执行 `alembic upgrade head`，只动表结构不动数据。CI 在 `quality-and-database` 阶段已验证迁移可执行，部署时再应用到生产库。

CI 当前不调用远程 Casdoor 或 MinIO/S3，也不需要相关真实 Secret。后续增加远程集成测试时，建议使用独立的测试租户、测试 Bucket 和受保护的 GitHub Environment Secrets，且不要让来自 Fork 的 Pull Request 获得这些凭据。

## 生产部署

生产环境通过 1Panel 面板管理，PostgreSQL 由 1Panel 应用商店提供，API 容器从 GHCR 拉取镜像运行。

### 一次性服务器准备

1. **1Panel 安装 PostgreSQL 17**（应用商店）；
2. **在 1Panel PostgreSQL 管理界面创建项目数据库与账号**：
   - 数据库名：`moment_one`
   - 用户名：`moment_one`
   - 设置强密码并记录
3. **在服务器创建部署目录**（建议 `/opt/moment-one`）：
   ```bash
   sudo mkdir -p /opt/moment-one
   sudo chown -R $USER:$USER /opt/moment-one
   ```
4. **从仓库拷贝生产配置到部署目录**（或用 1Panel 文件管理上传）：
   - `compose.prod.yml`（来自本仓库，提交后可在 GitHub raw 下载）
5. **在部署目录创建 `.env`**（真实凭据，不进 git，1Panel 文件管理维护）：
   ```bash
   MOMENT_ONE_ENV=production
   MOMENT_ONE_DEBUG=false
   MOMENT_ONE_LOG_LEVEL=INFO
   MOMENT_ONE_API_PREFIX=/v1
   MOMENT_ONE_ALLOWED_ORIGINS=https://your-web-domain

   # 1Panel PostgreSQL。host.docker.internal 指向宿主机。
   MOMENT_ONE_DATABASE_URL=postgresql+psycopg://moment_one:你的强密码@host.docker.internal:5432/moment_one

   # Casdoor OIDC
   MOMENT_ONE_CASDOOR_ISSUER=https://auth.your-domain.com
   MOMENT_ONE_CASDOOR_AUDIENCE=moment-one-api
   MOMENT_ONE_CASDOOR_JWKS_URL=https://auth.your-domain.com/.well-known/jwks.json

   # MinIO / S3
   MOMENT_ONE_S3_ENDPOINT_URL=https://storage.your-domain.com
   MOMENT_ONE_S3_REGION=us-east-1
   MOMENT_ONE_S3_BUCKET=moment-one-media
   MOMENT_ONE_S3_ACCESS_KEY=你的access-key
   MOMENT_ONE_S3_SECRET_KEY=你的secret-key
   ```
6. **服务器登录 GHCR**（拉私有镜像需要，只需 `read:packages` 权限的 PAT）：
   ```bash
   echo "你的GitHub_PAT" | docker login ghcr.io -u 你的GitHub用户名 --password-stdin
   ```
7. **1Panel 配置网站/反向代理**：域名 → `127.0.0.1:8000`，申请 Let's Encrypt 证书上 HTTPS。

### GitHub Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret 名 | 说明 | 示例 |
|---|---|---|
| `SERVER_HOST` | 服务器 IP 或域名 | `1.2.3.4` |
| `SERVER_USER` | SSH 用户 | `root` 或专用部署用户 |
| `SERVER_PORT` | SSH 端口 | `22` |
| `SERVER_SSH_KEY` | SSH 私钥（完整内容，含 BEGIN/END 行） | `-----BEGIN OPENSSH PRIVATE KEY-----...` |
| `SERVER_DEPLOY_PATH` | 服务器部署目录绝对路径 | `/opt/moment-one` |

SSH 密钥建议专门为部署生成一对（`ssh-keygen -t ed25519 -f ~/.ssh/moment_one_deploy`），公钥追加到服务器 `~/.ssh/authorized_keys`，私钥粘贴到 `SERVER_SSH_KEY`。

### 首次部署

合并第一个 PR 到 `main` 后，CI 自动完成：
1. 构建并推送镜像到 GHCR；
2. SSH 到服务器 `docker compose -f compose.prod.yml pull`；
3. `docker compose -f compose.prod.yml run --rm api alembic upgrade head`（首次会在空库自动建 `users` 和 `moments` 表）；
4. `docker compose -f compose.prod.yml up -d api`；
5. `curl /healthz` 健康检查。

### 手动紧急部署

CI 自动部署失败或需要重新部署当前镜像时，从本地执行：

```bash
./scripts/deploy.sh
```

需要本地有 `~/.moment-one-deploy.env` 或导出 `SERVER_HOST` / `SERVER_USER` / `SERVER_PORT` / `SERVER_DEPLOY_PATH` 环境变量，SSH 走本地 `~/.ssh` 配置。

## Secret 规则

以下信息不能提交到 Git：

```text
数据库生产连接串
Casdoor 凭据或私密配置
MinIO/S3 Access Key 和 Secret Key
Token、JWT 私钥、Presigned URL
```

本地真实配置放在被忽略的 `.env` 中；测试、Staging 和生产配置由 CI/CD 或部署平台 Secret 注入。
