"""jobs 持久化任务队列 + RLS

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TENANT_TABLES = ["jobs"]


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
    if not _has_table("jobs"):
        op.create_table(
            "jobs",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("type", sa.String(64), nullable=False),
            sa.Column("payload", JSONB, nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "run_after",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
        op.create_index("ix_jobs_type", "jobs", ["type"])
        op.create_index("ix_jobs_status", "jobs", ["status"])
        op.create_index("ix_jobs_run_after", "jobs", ["run_after"])
        op.create_index("ix_jobs_claim", "jobs", ["status", "run_after", "created_at"])

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("jobs"):
        op.drop_table("jobs")
