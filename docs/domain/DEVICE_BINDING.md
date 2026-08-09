# 设备绑定（Device Binding）设计

> 文档状态：Current  |  更新日期：2026-08-04
>
> 适用范围：Moment One Cloud Core / `MomentOneServer`、`MomentOneWeb`、`MomentOneGlasses`。
> 本文是 Phase 2 设备绑定模块的权威契约，三端实现必须对齐。

## 1. 背景与目标

Moment One 三端架构：

- **Web（MomentOneWeb）**：用户已通过 Casdoor OIDC 登录，持有 Casdoor ID Token。
- **眼镜（MomentOneGlasses）**：运行在 Rokid AIUI 沙箱，无浏览器、无 Casdoor 登录态，需要长期离线可用 token。
- **Server（MomentOneServer）**：唯一事实源，签发与撤销眼镜端 token。

眼镜端不能直接走 Casdoor OIDC（无浏览器、无 redirect URI），因此引入**设备绑定**：
用户在 Web 端发起绑定 → 眼镜扫码 → Server 校验并签发**双 token**，眼镜端此后用 token 调 Server。

### 设计目标

1. **零 Casdoor 依赖**：眼镜端只持有 Server 签发的 JWT，不接触 Casdoor。
2. **可撤销**：用户可在 Web 端随时撤销某台设备的访问权。
3. **滚动续期**：access_token 短期、refresh_token 长期，刷新时滚动续期，无需重新扫码。
4. **MCP 兼容**：access_token 同时是未来 MCP Server 的 bearer，一套密钥两端通用。
5. **最小暴露面**：binding_code 一次性、5 分钟过期；私钥只在 Server 内存在。

## 2. 整体流程

```text
┌─────────┐                ┌─────────┐                ┌─────────┐
│   Web   │                │  Server │                │ Glasses │
└────┬────┘                └────┬────┘                └────┬────┘
     │  1. POST /v1/device/bindings │                        │
     │  (Casdoor Bearer)            │                        │
     │─────────────────────────────>│                        │
     │  2. binding_code + qr_payload │                        │
     │<─────────────────────────────│                        │
     │  3. 渲染二维码                │                        │
     │  ┌──────┐                    │                        │
     │  │ QR   │                    │  4. 扫码得到 binding_code │
     │  └──────┘                    │<───────────────────────│
     │                              │  5. POST /oauth/token    │
     │                              │     grant_type=qr-binding│
     │                              │     binding_code=xxx     │
     │                              │     device_id=yyy        │
     │                              │────────────────────────>│
     │                              │  6. access_token         │
     │                              │     refresh_token        │
     │                              │<────────────────────────│
     │                              │                        │
     │  7. GET /v1/device/bindings  │                        │
     │  (Casdoor Bearer)            │  8. 业务请求带 access_token │
     │─────────────────────────────>│<───────────────────────│
     │  9. 看到新设备已绑定          │                        │
     │<─────────────────────────────│                        │
```

### 步骤说明

1. **Web 发起绑定**：用户在 Web 端点击"绑定新设备"，Web 用 Casdoor ID Token 调 `POST /v1/device/bindings`。
2. **Server 生成 binding_code**：随机 22 字符 URL-safe，5 分钟过期，写入 `binding_codes` 表。
3. **Web 渲染二维码**：内容为 `momentone://bind?code=<binding_code>`。
4. **眼镜扫码**：调用系统相机扫出 `binding_code`。
5. **眼镜换 token**：调 `POST /oauth/token`，`grant_type=urn:momentone:oauth:grant-type:qr-binding`。
6. **Server 校验 + 签发**：
   - 校验 binding_code 有效、未使用、未过期；
   - 创建 `devices` 记录（若 device_id 不存在）；
   - 创建 `device_bindings` 记录（status=active）；
   - 用 RS256 私钥签 access_token + refresh_token；
   - 标记 binding_code 已使用；
   - 返回双 token。
7. **Web 列表刷新**：看到新设备出现在已绑定列表。
8. **眼镜调业务 API**：`Authorization: Bearer <access_token>`。

## 3. 双 Token 设计

### 3.1 access_token

| 属性 | 值 |
|---|---|
| 算法 | RS256（私钥签、公钥验） |
| issuer | `settings.jwt_issuer`（如 `https://api.momentone.app`） |
| audience | `settings.jwt_audience`（如 `moment-one-api`） |
| subject | `user_id`（UUID 字符串） |
| TTL | `settings.access_token_ttl_seconds`（默认 3600 = 1 小时） |
| claims | `sub`, `iss`, `aud`, `iat`, `nbf`, `exp`, `jti`, `scope`, `binding_id`, `device_id` |

