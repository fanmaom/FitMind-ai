"""plans 周期计划表 + RLS

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TENANT_TABLES = ["plans"]


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
    if not _has_table("plans"):
        op.create_table(
            "plans",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("type", sa.String(32), nullable=False),
            sa.Column("payload", JSONB, nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_plans_user_id", "plans", ["user_id"])
        op.create_index("ix_plans_type", "plans", ["type"])
        op.create_index("ix_plans_status", "plans", ["status"])
        op.create_index("ix_plans_user_type_status", "plans", ["user_id", "type", "status"])

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("plans"):
        op.drop_table("plans")
