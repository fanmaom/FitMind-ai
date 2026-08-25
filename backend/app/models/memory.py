"""L2 事实记忆模型。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey

_DIMENSION = get_settings().embedding_dim


class Memory(Base, UUIDPrimaryKey, TimestampMixin):
    """可召回的用户事实。

    失效事实通过 ``superseded_by`` 指向替代项，不物理删除，便于追溯助理
    曾经为何做出某个判断。用户主动删除则由 API 执行物理删除。
    """

    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    embedding: Mapped[list[float]] = mapped_column(Vector(_DIMENSION), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
