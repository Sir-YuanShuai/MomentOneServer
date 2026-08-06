"""unify device permissions into mcp_authorizations

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-06 14:00:00

设备权限模型统一（眼镜端即 MCP 客户端的一种）：

- mcp_authorizations 增加 client_type（mcp / glasses），成为统一授权记录
  （权限事实源，scope/status 由 Web 端统一管理）；
- 存量 device_bindings 回填为 client_type='glasses' 的授权记录
  （client_id = 'glasses:{device_id}'，scope 规范化后写入）；
- device_bindings.scope 保留为 legacy 镜像，不再作为权限事实源；
- 存量 mcp_authorizations 的 scope 同样规范化（防御历史冒号命名）。
"""
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_ALIASES = (
    ("moments:read", "moments.read"),
    ("moments:write", "moments.write"),
    ("moments:delete", "moments.delete"),
)


def _normalize_scope(scope: list[str] | str | None) -> str:
    if not scope:
        return ""
    parts = scope if isinstance(scope, list) else scope.split()
    normalized = []
    for part in parts:
        for legacy, canonical in _SCOPE_ALIASES:
            if part == legacy:
                part = canonical
                break
        normalized.append(part)
    return " ".join(normalized)


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column(
        "mcp_authorizations",
        sa.Column(
            "client_type",
            sa.String(16),
            nullable=False,
            server_default="mcp",
        ),
    )

    # 1) 存量 mcp_authorizations 的 scope 规范化（防御历史冒号命名）
    rows = bind.execute(sa.text("SELECT id, scope FROM mcp_authorizations")).fetchall()
    for auth_id, scope in rows:
        normalized = _normalize_scope(scope)
        if normalized != scope:
            bind.execute(
                sa.text("UPDATE mcp_authorizations SET scope = :scope WHERE id = :id"),
                {"scope": normalized, "id": auth_id},
            )

    # 2) 存量设备绑定回填为 glasses 授权记录（scope 规范化）
    bindings = bind.execute(
        sa.text(
            "SELECT id, user_id, device_id, scope, status, bound_at, last_active_at, revoked_at "
            "FROM device_bindings"
        )
    ).fetchall()
    inserted = 0
    for b in bindings:
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM mcp_authorizations "
                "WHERE user_id = :uid AND client_id = :cid"
            ),
            {"uid": b.user_id, "cid": f"glasses:{b.device_id}"},
        ).fetchone()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO mcp_authorizations "
                "(id, user_id, client_id, client_name, client_type, scope, status, "
                " last_active_at, revoked_at, created_at, updated_at) "
                "VALUES (:id, :uid, :cid, :cname, 'glasses', :scope, :status, "
                " :last_active, :revoked, :created, :updated)"
            ),
            {
                "id": uuid4(),
                "uid": b.user_id,
                "cid": f"glasses:{b.device_id}",
                "cname": b.device_id,
                "scope": _normalize_scope(b.scope),
                "status": b.status,
                "last_active": b.last_active_at,
                "revoked": b.revoked_at,
                "created": b.bound_at,
                "updated": b.last_active_at or b.bound_at,
            },
        )
        inserted += 1
    if inserted:
        bind.commit()


def downgrade() -> None:
    bind = op.get_bind()
    # 删除回填的 glasses 授权记录（client_type='glasses'）
    bind.execute(sa.text("DELETE FROM mcp_authorizations WHERE client_type = 'glasses'"))
    bind.commit()
    op.drop_column("mcp_authorizations", "client_type")
