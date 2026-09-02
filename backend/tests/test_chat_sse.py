"""SSE 流式对话端点。"""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


async def _auth(client: AsyncClient, tag: str = "chat") -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": f"{tag}-{uuid.uuid4().hex[:8]}@test.com", "password": "pw123456",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _new_conversation(client: AsyncClient, headers: dict) -> str:
    r = await client.post("/api/v1/conversations", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ").strip()
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line.removeprefix("data: "))))
    return events


class TestFastPath:
    @pytest.mark.asyncio
    async def test_returns_card_without_calling_model(self, monkeypatch):
        """快路径全程不碰模型——用一个会炸的 build_provider 证明它没被调用。"""
        import app.services.chat_service as svc

        def exploding(*args, **kwargs):
            raise AssertionError("快路径不应该调用模型")

        monkeypatch.setattr(svc, "build_provider", exploding)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "fast")
            conv = await _new_conversation(client, headers)

            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "卧推 80kg 5x5"})
            assert r.status_code == 200, r.text

            names = [n for n, _ in _parse_sse(r.text)]
            assert "card" in names
            assert names[-1] == "message_done"

    @pytest.mark.asyncio
    async def test_card_payload_carries_logged_data(self, monkeypatch):
        import app.services.chat_service as svc

        monkeypatch.setattr(svc, "build_provider",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "card")
            conv = await _new_conversation(client, headers)
            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "深蹲 120kg 5x5"})

            card = next(d for n, d in _parse_sse(r.text) if n == "card")
            assert card["type"] == "workout_logged"
            assert card["payload"]["exercise"] == "深蹲"
            assert card["payload"]["total_volume_kg"] == pytest.approx(3000.0)


class TestPersistence:
    @pytest.mark.asyncio
    async def test_messages_persisted_with_done_status(self, monkeypatch):
        """助理消息必须先落库(streaming)再推流，跑完置 done。"""
        import app.services.chat_service as svc

        monkeypatch.setattr(svc, "build_provider",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "persist")
            conv = await _new_conversation(client, headers)
            await client.post(f"/api/v1/conversations/{conv}/messages",
                              headers=headers, json={"text": "卧推 80kg 5x5"})

            msgs = (await client.get(f"/api/v1/conversations/{conv}/messages",
                                     headers=headers)).json()
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert all(m["status"] == "done" for m in msgs)
            assert msgs[1]["content"]["cards"]

    @pytest.mark.asyncio
    async def test_usage_recorded(self, monkeypatch):
        import app.services.chat_service as svc

        monkeypatch.setattr(svc, "build_provider",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "usage")
            conv = await _new_conversation(client, headers)
            await client.post(f"/api/v1/conversations/{conv}/messages",
                              headers=headers, json={"text": "卧推 80kg 5x5"})

            from sqlalchemy import select

            from app.core.database import async_session_maker, bind_rls_user
            from app.core.security import decode_token
            from app.models.llm_usage import LLMUsage

            uid = uuid.UUID(decode_token(headers["Authorization"].removeprefix("Bearer ")))
            session = async_session_maker()
            try:
                bind_rls_user(session, uid)
                rows = (await session.scalars(select(LLMUsage))).all()
                assert len(rows) == 1
                assert rows[0].degradation_level == 0
            finally:
                await session.close()


