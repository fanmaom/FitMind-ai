"""初始表：users + pgvector 扩展

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    if _has_table("users"):
        op.drop_index("ix_users_email", table_name="users")
        op.drop_table("users")
