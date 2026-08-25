"""L1 用户档案模型。"""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class Profile(Base, TimestampMixin):
    """每位用户一行的 JSONB 档案。

    档案始终整份读取，字段也会随产品演进；JSONB 能避免每增加一个档案字段
    就修改表结构，同时仍由应用层白名单约束可写字段。
    """

    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