`scope` 是空格分隔字符串，如 `moments.read moments.write`。
`jti` 是 UUID，写入 `device_bindings.access_token_jti`，用于防重放和滚动续期判定。

> **权限管理（MCP 式）**：scope 命名与 MCP 工具一致（`moments.read` / `moments.write` /
> `moments.delete`，点号分隔）。历史冒号命名（`moments:read`）在边界处自动规范化
> （`mcp.scope.normalize_scope_names`，回填迁移 0015）。MCP Server 验证时**以
> `device_bindings.scope` 记录为权限事实源**（与 `mcp_authorizations` 同模型）：Web 端
> 通过 `PATCH /v1/device/bindings/{id}` 调整读写权限后，下一次 MCP 调用即实时生效，
> 无需重新扫码；refresh 也以绑定记录为准重签 scope。

### 3.2 refresh_token

| 属性 | 值 |
|---|---|
| 算法 | RS256 |
| TTL | `settings.refresh_token_ttl_seconds`（默认 2592000 = 30 天） |
| claims | `sub`, `iss`, `aud`, `iat`, `nbf`, `exp`, `jti`, `binding_id`, `device_id` |
| 用途 | 仅用于 `POST /oauth/token` `grant_type=refresh_token` |

refresh_token 不带 `scope`，刷新时 scope 以 `device_bindings.scope` 为准（用户可在 Web 端调 PATCH 改 scope）。

### 3.3 滚动续期

每次 `refresh_token` 换新 access_token 时：

1. 校验 refresh_token 签名 + 未过期 + jti 在 `device_bindings.refresh_token_jti`；
2. 校验 binding status=active；
3. 签发新 access_token（新 jti、新 exp）；
4. **不签发新 refresh_token**（refresh_token 保持原值直到自然过期或被撤销）；
5. 更新 `device_bindings.access_token_jti` + `access_token_expires_at` + `last_active_at`。

> 设计取舍：refresh_token 不滚动，避免"永久在线"。30 天后眼镜必须重新扫码绑定。
> 如未来需要永久在线，可改为 refresh_token 也滚动，但需引入 refresh_token rotation 防重放。

### 3.4 撤销

- **用户主动撤销**：`DELETE /v1/device/bindings/{id}` → status=revoked、revoked_at=now、jti 清空。当前 access_token 在 exp 前仍可用（无 token 黑名单），但 refresh 立即失败。
- **refresh_token 自然过期**：30 天后 exp 到期，眼镜端收到 401，需引导用户重新扫码。
- **未来增强**：如需即时踢下线，可引入 Redis jti 黑名单 + access_token 短 TTL（15 分钟内自然失效）。

### 3.5 统一授权模型（眼镜 = MCP 客户端的一种）

> 2026-08-06 起，设备权限管理并入 **MCP 授权模型**（`mcp_authorizations`），
> 不再维护两套平行的权限模型（避免 scope 命名/口径漂移）。

| 角色 | 记录 | 说明 |
|---|---|---|
| 权限事实源 | `mcp_authorizations` | `client_type` 区分 `mcp`（Web OAuth 客户端）与 `glasses`（眼镜设备，`client_id=glasses:{device_id}`）；scope/status 由 Web 端统一管理，调整后**下一次调用实时生效** |
| 设备生命周期 | `device_bindings` | 仅保留 token 管理（refresh_token_hash、绑定状态、设备信息）；`scope` 列为 legacy 镜像 |

- **扫码绑定 = 新增一条 glasses 授权**：`complete_binding` 创建/更新 `mcp_authorizations`（已有 active 授权时保留用户已配置 scope，重绑不覆盖）；
- **权限校验**：MCP Server 验证眼镜 token 时按 `client_id=glasses:{device_id}` 读授权记录 scope（存量无授权记录回退 `device_bindings.scope`，迁移 0015/0016 已回填）；
- **撤销/删除设备**：`DELETE /v1/mcp/authorizations/{id}`（glasses 类型）同步撤销设备绑定；`DELETE /v1/device/bindings/{id}` 同步撤销授权记录；
- **Web 端管理**：设置页「授权与设备」统一列表（眼镜设备 + MCP 客户端同款读写权限开关）。

### 3.6 订阅、设备数量与远程调用额度

DeviceBinding 表示授权关系，不代表用户天然拥有无限设备和无限远程调用。服务端在以下时点读取内部 User 的 Entitlement/Quota：

