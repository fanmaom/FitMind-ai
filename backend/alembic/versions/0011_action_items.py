"""action_items 表、向量索引与 RLS

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.core.config import get_settings

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TENANT_TABLES = ["action_items"]
_DIMENSION = get_settings().embedding_dim


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """,
    )


def _disable_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    if not _has_table("action_items"):
        op.create_table(
            "action_items",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("category", sa.String(32), nullable=False, server_default="general"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("embedding", Vector(_DIMENSION), nullable=False),
            sa.Column("source_message_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("source_conversation_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("source_quote", sa.Text(), nullable=True),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_action_items_user_id", "action_items", ["user_id"])
        # 面板默认只查 pending，去重也只扫未完成项——两者都走 (user_id, status)。
        op.create_index(
            "ix_action_items_open", "action_items", ["user_id", "status", "created_at"],
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_action_items_embedding ON action_items "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        )

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("action_items"):
        op.drop_table("action_items")
