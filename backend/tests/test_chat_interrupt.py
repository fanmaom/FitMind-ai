"""中断一个正在生成的回合。

登记表本身的单测在 test_interrupt.py；这里测的是「中断之后落库成什么样」——
真中断和假中断的分水岭全在这几条断言上。
"""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.llm.client import ChatChunk
from app.main import app


async def _auth(client: AsyncClient, tag: str) -> dict:
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


class InterruptingProvider:
    """吐到第 interrupt_at 块时自己插旗，模拟用户中途点了停止。

    生成器是懒的：插旗那行只在消费者来取第 interrupt_at 块时才执行，所以
    「前面几块已经落地、这一块起不算」的时序和真实中断完全一致。
    """

    model = "fake-model"
    CHUNKS = ("一", "二", "三", "四")

    def __init__(self, conversation_id: str, interrupt_at: int = 2) -> None:
        self.conversation_id = uuid.UUID(conversation_id)
        self.interrupt_at = interrupt_at

    async def stream(self, req):
        from app.services import interrupt

        for i, text in enumerate(self.CHUNKS):
            if i == self.interrupt_at:
                interrupt.request(self.conversation_id)
            # usage 挂在第 2 块上：证明「中断前已收到的 usage」不会丢
            usage = {"prompt_tokens": 11, "completion_tokens": 4} if i == 1 else None
            yield ChatChunk(text_delta=text, usage=usage)


async def _run_interrupted_turn(client: AsyncClient, headers: dict, monkeypatch):
    """跑一个会被中途中断的回合，返回 (conv_id, sse_events, provider)。"""
    import app.services.chat_service as svc

    conv = await _new_conversation(client, headers)
    provider = InterruptingProvider(conv)
    monkeypatch.setattr(svc, "build_provider", lambda *a, **k: provider)

    r = await client.post(
        f"/api/v1/conversations/{conv}/messages",
        headers=headers,
        # 刻意不用能命中快路径的文本：快路径不碰模型，也就无从中断
        json={"text": "帮我讲讲增肌该怎么吃"},
    )
    assert r.status_code == 200, r.text
    return conv, _parse_sse(r.text), provider


