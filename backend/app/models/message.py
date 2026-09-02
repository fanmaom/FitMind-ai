"""消息。

content 用 JSONB：一条助理消息可能同时含文本和多张卡片，结构会演进。
status 区分 streaming / done：先落库再推流，客户端断连时服务端跑完仍能落盘。
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, Sequence, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey

# 会话内消息顺序的排序键。
#
# created_at 单独排序是不确定的：PostgreSQL 的 now() 返回**事务开始时刻**，
# 而一个回合里 user 与 assistant 两条消息在同一事务提交，两者的 created_at
# 逐微秒相同。ORDER BY created_at 时同一时刻的行由数据库自行决定顺序——
# 它可能把助理的回复排在用户的提问之前，模型读到的对话就是乱的。
#
# 用 id 做 tie-breaker 无效：随机 UUID 只是把"不确定"换成"确定但错误"。
_MESSAGE_SEQ = Sequence("messages_seq_seq")


class Message(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("user_id", "client_message_id", name="uq_messages_client_id"),
        # 历史加载的唯一查询形态：按会话取最新 N 条。带上 seq 让
        # ORDER BY ... LIMIT 走索引，不必排序整个会话。
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seq: Mapped[int] = mapped_column(
        BigInteger, _MESSAGE_SEQ, server_default=_MESSAGE_SEQ.next_value(),
        nullable=False,
    )
