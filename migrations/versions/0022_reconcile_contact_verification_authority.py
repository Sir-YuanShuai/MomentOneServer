"""reconcile contact verification authority

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-11 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Casdoor 的 emailVerified 可由普通用户更新，且没有 phoneVerified 字段。
    # 只有本系统完成 challenge 后创建的 active contact identity 才是权威记录。
    op.execute(
        """
        UPDATE users AS u
        SET email_verified = EXISTS (
            SELECT 1 FROM user_identities AS i
            WHERE i.user_id = u.id AND i.identity_type = 'email'
              AND i.subject = u.email AND i.status = 'active'
              AND i.verified_at IS NOT NULL
        ),
        phone_verified = EXISTS (
            SELECT 1 FROM user_identities AS i
            WHERE i.user_id = u.id AND i.identity_type = 'phone'
              AND i.subject = u.phone AND i.status = 'active'
              AND i.verified_at IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    # 安全状态回收不可逆：旧的非权威验证标记无法可靠恢复。
    pass
