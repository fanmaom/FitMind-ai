"""body_metrics 表 + RLS

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TENANT_TABLES = ["body_metrics"]


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _enable_rls(table: str) -> None:
    """与 0002 完全同构。NULLIF(..., '') 不能省：自定义 GUC 被设置过又随事务
    结束回到默认值后返回的是空字符串而非 NULL，''::uuid 会直接抛异常。"""
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
    if not _has_table("body_metrics"):
        op.create_table(
            "body_metrics",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", PGUUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("weight_kg", sa.Float(), nullable=False),
            sa.Column("body_fat_pct", sa.Float(), nullable=True),
            sa.Column("idempotency_key", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_body_metrics_idem"),
        )
        op.create_index("ix_body_metrics_user_id", "body_metrics", ["user_id"])
        op.create_index("ix_body_metrics_date", "body_metrics", ["date"])

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("body_metrics"):
        op.drop_table("body_metrics")
