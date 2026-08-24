"""workout_logs 表 + 租户表 RLS

Revision ID: 0002
Revises: 0001

关于 users 表为何不启用 RLS：注册时用户还没有身份，WITH CHECK 会拦掉
插入。users 表通过"不存在任何列举用户的接口"来保护，/me 按主键取自己。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# 后续迁移新增的租户表追加到各自迁移里，套用同一套 _enable_rls
TENANT_TABLES = ["workout_logs"]


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _enable_rls(table: str) -> None:
    """启用并强制 RLS。

    FORCE 是必须的：表 owner 默认绕过 RLS，而应用连的角色正是库的 owner，
    不 FORCE 的话 policy 建了也不生效，测试还会以为通过了。
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    # NULLIF(..., '') 不能省。自定义 GUC 被设置过又随事务结束回到默认值后，
    # current_setting(name, true) 返回的是空字符串而不是 NULL，
    # 而 ''::uuid 会直接抛 InvalidTextRepresentation —— 于是任何已认证请求
    # 提交之后，那条池化连接上再查租户表就是 500 崩溃，而不是安全地返回零行。
    # 只有从未设置过该 GUC 的全新连接才会返回 NULL，所以单跑一个测试发现不了。
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
    if not _has_table("workout_logs"):
        op.create_table(
            "workout_logs",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", PGUUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("exercise", sa.String(64), nullable=False),
            sa.Column("sets", JSONB, nullable=False),
            sa.Column("idempotency_key", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_workout_logs_idem"),
        )
        op.create_index("ix_workout_logs_user_id", "workout_logs", ["user_id"])
        op.create_index("ix_workout_logs_date", "workout_logs", ["date"])
        op.create_index("ix_workout_logs_exercise", "workout_logs", ["exercise"])

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("workout_logs"):
        op.drop_table("workout_logs")
