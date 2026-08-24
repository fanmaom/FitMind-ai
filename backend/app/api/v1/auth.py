"""认证路由。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestContext, get_context
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/api/v1", tags=["auth"])

# 未注册邮箱与错密码返回同样的信息，避免被用来枚举已注册账号
_CREDENTIALS_ERROR = "邮箱或密码错误"


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: AsyncSession = Depends(get_db)) -> TokenResponse:
    exists = await session.scalar(select(User).where(User.email == payload.email))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "邮箱已注册")

    user = User(email=payload.email, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _CREDENTIALS_ERROR)
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserResponse)
async def me(ctx: RequestContext = Depends(get_context)) -> UserResponse:
    user = await ctx.session.get(User, ctx.user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return UserResponse(id=user.id, email=user.email)
