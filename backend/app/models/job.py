"""持久化的 PostgreSQL 异步任务队列。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey


class Job(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        # 看门狗的查询形态：status='running' AND claimed_at < 阈值。
        Index("ix_jobs_running_claimed", "status", "claimed_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 领取时刻。判断 running 是否卡死要靠它，不能用 created_at——那是入队
    # 时间，一条排队很久才被领走的任务会被误判成超时。
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
