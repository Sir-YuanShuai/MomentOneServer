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


def parse_scopes(scope_str: str | None) -> tuple[str, ...]:
    """把 JWT claims 里的空格分隔 scope 解析为元组。"""
    if not scope_str:
        return ()
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
    "parse_scopes",
    "has_scope",
]
