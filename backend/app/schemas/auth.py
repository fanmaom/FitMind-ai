"""认证相关 schema。"""

import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import BCRYPT_MAX_PASSWORD_BYTES


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, v: str) -> str:
        """按字节校验——bcrypt 只取前 72 字节，中文密码很容易超。"""
        if len(v.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(
                f"密码过长：UTF-8 编码超过 {BCRYPT_MAX_PASSWORD_BYTES} 字节"
                f"（约 72 个英文字符或 24 个汉字）",
            )
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