1. 创建绑定会话：检查 `access.active_glasses`；
2. 完成绑定：再次原子检查，防止并发超过设备数；
3. 眼镜调用 REST/MCP：按业务操作计入用户统一额度；
4. Token 刷新：检查 User 状态、Binding 状态和计划是否仍允许保留已有设备；
5. 计划降级：不自动删除已有绑定，但若超过新计划上限则禁止新增；管理员或用户可选择撤销多余设备。

建议默认：

| 计划 | 活跃眼镜 | 活跃 MCP 客户端 | 远程 Tool/月 | 写 Tool/月 | `agent_plan`/日 |
|---|---:|---:|---:|---:|---:|
| Free | 1 | 1 | 1,000 | 100 | 30 |
| Plus | 3 | 5 | 10,000 | 2,000 | 300 |
| Pro | 10 | 20 | 100,000 | 20,000 | 3,000 |

计量规则：

- 眼镜和第三方 MCP Client 共享该用户额度；
- 本地离线 Moment 创建不计远程 Tool；同步、上传和服务端 Tool 执行时计量；
- REST 与 MCP 对同一幂等业务操作不重复计量；
- 初始化、`tools/list`、Capability 协商和 Token 刷新不计商业 Tool 额度，但受安全速率限制；
- A2UI/Text/structuredContent 是同一结果的不同表示，只计一次；
- 额度耗尽时眼镜保留本地数据，禁止受限远程动作并返回稳定错误和 `resetAt`；
- 设备数和调用额度以数据库为事实源，不能只写入 30 天 refresh_token 后长期信任 Token Claim。

相关契约：`../../../docs/contracts/ENTITLEMENTS_AND_LIMITS.md`。

## 4. API 契约

### 4.1 Web 端（Casdoor Bearer 鉴权）

#### POST /v1/device/bindings — 创建绑定会话

```http
POST /v1/device/bindings
Authorization: Bearer <Casdoor ID Token>
Content-Type: application/json

{
  "device_name": "Rokid Max",
  "scope": ["moments.read", "moments.write"]
}
```

响应 `201`：

```json
{
  "binding_code": "aBcDeFgHiJkLmNoPqRsTuV",
  "qr_payload": "momentone://bind?code=aBcDeFgHiJkLmNoPqRsTuV",
  "expires_at": "2026-08-04T12:00:05+00:00",
  "scope": ["moments.read", "moments.write"]
}
```

`scope` 可省略，默认 `("moments.read", "moments.write")`。
`device_name` 可省略，眼镜端在换 token 时可补填。

#### GET /v1/device/bindings — 列出已绑定设备

```http
GET /v1/device/bindings
Authorization: Bearer <Casdoor ID Token>
```

响应 `200`：

```json
[
  {
    "id": "uuid-binding-id",
    "device_id": "rokid-serial-xxx",
    "scope": ["moments.read", "moments.write"],
    "status": "active",
    "bound_at": "2026-08-04T12:00:10+00:00",
    "last_active_at": "2026-08-04T15:30:00+00:00",
    "revoked_at": null
  }
]
```

#### PATCH /v1/device/bindings/{id} — 调整 scope

```http
PATCH /v1/device/bindings/{binding_id}
Authorization: Bearer <Casdoor ID Token>
Content-Type: application/json

{ "scope": ["moments.read"] }
```

响应 `200`：返回更新后的 `DeviceBindingResponse`。
下次眼镜刷新 token 时拿到的是新 scope。

#### DELETE /v1/device/bindings/{id} — 撤销绑定

```http
DELETE /v1/device/bindings/{binding_id}
Authorization: Bearer <Casdoor ID Token>
```

响应 `204`。当前 access_token 在 exp 前仍可用，refresh 立即失败。

### 4.2 眼镜端（OAuth 2.1 Token 端点，无 Casdoor 鉴权）

#### POST /oauth/token — 换 token / 刷新 token

请求体为 `application/x-www-form-urlencoded`（OAuth 2.1 规范）。

**绑定**：

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:momentone:oauth:grant-type:qr-binding
&binding_code=aBcDeFgHiJkLmNoPqRsTuV
&device_id=rokid-serial-xxx
&device_name=Rokid+Max
&device_type=rokid-glasses
```

**刷新**：

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&refresh_token=eyJhbGciOi...
```

响应 `200`（RFC 6749 §5.1）：

```json
{
  "binding_id": "uuid-binding-id",
  "access_token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "moments.read moments.write"
}
```

错误响应 `400` / `401`（RFC 6749 §5.2）：