class TestNoInternalNamesReachTheUser:
    """线上出过这样的回复：「用 `plan_strength_cycle` 或下肢保护性安排」。
    系统提示已经禁止这么说，但提示只是要求——这里验的是确定性的那一层。"""

    @pytest.mark.asyncio
    async def test_leaked_identifiers_are_scrubbed_in_stream_and_in_db(self, monkeypatch):
        import app.services.chat_service as svc
        from app.core.llm.client import ChatChunk, ChatRequest

        class LeakyProvider:
            model = "leaky"

            async def stream(self, req: ChatRequest):
                # 故意在标识符中间切断：真实流式就是这么来的
                for piece in ["我可以用 `plan_st", "rength_cycle` 帮你排",
                              "，另外 weight_kg 也要补"]:
                    yield ChatChunk(text_delta=piece)
                yield ChatChunk(finish_reason="stop")

        monkeypatch.setattr(svc, "build_provider", lambda *a, **k: LeakyProvider())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "leak")
            conv = await _new_conversation(client, headers)

            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "帮我安排一下训练"})
            streamed = "".join(d["text"] for n, d in _parse_sse(r.text) if n == "text_delta")

            assert "plan_strength_cycle" not in streamed
            assert "weight_kg" not in streamed
            assert "编排增力周期" in streamed
            assert "另外" in streamed, "脱敏不能把话吞掉"

            # 落库的也必须是脱敏后的：重连要取回它，下一轮还要当历史喂回模型
            msgs = (await client.get(f"/api/v1/conversations/{conv}/messages",
                                     headers=headers)).json()
            stored = msgs[1]["content"]["text"]
            assert "plan_strength_cycle" not in stored
            assert stored == streamed, "流出去的和落库的不是同一段文字"

    @pytest.mark.asyncio
    async def test_identifier_at_the_very_end_is_not_swallowed(self, monkeypatch):
        """脱敏靠扣住结尾等边界，忘了 flush 就会吞掉最后一段。"""
        import app.services.chat_service as svc
        from app.core.llm.client import ChatChunk, ChatRequest

        class TailProvider:
            model = "tail"

            async def stream(self, req: ChatRequest):
                yield ChatChunk(text_delta="这条得用 log_workout")
                yield ChatChunk(finish_reason="stop")

        monkeypatch.setattr(svc, "build_provider", lambda *a, **k: TailProvider())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "tail")
            conv = await _new_conversation(client, headers)
            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "帮我安排一下训练"})
            streamed = "".join(d["text"] for n, d in _parse_sse(r.text) if n == "text_delta")
            assert streamed == "这条得用 「记录训练」"

    @pytest.mark.asyncio
    async def test_tool_events_carry_label_only(self, monkeypatch):
        """前端状态行的数据源。给了 name 就会显示"正在调用 plan_meals…"；
        给了 input 就等于把参数（也是内部字段名）一起送到浏览器。"""
        import app.services.chat_service as svc
        from app.core.llm.client import ChatChunk, ChatRequest, ToolCall
        from app.core.tools import registry as reg_mod

        async def fake_invoke(name, args, ctx):
            return {"kcal": 2100}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        class ToolThenTalk:
            model = "tool"

            def __init__(self) -> None:
                self.calls = 0

            async def stream(self, req: ChatRequest):
                self.calls += 1
                if self.calls == 1:
                    yield ChatChunk(
                        tool_calls=[ToolCall(id="c1", name="calc_macros",
                                             arguments={"tdee": 2600, "weight_kg": 82})],
                        finish_reason="tool_calls",
                    )
                else:
                    yield ChatChunk(text_delta="每天 2100 大卡")
                    yield ChatChunk(finish_reason="stop")

        provider = ToolThenTalk()
        monkeypatch.setattr(svc, "build_provider", lambda *a, **k: provider)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "toolwire")
            conv = await _new_conversation(client, headers)
            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "我该吃多少蛋白"})

            events = _parse_sse(r.text)
            start = next(d for n, d in events if n == "tool_start")
            assert start == {"label": "计算营养素分配"}
            assert "calc_macros" not in r.text, "内部工具名出现在了 SSE 报文里"
            assert "tdee" not in r.text, "工具参数出现在了 SSE 报文里"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_client_message_id(self, monkeypatch):
        import app.services.chat_service as svc

        monkeypatch.setattr(svc, "build_provider",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "idem")
            conv = await _new_conversation(client, headers)
            body = {"text": "卧推 80kg 5x5", "client_message_id": "fixed-id-1"}

            await client.post(f"/api/v1/conversations/{conv}/messages",
                              headers=headers, json=body)
            r2 = await client.post(f"/api/v1/conversations/{conv}/messages",
                                   headers=headers, json=body)

            done = next(d for n, d in _parse_sse(r2.text) if n == "message_done")
            assert done["deduplicated"] is True

            msgs = (await client.get(f"/api/v1/conversations/{conv}/messages",
                                     headers=headers)).json()
            assert len([m for m in msgs if m["role"] == "user"]) == 1


class TestIsolation:
    @pytest.mark.asyncio
    async def test_cannot_read_other_users_conversation(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers_a = await _auth(client, "iso-a")
            headers_b = await _auth(client, "iso-b")
            conv = await _new_conversation(client, headers_a)

            r = await client.get(f"/api/v1/conversations/{conv}/messages", headers=headers_b)
            assert r.status_code == 404, "B 不该能读到 A 的会话"

    @pytest.mark.asyncio
    async def test_conversation_list_is_scoped(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers_a = await _auth(client, "list-a")
            headers_b = await _auth(client, "list-b")
            await _new_conversation(client, headers_a)

            assert (await client.get("/api/v1/conversations", headers=headers_b)).json() == []

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/v1/conversations")).status_code == 401


class TestInterruptEndpoint:
    @pytest.mark.asyncio
    async def test_sets_the_flag_for_that_conversation(self):
        from app.services import interrupt

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-flag")
            conv = await _new_conversation(client, headers)

            r = await client.post(f"/api/v1/conversations/{conv}/interrupt", headers=headers)
            assert r.status_code == 204, r.text
            try:
                assert interrupt.is_requested(uuid.UUID(conv)) is True
            finally:
                interrupt.clear(uuid.UUID(conv))

    @pytest.mark.asyncio
    async def test_cannot_interrupt_another_users_conversation(self):
        """否则任何人都能掐断别人正在生成的回复。"""
        from app.services import interrupt

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers_a = await _auth(client, "int-a")
            headers_b = await _auth(client, "int-b")
            conv = await _new_conversation(client, headers_a)

            r = await client.post(f"/api/v1/conversations/{conv}/interrupt", headers=headers_b)
            assert r.status_code == 404, "B 不该能中断 A 的会话"
            assert interrupt.is_requested(uuid.UUID(conv)) is False, "404 了还是插了旗"

    @pytest.mark.asyncio
    async def test_nonexistent_conversation_is_404(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-404")
            r = await client.post(
                f"/api/v1/conversations/{uuid.uuid4()}/interrupt", headers=headers,
            )
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(f"/api/v1/conversations/{uuid.uuid4()}/interrupt")
            assert r.status_code == 401


class TestDegradedPath:
    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rules(self, monkeypatch):
        """模型全挂时端到端仍能出正确数字。"""
        import app.services.chat_service as svc
        from app.core.llm.client import ChatRequest, LLMError

        class DeadProvider:
            model = "dead"

            async def stream(self, req: ChatRequest):
                raise LLMError("模型服务不可用")
                yield  # pragma: no cover

        monkeypatch.setattr(svc, "build_provider", lambda *a, **k: DeadProvider())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "degraded")
            conv = await _new_conversation(client, headers)

            r = await client.post(f"/api/v1/conversations/{conv}/messages", headers=headers,
                                  json={"text": "我该吃多少"})
            events = _parse_sse(r.text)
            done = next(d for n, d in events if n == "message_done")
            assert done["degradedTo"] == 4
