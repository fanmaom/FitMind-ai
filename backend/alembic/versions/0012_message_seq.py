"""messages.seq：会话内消息顺序的确定性排序键

Revision ID: 0012
Revises: 0011

created_at 单独排序是不确定的。PostgreSQL 的 now() 返回**事务开始时刻**，
而一个回合里 user 与 assistant 两条消息在同一个事务提交，两者的 created_at
逐微秒相同。落到 ORDER BY created_at 上，同一时刻的行由 PostgreSQL 自行决定
顺序——它可能把助理的回复排在用户的提问之前。

后果是模型读到的对话是乱的：先看到回答、再看到问题。这个 bug 不报错，只
表现为多轮对话里模型偶尔答非所问，而且因为顺序取决于物理存储，重放同一份
数据未必能复现。

用 id（随机 UUID）做 tie-breaker 解决不了：那只是把"不确定"换成"确定但错误"。
需要一个真正单调递增的列，于是补一个 BIGSERIAL。

现有数据按 (created_at, ctid) 回填——ctid 是物理行位置，在这里是唯一能
近似还原插入顺序的依据。会话内顺序在极少数情况下可能仍不完美，但这只影响
迁移前的旧数据，且不会比现状更差。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("messages", "seq"):
        return

    # 先加可空列，回填后再补非空约束：直接加 BIGSERIAL NOT NULL 在大表上会
    # 长时间持锁。
    op.add_column("messages", sa.Column("seq", sa.BigInteger(), nullable=True))

    # 回填。ctid 是物理行位置，在没有其他线索时是最接近插入顺序的依据。
    #
    # messages 上是 FORCE ROW LEVEL SECURITY，表所有者也受策略约束，而策略
    # 比对的 app.user_id 在迁移里没有设置——不先关掉的话这条 UPDATE 一行都
    # 匹配不到，回填静默失败，紧接着的 SET NOT NULL 才会炸出来。
    op.execute("ALTER TABLE messages NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        WITH ordered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, ctid) AS rn
            FROM messages
        )
        UPDATE messages SET seq = ordered.rn
        FROM ordered WHERE messages.id = ordered.id
        """,
    )
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY")

    op.execute("CREATE SEQUENCE IF NOT EXISTS messages_seq_seq OWNED BY messages.seq")
    op.execute(
        "SELECT setval('messages_seq_seq', COALESCE((SELECT MAX(seq) FROM messages), 0) + 1, false)",
    )
    op.execute("ALTER TABLE messages ALTER COLUMN seq SET DEFAULT nextval('messages_seq_seq')")
    op.alter_column("messages", "seq", nullable=False)

    # 历史加载的唯一查询形态：按会话取最新 N 条。把 seq 放进索引让
    # ORDER BY ... LIMIT 直接走索引，不必排序整个会话。
    op.create_index("ix_messages_conversation_seq", "messages", ["conversation_id", "seq"])


def downgrade() -> None:
    if not _has_column("messages", "seq"):
        return
    op.drop_index("ix_messages_conversation_seq", table_name="messages")
    op.drop_column("messages", "seq")
    op.execute("DROP SEQUENCE IF EXISTS messages_seq_seq")
