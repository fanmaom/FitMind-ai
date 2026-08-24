"""API 依赖注入：请求上下文。"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
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

    # 注入 RLS 上下文。两个关键点：
    #
    # 1. 用 set_config(..., true) 而不是 SET。第三个参数 true 表示事务级
    #    （等价于 SET LOCAL），事务结束自动失效。若用会话级的 SET，值会残留
    #    在池化连接上被下一个请求复用 —— 下一个用户读到上一个用户的数据。
    #    这个 bug 只在并发 + 连接复用时出现，单用户顺序测试永远发现不了。
    #
    # 2. 必须用参数绑定。Postgres 的 SET 语法不支持占位符，只有 set_config()
    #    这个函数形式可以，顺带避免了 SQL 注入。
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": str(user_id)},
    )
    return RequestContext(user_id=user_id, session=session)
