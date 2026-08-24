"""训练日志（L3 日志层）。"""

import uuid
from datetime import date as date_type

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey


class WorkoutLog(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "workout_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_workout_logs_idem"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    exercise: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sets: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
