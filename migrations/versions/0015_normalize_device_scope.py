"""normalize legacy colon-style scopes in device tables

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-06 12:00:00

设备权限命名对齐：早期契约（DEVICE_BINDING.md v1 / Web 绑定对话框）用冒号
（moments:read / moments:write / moments:delete），MCP 工具与 scope.py 用
点号（moments.read / moments.write / moments.delete）。本迁移回填存量数据；
新数据在 devices/service.py 与 token_verifier.py 边界处统一规范化。

- binding_codes.scope / device_bindings.scope 为 ARRAY(String)，
  用 postgres array_replace 逐项替换冒号为点号。
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_TO_CANONICAL = (
    ("moments:read", "moments.read"),
    ("moments:write", "moments.write"),
    ("moments:delete", "moments.delete"),
)


def _array_replace_sql(column: str, table: str) -> str:
    """把 ARRAY 列里的冒号 scope 替换为点号（array_replace 逐层嵌套）。"""
    expr = column
    for legacy, canonical in _LEGACY_TO_CANONICAL:
        expr = f"array_replace({expr}, '{legacy}', '{canonical}')"
    return (
        f"UPDATE {table} SET {column} = {expr} "
        f"WHERE {column} && ARRAY['moments:read','moments:write','moments:delete']::varchar[]"
    )


def upgrade() -> None:
    op.execute(_array_replace_sql("scope", "binding_codes"))
    op.execute(_array_replace_sql("scope", "device_bindings"))


def downgrade() -> None:
    # 幂等回填，无需回退（旧数据早已是冒号风格，新数据本就走点号）
    pass