```json
{
  "detail": {
    "code": "INVALID_BINDING_CODE",
    "message": "绑定码已过期或无效，请重新扫码。"
  }
}
```

错误码：

| code | HTTP | 说明 |
|---|---|---|
| `INVALID_REQUEST` | 400 | 缺少必需参数 |
| `UNSUPPORTED_GRANT_TYPE` | 400 | grant_type 不支持 |
| `INVALID_BINDING_CODE` | 400 | binding_code 不存在/已用/已过期 |
| `INVALID_REFRESH_TOKEN` | 401 | refresh_token 签名无效/已过期/binding 已撤销 |
| `DEVICE_ALREADY_BOUND` | 409 | 该 user 对该 device 已有 active binding |

## 5. 数据模型

详见 [IMPLEMENTATION_PROGRESS.md](../../IMPLEMENTATION_PROGRESS.md#devices) 的 `devices` / `device_bindings` / `binding_codes` 三表。

关键约束：

- `device_bindings (user_id, device_id)` 在 `status='active'` 下唯一（一个用户对同一设备只能有一份有效绑定）。
- `binding_codes.code` 是 PK，一次性使用。
- `access_token_jti` / `refresh_token_jti` UNIQUE，防重放。

## 6. JWT 密钥配置

### 6.1 生成密钥对

```bash
# 私钥（Server 持有，签 token）
openssl genpkey -algorithm RSA -out jwt_private.pem -pkeyopt rsa_keygen_bits:2048

# 公钥（验证方持有，可对外公开）
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem
```

### 6.2 环境变量

```bash
# .env
MOMENT_ONE_JWT_PRIVATE_KEY_PATH=/etc/momentone/jwt_private.pem
MOMENT_ONE_JWT_PUBLIC_KEY_PATH=/etc/momentone/jwt_public.pem
MOMENT_ONE_JWT_ISSUER=https://api.momentone.app
MOMENT_ONE_JWT_AUDIENCE=moment-one-api
MOMENT_ONE_ACCESS_TOKEN_TTL_SECONDS=900
MOMENT_ONE_REFRESH_TOKEN_TTL_SECONDS=2592000
```

### 6.3 密钥轮换

- 私钥泄露时立即换新密钥对，更新 `device_bindings` 全部 status=revoked，强制所有眼镜重新扫码。
- 公钥可对 MCP Server / 其他验证方公开。
- 当前不支持多密钥并行验证（kid header），未来如需无缝轮换可引入 kid + JWKS endpoint。

## 7. 安全考量

1. **binding_code 防爆破**：22 字符 URL-safe（约 131 bit 熵），5 分钟过期，一次性使用。Server 应对该端点限流（未来增强）。
2. **私钥隔离**：私钥只在 Server 进程内存，不进 git、不进日志、不下发眼镜。
3. **scope 最小化**：Web 端默认下发 `moments.read moments.write`，未来可按设备类型收窄。
4. **device_id 由眼镜生成**：建议用设备序列号或首次启动生成的 UUID，Server 不主动生成，避免被伪造绑定到任意 device_id。
5. **无 Casdoor 触达**：眼镜端 token 完全由 Server 签发，Casdoor 故障不影响已绑定眼镜。
6. **refresh_token 不滚动**：30 天硬上限，避免永久在线风险。
7. **订阅额度不写死在设备 Token**：Server 每次业务请求结合 User、Binding、Scope、Entitlement 和 Quota 判断。
8. **离线优先**：云端额度不足不能删除眼镜本地记录，恢复网络后提示清理或升级。

## 8. 三端实现进度

| 端 | 模块 | 状态 |
|---|---|---|
| Server | `app/modules/devices/` + JWT + binding_codes + OAuth 端点 | 已实现（Phase 1 完成） |
| Web | 扫码绑定页面 + 已绑定设备管理 | 待实现（Phase 2） |
| Glasses | 扫码 + token 存储 + token 刷新 + 业务请求带 token | 待实现（Phase 3） |

## 9. 未来增强（不在当前 Phase）

- **MCP Server**：复用 access_token 作为 MCP bearer，同一 RS256 密钥。
- **JWKS endpoint**：`GET /.well-known/jwks.json` 暴露公钥，供 MCP Server / 第三方验证。
- **token 黑名单**：Redis 维护撤销的 jti，实现 access_token 即时失效。
- **设备指纹**：device_id + device_type + user_agent 联合识别异常绑定。
- **多设备类型**：scope 按设备类型模板化（眼镜、手机、手表默认 scope 不同）。
