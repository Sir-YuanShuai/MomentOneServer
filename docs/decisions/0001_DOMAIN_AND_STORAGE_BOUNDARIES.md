# ADR-0001：领域模型与存储边界

> 状态：Accepted for Phase 0  
> 决策日期：2026-08-01

## 背景

Moment One 将同时支持眼镜、Web、移动端和未来 MCP。若不同入口直接围绕数据库表、页面字段或 Agent Prompt 实现业务，会产生多套 Moment 定义、权限规则和删除逻辑。

项目当前处于 Phase 0，已有 FastAPI、SQLAlchemy/Alembic 骨架和初步 `Moment` dataclass，但尚无业务表 migration，因此需要先冻结概念边界和存储职责。

## 决策

### 1. Moment 是核心聚合根

Moment 表示用户确认保存的个人经历、观察或状态。所有创建、修改、删除、媒体关联和版本规则由 Moment 领域服务统一执行。

REST、未来 MCP、同步处理器和后台任务不得各自实现一套业务规则。

### 2. 内部 User 是所有权边界

Casdoor 提供认证，Moment One 保存内部 User。

外部身份使用 `issuer + subject` 映射到内部 `user_id`；邮箱仅为资料字段。请求中的目标用户不能由客户端自由指定，必须来自验证后的认证上下文。

### 3. PostgreSQL 是云端业务事实源

PostgreSQL 保存：

- 用户及身份映射；
- Moment 当前快照及历史 Revision；
- 媒体元数据和关联；
- 幂等、删除确认、配置和审计。

Redis、搜索索引和客户端本地库都不能成为云端 Canonical Source of Truth。

### 4. MinIO 只保存媒体字节

MinIO 保存原图、音频、视频、缩略图和转码结果。对象 Key 由服务端生成，客户端只获得短期签名 URL。

业务所有权、可用状态和 Moment 关联保存在 PostgreSQL，不能通过 MinIO 路径或对象存在性代替。

### 5. 当前快照与历史版本分离

`moments` 保存查询所需的当前快照，`moment_revisions` 保存每次成功变更后的完整快照。

所有修改和删除使用 `expectedRevision` 乐观锁；冲突时不得静默覆盖。

### 6. 用户内容与 AI 派生内容分离

用户标题、正文和确认后的输入是用户内容。AI Summary、标签建议、情绪推断和搜索向量是可重建派生数据。

派生内容不能自动覆盖用户内容，也不能伪装成用户确认的事实。

### 7. 删除使用 Tombstone 和两阶段确认

删除先执行 Preview，再使用一次性 Confirmation 执行 Confirm。成功删除设置 `deleted_at` 并递增 Revision，以支持审计和离线同步。

物理清理与业务删除分离，由后续数据保留策略控制。

### 8. 稳定字段关系化，未稳定结构有限使用 JSONB

所有权、Revision、时间、Category、状态和关联使用普通列及约束。Location、Emotion、Revision Snapshot 和少量扩展元数据可在结构未稳定时使用 JSONB。

JSONB 子字段一旦承担稳定查询、唯一性或外键职责，就升级为普通列或独立表。

## 结果

优点：

- 多个客户端和协议共享同一套业务语义；
- 数据所有权、并发修改和删除行为可验证；
- PostgreSQL 和 MinIO 职责明确；
- 为离线同步、审计和 MCP 留出稳定边界；
- 数据库 migration 可以从明确模型生成，而不是反向定义产品。

代价：

- 写操作需要事务协调当前快照、Revision、审计和幂等记录；
- 媒体上传采用状态机，不能假设上传 URL 返回后文件即可用；
- Tombstone、历史版本和孤立媒体需要清理策略；
- 部分尚未稳定的 Location/Emotion 结构以后可能需要 migration。

## 不在本决策中的事项

本文不决定：

- 具体字段长度和上传大小；
- Tombstone、Revision、审计的保留天数；
- 是否以及何时引入 pgvector/OpenSearch；
- 离线冲突的自动合并算法；
- Habit 是否未来演化为独立聚合；
- AI Artifact 的完整模型管理方案。

这些事项在出现明确产品或运行需求后分别记录 ADR。

## 相关文档

- [Moment One 领域模型](../domain/MOMENT_DOMAIN_MODEL.md)
- [PostgreSQL 与 MinIO 存储数据模型](../data/STORAGE_DATA_MODEL.md)
- [服务端实施方案](../../BACKEND_IMPLEMENTATION_PLAN.md)
