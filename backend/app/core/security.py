"""密码哈希与 JWT。

直接用 bcrypt 而非 passlib：后者最后一次发版是 2020 年，且它读的
bcrypt.__about__ 在 bcrypt 4.1+ 已被移除。我们只需要 hash / verify 两个函数。
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings

# bcrypt 只处理密码的前 72 字节，超出部分静默丢弃。
# 不能靠字符数限制——一个 25 字的中文密码就是 75 字节，后面会被悄悄截掉，
# 用户以为自己设了长密码。所以按字节校验并明确报错。
BCRYPT_MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ValueError):
    """密码 UTF-8 编码超过 bcrypt 的 72 字节上限。"""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"密码过长：UTF-8 编码 {len(encoded)} 字节，上限 {BCRYPT_MAX_PASSWORD_BYTES} 字节"
            f"（约 72 个英文字符或 24 个汉字）",
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("utf-8"))
    except ValueError:
        # 哈希串格式非法（例如库里存了脏数据）——当作校验失败，不要抛给调用方
        return False


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str:
    """返回 user_id；无效或过期抛 ValueError。"""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise ValueError("token 无效或已过期") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise ValueError("token 缺少 sub 字段")
    return user_id