class TestInterruptedTurnPersistence:
    @pytest.mark.asyncio
    async def test_persists_partial_text_with_interrupted_status(self, monkeypatch):
        """已推给用户的字要留住，插旗那块起的不能留。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-persist")
            conv, _, _ = await _run_interrupted_turn(client, headers, monkeypatch)

            msgs = (await client.get(
                f"/api/v1/conversations/{conv}/messages", headers=headers,
            )).json()
            assistant = msgs[1]
            assert assistant["status"] == "interrupted"
            assert assistant["content"]["text"] == "一二", "只该留下中断前推出去的字"

    @pytest.mark.asyncio
    async def test_does_not_persist_a_half_written_internal_name(self, monkeypatch):
        """脱敏靠"扣住结尾等边界"实现。停止正好落在标识符中间时，那半截既替
        不掉（词表里没有 log_work）也不能落库——库里的文本会在重连时显示给
        用户，下一轮还会当历史喂回模型。"""
        import app.services.chat_service as svc

        class LeakyInterrupting(InterruptingProvider):
            CHUNKS = ("这条得用 log_work", "out 存起来", "还有更多")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-halfname")
            conv = await _new_conversation(client, headers)
            monkeypatch.setattr(svc, "build_provider",
                                lambda *a, **k: LeakyInterrupting(conv, interrupt_at=1))

            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "帮我讲讲增肌该怎么吃"})
            assert r.status_code == 200, r.text
            assert "log_work" not in r.text, "半截工具名推给了用户"

            msgs = (await client.get(f"/api/v1/conversations/{conv}/messages",
                                     headers=headers)).json()
            assert msgs[1]["content"]["text"] == "这条得用 "

    @pytest.mark.asyncio
    async def test_emits_interrupted_event_not_message_done(self, monkeypatch):
        """前端要能区分「停止了」和「正常说完了」，否则渲染不出角标。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-event")
            _, events, _ = await _run_interrupted_turn(client, headers, monkeypatch)

            names = [n for n, _ in events]
            assert "interrupted" in names
            assert "message_done" not in names

    @pytest.mark.asyncio
    async def test_records_usage_row(self, monkeypatch):
        """token 是真烧了的，账得记。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-usage")
            await _run_interrupted_turn(client, headers, monkeypatch)

            summary = (await client.get("/api/v1/usage/summary", headers=headers)).json()
            assert summary["request_count"] == 1

    @pytest.mark.asyncio
    async def test_does_not_enqueue_memory_extraction(self, monkeypatch):
        """从半句话里抽长期事实会污染 L2 记忆库。"""
        from sqlalchemy import select

        from app.core.database import async_session_maker, bind_rls_user
        from app.core.security import decode_token
        from app.models.job import Job

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-nojob")
            await _run_interrupted_turn(client, headers, monkeypatch)

            uid = uuid.UUID(decode_token(headers["Authorization"].removeprefix("Bearer ")))
            session = async_session_maker()
            try:
                bind_rls_user(session, uid)
                jobs = (await session.scalars(
                    select(Job).where(Job.type == "extract_memory"),
                )).all()
                assert jobs == [], "中断的回合不该投递记忆抽取"
            finally:
                await session.close()

    @pytest.mark.asyncio
    async def test_explicitly_closes_the_event_generator(self, monkeypatch):
        """中断后必须显式 aclose，不能指望 GC。

        CPython 的引用计数确实会很快回收 break 掉的生成器，所以"漏不漏"用
        provider 的 finally 是测不出来的——那条断言无论有没有 aclose 都成立。
        真正的理由是**确定性**：清理必须发生在本请求的事件循环里。若留给 GC，
        异常传播时的 traceback 会持有帧、形成引用环，回收被推迟到循环可能已经
        关闭之后，那时生成器 finally 里的 await 会炸 "Event loop is closed"。

        所以这里钉的就是"我们调了 aclose"——把 aclose 那行删掉，此测试转红。
        """
        import app.services.chat_service as svc
        from app.core.agent.loop import AgentLoop

        closed: list[bool] = []
        original_run = AgentLoop.run

        def spying_run(self, **kwargs):
            inner = original_run(self, **kwargs)

            class Spy:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return await inner.__anext__()

                async def aclose(self):
                    closed.append(True)
                    await inner.aclose()

            return Spy()

        monkeypatch.setattr(AgentLoop, "run", spying_run)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-close")
            conv = await _new_conversation(client, headers)
            provider = InterruptingProvider(conv)
            monkeypatch.setattr(svc, "build_provider", lambda *a, **k: provider)

            r = await client.post(
                f"/api/v1/conversations/{conv}/messages",
                headers=headers, json={"text": "帮我讲讲增肌该怎么吃"},
            )
            assert r.status_code == 200, r.text

        assert closed == [True], "chat_service 没有显式关闭事件生成器"

    @pytest.mark.asyncio
    async def test_clears_the_flag_so_next_turn_survives(self, monkeypatch):
        """旗子留着的话，下一个回合会被上一次的中断当场杀掉。"""
        from app.services import interrupt

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "int-clear")
            conv, _, _ = await _run_interrupted_turn(client, headers, monkeypatch)

            assert interrupt.is_requested(uuid.UUID(conv)) is False


class TestInterruptedMessageInHistory:
    @pytest.mark.asyncio
    async def test_load_history_includes_interrupted_messages(self, db, seeded_user):
        """你看见的那半句，模型也得看见——不然下一轮它会自相矛盾。"""
        from app.models.conversation import Conversation
        from app.models.message import Message
        from app.services.chat_service import load_history

        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()

        db.add(Message(
            conversation_id=conv.id, user_id=seeded_user, role="assistant",
            content={"text": "说到一半被停"}, status="interrupted",
        ))
        await db.flush()

        history = await load_history(db, conv.id)
        assert [m["content"] for m in history] == ["说到一半被停"]
