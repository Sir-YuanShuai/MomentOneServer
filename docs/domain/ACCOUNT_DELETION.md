# 账号永久注销

> 状态：Implemented（2026-08-09）

## 安全流程

1. 用户必须使用 Casdoor 登录态发起 `POST /v1/account/delete-preview`；
2. Server 返回 Moment、Asset、习惯、设备和 MCP 授权数量，以及 10 分钟确认票据；
3. 用户在界面输入“永久注销”；
4. `POST /v1/account/delete-confirm` 要求 `Idempotency-Key`，并校验 Casdoor Token 的 `iat` 在最近 5 分钟内；
5. Token 不够新时返回 `REAUTHENTICATION_REQUIRED`，前端通过 `prompt=login&max_age=0` 让 Casdoor 重新验证密码；
6. Server 先删除对象存储原件/缩略图，再删除用户业务数据和内部 User。

## 不变量

- Moment One 不接收、不保存 Casdoor 密码；
- 对象存储不可用且用户存在 Asset 时拒绝注销，避免数据库删除后留下不可追踪对象；
- 删除不可恢复，不转为软删除；
- 管理员不能代替用户绕过近期重新认证；
- 删除后旧 Token 因本地 User 不存在而立即失效。
