# Moment One Server 文档

> 更新日期：2026-08-09

## 核心设计

- [Moment One 领域模型](./domain/MOMENT_DOMAIN_MODEL.md)：产品边界、领域词汇、Moment v1 字段语义和业务不变量。
- [设备绑定（Device Binding）设计](./domain/DEVICE_BINDING.md)：三端扫码绑定流程、OAuth 2.1 Token 端点、RS256 双 token、订阅设备数与远程调用额度。
- [外部身份关联与账号合并](./domain/IDENTITY_ACCOUNT_LINKING.md)：多平台登录身份绑定、解绑保护、重复 User 合并和权益去重。
- [PostgreSQL 与 MinIO 存储数据模型](./data/STORAGE_DATA_MODEL.md)：表关系、字段、约束、索引、事务和数据生命周期。
- [ADR-0001：领域模型与存储边界](./decisions/0001_DOMAIN_AND_STORAGE_BOUNDARIES.md)：关键决策及其取舍。
- [服务端实施方案](../BACKEND_IMPLEMENTATION_PLAN.md)：总体架构、阶段、API 草案和验收标准。

## 阅读顺序

首次参与项目时建议按以下顺序阅读：

1. 领域模型：理解 Moment One 在管理什么，以及明确不管理什么；
2. 设备绑定：理解三端如何安全地把眼镜接入云端（Web/Server/Glasses 共享契约）；
3. 身份关联：理解不同登录渠道如何归属于同一内部 User；
4. ADR：理解为什么采用当前边界；
5. 存储模型：理解 PostgreSQL 和 MinIO 如何承载领域对象；
6. 实施方案：理解各阶段如何交付这些能力。

## 文档状态说明

- `Draft`：设计方向明确，但部分参数或契约仍待冻结；
- `Accepted for Phase N`：当前阶段按该决策实施；
- `Implemented`：对应代码、migration 和测试已经落地；
- `Superseded`：已被后续 ADR 替代。

文档描述目标模型时必须单独说明当前实现状态，避免把目标设计误认为已交付能力。
