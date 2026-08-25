"""体征记录（L3 日志层）。"""

import uuid
from datetime import date as date_type

from sqlalchemy import Date, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey


class BodyMetric(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "body_metrics"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_body_metrics_idem"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    body_fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
