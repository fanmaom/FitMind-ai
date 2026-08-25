"""全局共享的食物营养数据。"""

from sqlalchemy import Float, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey


class Food(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "foods"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    kcal_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    protein_g: Mapped[float] = mapped_column(Float, nullable=False)
    carb_g: Mapped[float] = mapped_column(Float, nullable=False)
    fat_g: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
