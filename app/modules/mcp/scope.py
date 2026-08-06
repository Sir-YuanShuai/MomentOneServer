"""MCP Scope 常量（与 MCP_MVP_PLAN §3.3 对齐）。

- `moments.read`：默认，读/统计工具
- `moments.write`：写工具（bookkeeping_create）
- `moments.delete`：删除（第一版无删除工具，保留声明）
"""

from __future__ import annotations

SCOPE_READ = "moments.read"
SCOPE_WRITE = "moments.write"
SCOPE_DELETE = "moments.delete"

ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_DELETE)

# 客户端类型（mcp_authorizations.client_type）：眼镜设备即 MCP 客户端的一种
CLIENT_TYPE_MCP = "mcp"
CLIENT_TYPE_GLASSES = "glasses"

# 眼镜设备的统一 client_id 约定：glasses:{device_id}（与 mcp_authorizations.client_id 对齐）
GLASSES_CLIENT_PREFIX = "glasses:"


def glasses_client_id(device_id: str) -> str:
    return f"{GLASSES_CLIENT_PREFIX}{device_id}"


def device_id_from_client_id(client_id: str) -> str | None:
    if not client_id or not client_id.startswith(GLASSES_CLIENT_PREFIX):
        return None
    return client_id[len(GLASSES_CLIENT_PREFIX) :]


# 历史命名兼容：早期契约（DEVICE_BINDING.md v1 / Web 绑定对话框）使用冒号
# 分隔（moments:read），MCP 工具与 scope.py 使用点号（moments.read）。
# 数据回填见迁移 0015；这里在边界处规范化，保证存量 token / 绑定行都能工作。
SCOPE_NAME_ALIASES: dict[str, str] = {
    "moments:read": SCOPE_READ,
    "moments:write": SCOPE_WRITE,
    "moments:delete": SCOPE_DELETE,
}


def normalize_scope_names(scopes: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """把历史冒号 scope 规范化为点号（未知 scope 原样透传）。"""
    if not scopes:
        return ()
    return tuple(SCOPE_NAME_ALIASES.get(s, s) for s in scopes)


def parse_scopes(scope_str: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """把 JWT claims 里的空格分隔 scope 解析为元组（兼容 list/tuple 输入）。"""
    if not scope_str:
        return ()
    if isinstance(scope_str, (list, tuple)):
        return tuple(s for s in scope_str if s)
    return tuple(s for s in scope_str.split() if s)


def has_scope(scopes: tuple[str, ...] | None, required: str) -> bool:
    """token 的 scope 是否包含 required（含通配前缀，如 moments.* 覆盖 moments.read）。"""
    if not scopes:
        return False
    if required in scopes:
        return True
    return any(s.endswith(".*") and required.startswith(s[:-2]) for s in scopes)


__all__ = [
    "SCOPE_READ",
    "SCOPE_WRITE",
    "SCOPE_DELETE",
    "ALL_SCOPES",
    "CLIENT_TYPE_MCP",
    "CLIENT_TYPE_GLASSES",
    "GLASSES_CLIENT_PREFIX",
    "glasses_client_id",
    "device_id_from_client_id",
    "SCOPE_NAME_ALIASES",
    "normalize_scope_names",
    "parse_scopes",
    "has_scope",
]
