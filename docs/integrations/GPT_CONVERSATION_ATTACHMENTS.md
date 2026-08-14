# GPT 对话附件无感导入契约

## 用户体验不变量

- 用户只在 GPT 对话中提交一次文件，不在 MCP Apps UI 中重复选择或上传。
- Agent 根据对话语义判断哪些既有会话附件适合当前记录；不相关附件不得带入。
- 上传申请、下载、校验、对象存储写入和 Asset 关联均为内部过程，不展示中间成功提示。
- 用户只看到最终的“记录成功（含附件数量）”或可行动的失败结果。

## GPT 接入

GPT Action 调用 `POST /v1/moments/from-openai-files`。请求继承普通 Moment 创建字段，额外包含官方约定的 `openaiFileIdRefs`；必须同时发送 `Idempotency-Key`。

GPT 编辑器导入 `docs/integrations/gpt-action.openapi.yaml`，认证选择 OAuth：Authorization URL 为 `/oauth/authorize`，Token URL 为 `/oauth/token`，scope 至少为 `moments.write`；Client ID/Secret 与 `MOMENT_ONE_GPT_ACTION_CLIENT_ID`、`MOMENT_ONE_GPT_ACTION_CLIENT_SECRET` 一致。该 confidential client 只接受 ChatGPT 官方 `/aip/g-*/oauth/callback` 回调地址。

`openaiFileIdRefs` 由 ChatGPT 自动填入，每项包含 `name`、`id`、`mime_type` 和五分钟有效的 `download_link`。Agent 只应选择与本次记录直接相关的附件。Server 校验来源后下载文件，转存至对象存储，创建 ready Asset，再通过 `assetIds` 与 Moment 关联。

Server 不保存临时下载地址，不接受任意远程 URL，不把上传中间状态作为面向用户的 UI。允许来源通过 `MOMENT_ONE_OPENAI_ATTACHMENT_ALLOWED_HOSTS` 配置，默认仅 `files.oaiusercontent.com`。

## 眼镜端后续规划

眼镜端由 Moment One 自己控制附件字节，后续采用“录制策略 + Agent 决策 + 后台上传队列”实现：

1. 用户按设置选择允许的记录媒介：图片、音频、视频，可配置默认形式与质量。
2. 录制结束后，本地生成稳定 `captureId`，保存文件、时间、设备与媒体元数据。
3. Agent 结合对话和采集上下文决定是否创建 Moment、选择哪些采集文件及记录类型。
4. 客户端在后台执行现有 upload-intent、对象存储 PUT、complete，再携带 ready `assetIds` 创建 Moment。
5. 以 `captureId` 派生幂等键；断网时进入加密离线队列，恢复后续传，成功后按保留策略清理本地文件。
6. UI 只呈现录制状态和最终记录结果；上传进度、重试与校验进入诊断日志，不打扰用户。

本阶段只记录规划，不修改 `MomentOneGlasses` 实现。
