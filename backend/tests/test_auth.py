"""认证流程测试。"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _email() -> str:
    return f"auth-{uuid.uuid4().hex[:8]}@test.com"


@pytest.mark.asyncio
async def test_register_then_login_then_me():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = _email()

        r = await client.post("/api/v1/auth/register", json={"email": email, "password": "pw123456"})
        assert r.status_code == 201
        assert "access_token" in r.json()

        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pw123456"})
        assert r.status_code == 200
        token = r.json()["access_token"]

        r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["email"] == email


@pytest.mark.asyncio
async def test_duplicate_email_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = _email()
        await client.post("/api/v1/auth/register", json={"email": email, "password": "pw123456"})
        r = await client.post("/api/v1/auth/register", json={"email": email, "password": "pw123456"})
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_login_wrong_password_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = _email()
        await client.post("/api/v1/auth/register", json={"email": email, "password": "pw123456"})
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrongpw"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email_rejected():
    """未注册邮箱和错密码要返回同样的状态码与信息，不给枚举账号的机会。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/auth/login",
                              json={"email": _email(), "password": "pw123456"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_without_token_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/me")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_with_garbage_token_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_short_password_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/auth/register", json={"email": _email(), "password": "short"})
        assert r.status_code == 422


class TestSecurityPrimitives:
    def test_password_hash_is_not_plaintext(self):
        from app.core.security import hash_password, verify_password

        hashed = hash_password("pw123456")
        assert hashed != "pw123456"
        assert verify_password("pw123456", hashed) is True
        assert verify_password("wrong", hashed) is False

    def test_same_password_hashes_differently(self):
        """加盐——同一密码两次哈希结果必须不同，否则彩虹表可用。"""
        from app.core.security import hash_password

        assert hash_password("pw123456") != hash_password("pw123456")

    def test_token_roundtrip(self):
        from app.core.security import create_access_token, decode_token

        uid = str(uuid.uuid4())
        assert decode_token(create_access_token(uid)) == uid

    def test_tampered_token_rejected(self):
        from app.core.security import create_access_token, decode_token

        token = create_access_token(str(uuid.uuid4()))
        with pytest.raises(ValueError):
            decode_token(token[:-4] + "aaaa")


class TestBcryptByteLimit:
    """bcrypt 只取前 72 字节。中文密码极易超限，必须明确拒绝而非静默截断。"""

    def test_long_chinese_password_rejected_at_hash(self):
        from app.core.security import PasswordTooLongError, hash_password

        # 25 个汉字 = 75 字节 > 72
        with pytest.raises(PasswordTooLongError):
            hash_password("密码" * 13)

    def test_truncation_would_have_been_a_security_hole(self):
        """证明这个坑是真的：若不拦，两个不同的长密码会哈希成同一个值。"""
        from app.core.security import BCRYPT_MAX_PASSWORD_BYTES

        a = "对" * 24 + "甲"   # 前 72 字节相同，第 73 字节起不同
        b = "对" * 24 + "乙"
        assert a.encode()[:BCRYPT_MAX_PASSWORD_BYTES] == b.encode()[:BCRYPT_MAX_PASSWORD_BYTES]
        assert a != b

    @pytest.mark.asyncio
    async def test_long_chinese_password_rejected_at_api(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/v1/auth/register",
                                  json={"email": _email(), "password": "密码" * 13})
            assert r.status_code == 422

    def test_boundary_password_accepted(self):
        from app.core.security import hash_password, verify_password

        pw = "a" * 72
        assert verify_password(pw, hash_password(pw)) is True
