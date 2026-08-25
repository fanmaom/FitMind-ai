"""L1 档案层。"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.memory.profile import PROFILE_FIELDS, load_profile, merge_profile
from app.core.tools.registry import ToolContext, load_tools, registry
from app.main import app


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


async def _auth(client: AsyncClient, tag: str) -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": f"{tag}-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestStorage:
    @pytest.mark.asyncio
    async def test_empty_profile_returns_empty_dict(self, db, seeded_user):
        assert await load_profile(db, seeded_user) == {}

    @pytest.mark.asyncio
    async def test_merge_creates_then_updates(self, db, seeded_user):
        await merge_profile(db, seeded_user, {"weight_kg": 82.0})
        await merge_profile(db, seeded_user, {"height_cm": 178.0})
        loaded = await load_profile(db, seeded_user)
        assert loaded["weight_kg"] == 82.0
        assert loaded["height_cm"] == 178.0, "第二次写入把第一次覆盖了，应该是合并"

    @pytest.mark.asyncio
    async def test_merge_overwrites_same_field(self, db, seeded_user):
        await merge_profile(db, seeded_user, {"weight_kg": 82.0})
        await merge_profile(db, seeded_user, {"weight_kg": 81.0})
        assert (await load_profile(db, seeded_user))["weight_kg"] == 81.0

    @pytest.mark.asyncio
    async def test_rejects_unknown_fields(self, db, seeded_user):
        """白名单外的字段一律拒绝——模型可能编出任意 key，
        污染档案后每轮都会注入进 prompt。"""
        with pytest.raises(ValueError) as exc:
            await merge_profile(db, seeded_user, {"favorite_color": "blue"})
        assert "favorite_color" in str(exc.value)

    @pytest.mark.asyncio
    async def test_null_clears_field(self, db, seeded_user):
        await merge_profile(db, seeded_user, {"weight_kg": 82.0})
        await merge_profile(db, seeded_user, {"weight_kg": None})
        assert "weight_kg" not in await load_profile(db, seeded_user)

    @pytest.mark.asyncio
    async def test_nested_values_supported(self, db, seeded_user):
        """lifts 是 dict、injuries 是 list，JSONB 要能原样存取。"""
        await merge_profile(db, seeded_user, {
            "lifts": {"深蹲": 130.0}, "injuries": ["右肩"],
        })
        loaded = await load_profile(db, seeded_user)
        assert loaded["lifts"]["深蹲"] == 130.0
        assert loaded["injuries"] == ["右肩"]


class TestConfirmationSemantics:
    """档案影响所有后续计划，写错会长期污染。
    用户明说的直接写；助理推断的必须先征询。"""

    @pytest.mark.asyncio
    async def test_unconfirmed_sensitive_does_not_write(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("update_profile", {
            "updates": {"injuries": ["右肩"]}, "confirmed": False,
            "reason": "用户提到肩膀酸",
        }, ctx=ctx)

        assert out["written"] is False
        assert out["needs_confirmation"] is True
        assert "右肩" in out["confirmation_prompt"]
        assert await load_profile(db, seeded_user) == {}, "未确认却写库了"

    @pytest.mark.asyncio
    async def test_confirmed_sensitive_writes(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("update_profile", {
            "updates": {"injuries": ["右肩"]}, "confirmed": True,
        }, ctx=ctx)

        assert out["written"] is True
        assert (await load_profile(db, seeded_user))["injuries"] == ["右肩"]

    @pytest.mark.asyncio
    async def test_non_sensitive_writes_without_confirmation(self, db, seeded_user):
        """身高体重这类客观数据不需要征询，否则每次都要确认太啰嗦。"""
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("update_profile", {
            "updates": {"height_cm": 178.0}, "confirmed": False,
        }, ctx=ctx)

        assert out["written"] is True
        assert (await load_profile(db, seeded_user))["height_cm"] == 178.0

    @pytest.mark.asyncio
    async def test_mixed_update_is_held_entirely(self, db, seeded_user):
        """一次更新里既有敏感又有非敏感字段时，整体挂起等确认——
        部分写入会让用户搞不清到底记了什么。"""
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("update_profile", {
            "updates": {"height_cm": 178.0, "goal": "cut"}, "confirmed": False,
        }, ctx=ctx)

        assert out["written"] is False
        assert await load_profile(db, seeded_user) == {}

    @pytest.mark.asyncio
    async def test_confirmation_prompt_is_actionable(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("update_profile", {
            "updates": {"goal": "cut"}, "confirmed": False, "reason": "用户说想瘦",
        }, ctx=ctx)
        assert len(out["confirmation_prompt"]) > 15
        assert "用户说想瘦" in out["confirmation_prompt"]

    @pytest.mark.asyncio
    async def test_unknown_field_rejected_by_tool(self, db, seeded_user):
        from app.core.tools.registry import ToolValidationError

        ctx = ToolContext(user_id=seeded_user, session=db)
        with pytest.raises((ValueError, ToolValidationError)):
            await registry.invoke("update_profile", {
                "updates": {"favorite_color": "blue"}, "confirmed": True,
            }, ctx=ctx)


class TestApi:
    @pytest.mark.asyncio
    async def test_get_and_patch(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "prof")

            assert (await client.get("/api/v1/profile", headers=headers)).json() == {}

            r = await client.patch("/api/v1/profile", headers=headers,
                                   json={"weight_kg": 82.0, "goal": "cut"})
            assert r.status_code == 200
            assert (await client.get("/api/v1/profile", headers=headers)).json()["goal"] == "cut"

    @pytest.mark.asyncio
    async def test_patch_rejects_unknown_field(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "prof2")
            r = await client.patch("/api/v1/profile", headers=headers,
                                   json={"favorite_color": "blue"})
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_profile_is_isolated_between_users(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            head_a = await _auth(client, "iso-a")
            head_b = await _auth(client, "iso-b")
            await client.patch("/api/v1/profile", headers=head_a, json={"weight_kg": 82.0})
            assert (await client.get("/api/v1/profile", headers=head_b)).json() == {}


class TestFieldWhitelist:
    def test_covers_everything_rule_fallback_needs(self):
        """L4 兜底要读这些字段，白名单里缺一个就会永远「档案不完整」。"""
        from app.core.agent.rule_fallback import REQUIRED_FOR_MACROS, REQUIRED_FOR_PROJECTION

        for field in set(REQUIRED_FOR_MACROS) | set(REQUIRED_FOR_PROJECTION):
            assert field in PROFILE_FIELDS, f"白名单缺少 L4 依赖的字段 {field}"

    def test_every_field_has_chinese_description(self):
        for name, desc in PROFILE_FIELDS.items():
            assert desc, f"{name} 缺说明"

    def test_sensitive_fields_are_subset_of_whitelist(self):
        from app.core.memory.profile import SENSITIVE_FIELDS

        assert SENSITIVE_FIELDS <= set(PROFILE_FIELDS)


class TestChatIntegration:
    @pytest.mark.asyncio
    async def test_profile_reaches_rule_fallback(self, monkeypatch):
        """档案接入后，L4 兜底应能算出真实数字而不再报「档案不完整」。"""
        import app.services.chat_service as svc

        monkeypatch.setattr(svc, "build_provider",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dead")))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "integ")
            await client.patch("/api/v1/profile", headers=headers, json={
                "weight_kg": 82.0, "height_cm": 178.0, "age": 30,
                "sex": "male", "activity": "moderate", "goal": "cut",
            })
            conv = (await client.post("/api/v1/conversations", headers=headers)).json()["id"]

            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "我该吃多少"})
            assert "档案不完整" not in r.text
            assert "macros" in r.text, "档案齐全时应给出营养素卡片"
