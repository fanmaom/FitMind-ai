"""自助注册：新用户能自己开号，且数据只属于他自己。

后端的 /auth/register 一直是通的，缺的是三件事：
  1. 界面上看不出「可以自己注册」——入口是表单底部一行小字链接
  2. 注册失败时的提示不可用（Pydantic 的 422 detail 是数组，前端直接塞进
     Error() 会渲染成 "[object Object]"）
  3. 没有退出功能——clearToken 定义了却没有任何调用方，登录后除了手动清
     localStorage 没法换账号

前端没有测试框架（package.json 里只有 dev/build/start/gen），项目一贯做法是
在后端用读源码的断言守住前端接线。
"""

import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(relative: str) -> str:
    return (WEB_SRC / relative).read_text()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        yield instance


class TestRegisterEndpoint:
    @pytest.mark.asyncio
    async def test_anyone_can_register_without_invitation(self, client):
        """没有邀请码、没有白名单——陌生人自己就能开号。"""
        response = await client.post("/api/v1/auth/register", json={
            "email": f"newcomer-{uuid.uuid4().hex[:8]}@example.com",
            "password": "pw123456",
        })
        assert response.status_code == 201
        assert response.json()["access_token"]

    @pytest.mark.asyncio
    async def test_new_account_starts_empty(self, client):
        """新账号是干净的。这是「独属于自己」最基本的一条。"""
        register = await client.post("/api/v1/auth/register", json={
            "email": f"fresh-{uuid.uuid4().hex[:8]}@example.com", "password": "pw123456",
        })
        headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

        for path in (
            "/api/v1/conversations", "/api/v1/memories",
            "/api/v1/action-items", "/api/v1/plans",
        ):
            rows = await client.get(path, headers=headers)
            assert rows.status_code == 200, path
            assert rows.json() == [], f"{path} 对新账号不是空的"

    @pytest.mark.asyncio
    async def test_two_new_users_cannot_see_each_other(self, client):
        tokens = []
        for _ in range(2):
            register = await client.post("/api/v1/auth/register", json={
                "email": f"iso-{uuid.uuid4().hex[:8]}@example.com", "password": "pw123456",
            })
            tokens.append(register.json()["access_token"])
        first, second = ({"Authorization": f"Bearer {t}"} for t in tokens)

        conv = await client.post("/api/v1/conversations", headers=first)
        conv_id = conv.json()["id"]

        assert (await client.get("/api/v1/conversations", headers=second)).json() == []
        leaked = await client.get(f"/api/v1/conversations/{conv_id}/messages", headers=second)
        assert leaked.status_code == 404, "另一个用户读到了不属于他的会话"

    @pytest.mark.asyncio
    async def test_me_reports_the_signed_in_account(self, client):
        """顶栏要显示「这是谁的数据」，靠的是这个接口。"""
        email = f"whoami-{uuid.uuid4().hex[:8]}@example.com"
        register = await client.post("/api/v1/auth/register", json={
            "email": email, "password": "pw123456",
        })
        headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
        me = await client.get("/api/v1/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["email"] == email

    @pytest.mark.asyncio
    async def test_me_rejects_invalid_token(self, client):
        """前端启动时拿 /me 探 token 有效性，靠的就是这里返回 401。"""
        bad = await client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert bad.status_code == 401

    @pytest.mark.asyncio
    async def test_duplicate_email_detail_is_a_string(self, client):
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        payload = {"email": email, "password": "pw123456"}
        assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 201

        again = await client.post("/api/v1/auth/register", json=payload)
        assert again.status_code == 409
        assert again.json()["detail"] == "邮箱已注册"

    @pytest.mark.asyncio
    async def test_validation_detail_is_a_list(self, client):
        """这条钉住前端那个 bug 的成因：Pydantic 的 detail 是**数组**，
        直接塞进模板字符串会渲染成 "[object Object]"。

        前端的 parseApiError 就是为这个形状写的；哪天 FastAPI 改了形状，
        这条会先报出来。
        """
        response = await client.post("/api/v1/auth/register", json={
            "email": f"short-{uuid.uuid4().hex[:8]}@example.com", "password": "123",
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["type"] == "string_too_short"
        assert detail[0]["loc"][-1] == "password"

    @pytest.mark.asyncio
    async def test_custom_validator_type_is_value_error(self, client):
        """超长密码走的是后端自定义校验器，type 是 value_error 且 msg 里
        已经是中文——前端要保留它而不是套通用兜底。"""
        response = await client.post("/api/v1/auth/register", json={
            "email": f"long-{uuid.uuid4().hex[:8]}@example.com", "password": "汉" * 30,
        })
        assert response.status_code == 422
        item = response.json()["detail"][0]
        assert item["type"] == "value_error"
        assert "密码过长" in item["msg"]


class TestFrontendWiring:
    def test_signup_is_a_visible_option(self):
        """新用户第一眼要能看见「创建账号」是个可选项——否则会以为这是内部
        系统、必须有人给他开号。"""
        source = _read("components/auth/AuthForm.tsx")
        assert "创建账号" in source
        assert "aria-pressed" in source, "分段控件缺少选中状态，读屏器分不出当前模式"

    def test_response_body_is_read_only_once(self):
        """Response 正文是一次性流。原来的代码在两个分支各写了一次
        r.json()，虽然靠短路侥幸没炸，但那是巧合而非设计。"""
        source = _read("components/auth/AuthForm.tsx")
        assert source.count("response.json()") == 1

    def test_validation_errors_are_translated(self):
        """Pydantic 的英文消息不该直接给用户看。映射按 type 做——它是稳定的
        机器标识，比匹配英文原文可靠。"""
        source = _read("services/api.ts")
        assert "parseApiError" in source
        for key in ("string_too_short", "missing", "value_error"):
            assert key in source, f"缺少 {key} 的处理"

    def test_custom_validator_message_is_preserved(self):
        """后端密码校验器写的是中文文案，比任何通用兜底都准确。"""
        assert "Value error, " in _read("services/api.ts")

    def test_password_requirement_shown_before_submitting(self):
        source = _read("components/auth/AuthForm.tsx")
        assert "MIN_PASSWORD_LENGTH" in source
        assert "至少" in source

    def test_register_and_login_use_different_autocomplete(self):
        """浏览器据此决定是"生成新密码"还是"填充已存的那个"。
        都写 current-password 会让密码管理器在注册时填进旧密码。"""
        source = _read("components/auth/AuthForm.tsx")
        assert "new-password" in source
        assert "current-password" in source

    def test_sign_out_exists_and_reloads(self):
        """clearToken 原本没有任何调用方。能切换身份和能注册是同一件事的两半。

        退出必须整页重载：聊天、记忆面板、待办各自持有上一个账号的数据，
        逐个清理容易漏，漏掉的那份会串到下一个账号的界面上。
        """
        menu = _read("components/auth/AccountMenu.tsx")
        assert "clearToken" in menu
        assert "reload" in menu

    def test_account_menu_is_mounted(self):
        source = _read("components/chat/ChatView.tsx")
        assert "AccountMenu" in source
        assert '"/me"' in source, "顶栏没有取当前账号，显示不出这是谁的数据"

    def test_expired_token_falls_back_to_auth_form(self):
        """光看 localStorage 里有没有 token 不够——JWT 会过期，而过期的 token
        长得跟有效的一模一样。只判存在的话，用户会进到一个每个请求都 401、
        且没有回登录页出路的界面。"""
        source = _read("app/page.tsx")
        assert '"/me"' in source, "启动时没有校验 token 有效性"
        assert "clearToken" in source, "token 失效后没有清掉，会一直卡在坏状态"
