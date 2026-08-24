"""SQLAlchemy 模型。

所有模型都要在这里导入，否则 Alembic autogenerate 看不到它们。
"""

from app.models.user import User

__all__ = ["User"]
