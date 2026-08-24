"""API 依赖注入：请求上下文。"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token


@dataclass
class RequestContext:
    """一次请求的用户与数据库上下文。

    所有需要认证的路由都通过它拿 user_id 和 session——数据隔离在这一层
    统一注入（见 T4），而不依赖每个 handler 自觉写过滤条件。
    """

    user_id: uuid.UUID
    session: AsyncSession


async def get_context(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_db),
) -> RequestContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer token")

    try:
        user_id = uuid.UUID(decode_token(authorization.removeprefix("Bearer ")))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    return RequestContext(user_id=user_id, session=session)
