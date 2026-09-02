"""待办项模型：从助理建议中沉淀的待跟进事项。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey

_DIMENSION = get_settings().embedding_dim

VALID_STATUSES = ("pending", "done", "ignored")


class ActionItem(Base, UUIDPrimaryKey, TimestampMixin):
    """一条待跟进事项。

    与 L2 记忆的分工：记忆存「用户是什么样的人」，待办存「用户接下来要做
    什么」。前者稳定、要召回进 prompt；后者有生命周期、只在面板里被消费，
    刻意不注入上下文——否则模型会把自己上轮的建议当成既定事实复述。

    ``ignored`` 不物理删除：它是去重的基准。用户明确划掉的建议，下一轮抽取
    再冒出来就是骚扰。
    """

    __tablename__ = "action_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    embedding: Mapped[list[float]] = mapped_column(Vector(_DIMENSION), nullable=False)
    # 溯源三件套。没有会话列表 UI，所以除了两个 id 还存一句原文摘录：
    # 消息不在当前已加载会话里时，摘录是唯一还能给用户看的凭据。
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True,
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True,
    )
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
