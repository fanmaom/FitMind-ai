"""memories 表、向量索引与 RLS

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.core.config import get_settings

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TENANT_TABLES = ["memories"]
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
    if not _has_table("memories"):
        op.create_table(
            "memories",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("category", sa.String(32), nullable=False, server_default="general"),
            sa.Column("embedding", Vector(_DIMENSION), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("source_message_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("superseded_by", PGUUID(as_uuid=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        op.create_index("ix_memories_user_id", "memories", ["user_id"])
        op.create_index("ix_memories_active", "memories", ["user_id", "superseded_by"])
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_memories_embedding ON memories "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        )

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("memories"):
        op.drop_table("memories")
