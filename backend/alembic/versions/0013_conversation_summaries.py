"""conversation_summaries 表与 RLS

Revision ID: 0013
Revises: 0012

长对话超出 token 预算时，compress_history 从最旧开始丢弃，被丢掉的内容彻底
消失。L2 事实层接不住这部分——它的判据是"用户明确说过的稳定事实"，而丢掉的
是"这个计划当初按什么前提排的""中途因为什么调整过"这类会随计划变化的上下文。

摘要异步生成（worker 在回合结束后更新），主链路只读已有的那份，所以不会给
用户等回复的路径增加任何同步 LLM 调用。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TENANT_TABLES = ["conversation_summaries"]


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
    if not _has_table("conversation_summaries"):
        op.create_table(
            "conversation_summaries",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column(
                "conversation_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                PGUUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("content", sa.Text(), nullable=False),
            # 摘要已覆盖到的最大 messages.seq，用于增量摘要与缺口判断。
            sa.Column("covered_seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
        )
        op.create_index(
            "ix_conversation_summaries_conversation_id",
            "conversation_summaries", ["conversation_id"],
        )
        op.create_index(
            "ix_conversation_summaries_user_id", "conversation_summaries", ["user_id"],
        )
        # 一个会话一份。并发回合同时想写时靠唯一约束 + upsert 收敛，
        # 而不是靠"先查再插"——后者在并发下会插出两行。
        op.create_unique_constraint(
            "uq_conversation_summaries_conv", "conversation_summaries",
            ["conversation_id"],
        )

    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_TABLES:
        if _has_table(table):
            _disable_rls(table)
    if _has_table("conversation_summaries"):
        op.drop_table("conversation_summaries")
