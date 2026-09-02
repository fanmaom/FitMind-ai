"""jobs.claimed_at：让卡死的 running 任务可被回收

Revision ID: 0014
Revises: 0013

worker 在执行任务途中硬退出（SIGKILL、OOM、容器被杀）时，那条任务永久停在
running。claim_jobs 只捞 pending，所以没有任何机制会再碰它——事实抽取或待办
抽取就此丢失，而且不报错、不重试，只是那一条永远不出现。

回收需要知道"这条 running 是什么时候领的"。created_at 不行：它是入队时间，
一条排队很久才被领走的任务会被误判成超时。所以补一个 claimed_at。
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("jobs", "claimed_at"):
        return
    op.add_column(
        "jobs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 看门狗的查询形态：status='running' AND claimed_at < 阈值。
    op.create_index("ix_jobs_running_claimed", "jobs", ["status", "claimed_at"])

    # 已有的 running 全是上一次进程留下的孤儿——这个字段刚加，它们不可能
    # 正在被执行。直接标为可回收，让看门狗第一轮就把它们捞回来。
    #
    # jobs 上是 FORCE ROW LEVEL SECURITY，表所有者也受策略约束，而策略比对的
    # app.user_id 在迁移里没有设置。不先关掉这条 UPDATE 一行都匹配不到，
    # 且不会报错——那些孤儿会继续卡在 running，看门狗永远捞不到它们。
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE jobs SET claimed_at = created_at WHERE status = 'running'",
    )
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    if not _has_column("jobs", "claimed_at"):
        return
    op.drop_index("ix_jobs_running_claimed", table_name="jobs")
    op.drop_column("jobs", "claimed_at")
