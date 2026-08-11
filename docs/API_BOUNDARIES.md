# Web、Casdoor 与 Server API 边界

## 固定边界

| 能力 | 调用路径 | 原因 |
|---|---|---|
| OIDC 登录、退出、近期重新认证 | Web → Casdoor | Authorization Code + PKCE，由身份提供方直接完成 |
| 显示名称、密码、MFA 状态、登录会话 | Web → Casdoor（用户 Token） | 纯身份自助操作，不使用管理身份，不在 Server 制造同义代理 |
| 邮箱/手机号验证 | Web → Server → Casdoor | 需要 Challenge、唯一性检查、本地身份映射和审计 |
| 第三方身份绑定、解绑、冲突与合并 | Web → Server → Casdoor | 涉及内部 User、业务所有权和不可逆保护 |
| 头像 | Web → Server → 对象存储/Casdoor | 同时属于业务存储和身份展示，需要额度及一致性处理 |
| Moment、记账、习惯、设备、MCP | Web → Server | Moment One 业务事实源 |
| 统计、批量删除、导入导出 | Web → Server | 聚合、事务、幂等与批量执行不应在浏览器拼装 |

Web 不持有 Casdoor Client Secret。Web 直连 Casdoor 时只使用当前用户 Access Token；需要管理凭据、内部 User 映射或业务数据联动的操作必须经过 Server。

## 本轮收敛

- 删除历史代理端点：`POST /v1/account/sync`、`POST /v1/account/password`、`GET /v1/account/identities`、`DELETE /v1/account/sessions/{name}`。
- 将仅保存语言/时区的 `PATCH /v1/account/profile` 更名为 `PATCH /v1/account/preferences`。
- 批量删除从每条记录两次请求收敛为一次 Preview + 一次 Confirm。
- 首页月度汇总和记账分析改由 `/v1/insights/*` 一次返回，不再自动翻完全部历史记录。
- Excel 导入、导出和分类清空由 `/v1/data/bookkeeping/*` 服务端用例负责。

## 保留的多阶段协议

- Asset 上传保留 Upload Intent → 对象存储 PUT → Complete。它是预签名上传协议，不是前端临时拼装。
- 删除保留 Preview → Confirm。它是领域安全约束；批量操作同样保持两阶段。
- OIDC、身份 Link Session 和设备绑定保留跳转、回调或扫码等多阶段流程。
