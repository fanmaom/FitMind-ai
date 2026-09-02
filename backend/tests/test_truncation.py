"""输出截断必须让用户看得见。

两种截断此前都只写日志：
  - finish_reason == "length"：模型撞上单次输出上限，话说到一半
  - Agent 轮次用尽：事情没做完

用户看到的是一句半截话，没有任何提示——比报错更难查，因为它看起来就像模型
答完了。用户会以为这就是完整回答，然后基于半截信息去训练。
"""

import inspect
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.agent.loop import AgentLoop
from app.core.llm.client import ChatChunk, ToolCall
from app.core.llm.with_fallback import FallbackProvider
from app.main import app

from tests.test_agent_loop import ScriptedProvider


async def _events(provider, **kw) -> list:
    loop = AgentLoop(FallbackProvider(provider, backoff_s=(0.0, 0.0)), tool_ctx=None, **kw)
    return [
        event async for event in loop.run(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            profile={}, user_text="hi",
        )
    ]


class TestLoopReportsTruncation:
    @pytest.mark.asyncio
    async def test_output_limit_is_reported(self):
        """撞上 max_tokens 时要报出来。这一路不能靠重试解决——正文已经推给
        用户了，重发会重复已经看见的字。"""
        provider = ScriptedProvider([[
            ChatChunk(text_delta="按你的体重，蛋白目标是"),
            ChatChunk(finish_reason="length"),
        ]])
        done = next(e for e in await _events(provider) if e.type == "done")
        assert done.data["output_truncated"] is True
        assert done.data["truncated"] is False, "输出截断被误报成轮次用尽"

    @pytest.mark.asyncio
    async def test_normal_stop_reports_neither(self):
        provider = ScriptedProvider([[
            ChatChunk(text_delta="说完了"), ChatChunk(finish_reason="stop"),
        ]])
        done = next(e for e in await _events(provider) if e.type == "done")
        assert done.data["output_truncated"] is False
        assert done.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_empty_output_with_length_is_not_reported(self):
        """有 length 但一个字都没产出，是降级层要重试的场景（思考预算被吃光），
        不是"说了一半"。报成截断会让用户看到一条空消息配一句"没说完"。"""
        provider = ScriptedProvider(
            [
                [ChatChunk(finish_reason="length")],
                [ChatChunk(text_delta="重试成功"), ChatChunk(finish_reason="stop")],
            ],
        )
        events = await _events(provider)
        done = next(e for e in events if e.type == "done")
        assert done.data["output_truncated"] is False

    @pytest.mark.asyncio
    async def test_turn_limit_is_reported(self):
        """轮次用尽是另一种截断：话说完了，但事情没做完。"""
        scripts = [
            [ChatChunk(
                tool_calls=[ToolCall(id=f"c{i}", name="calc_macros", arguments={})],
                finish_reason="tool_calls",
            )]
            for i in range(4)
        ]
        events = await _events(ScriptedProvider(scripts), max_turns=2, tool_timeout_s=1.0)
        done = next(e for e in events if e.type == "done")
        assert done.data["truncated"] is True
        assert done.data["output_truncated"] is False

    @pytest.mark.asyncio
    async def test_all_done_events_carry_both_flags(self):
        """字段形状要一致。少一个键前端就得写 ?? false，那种默认值最后总会
        掩盖掉真实的截断。"""
        for provider in (
            ScriptedProvider([[ChatChunk(text_delta="x"), ChatChunk(finish_reason="stop")]]),
            ScriptedProvider([[]]),
        ):
            for event in await _events(provider):
                if event.type == "done":
                    assert "truncated" in event.data
                    assert "output_truncated" in event.data


class TestPersistenceAndTransport:
    async def _run(self, monkeypatch, chunks: list[ChatChunk]) -> tuple[dict, list[dict]]:
        import app.services.chat_service as service

        monkeypatch.setattr(
            service, "build_provider",
            lambda *_a, **_kw: ScriptedProvider([chunks]),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            register = await client.post("/api/v1/auth/register", json={
                "email": f"trunc-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456",
            })
            headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
            conv = (await client.post("/api/v1/conversations", headers=headers)).json()["id"]
            response = await client.post(
                f"/api/v1/conversations/{conv}/messages",
                headers=headers, json={"text": "帮我算营养素"},
            )
            body = response.text
            rows = (await client.get(
                f"/api/v1/conversations/{conv}/messages", headers=headers,
            )).json()
        return {"sse": body}, rows

    @pytest.mark.asyncio
    async def test_truncation_reaches_sse_and_database(self, monkeypatch):
        """既要发给前端，也要落库：断线重连走 list_messages 取回历史，
        只发不存的话重连后那句半截话又变得"看起来正常"了。"""
        sent, rows = await self._run(monkeypatch, [
            ChatChunk(text_delta="蛋白目标是"), ChatChunk(finish_reason="length"),
        ])
        assert "truncated" in sent["sse"], "message_done 没有带上截断标记"

        assistant = [r for r in rows if r["role"] == "assistant"][-1]
        assert assistant["content"].get("truncated") == ["output"], (
            "截断标记没有落库，重连后提示会消失"
        )

    @pytest.mark.asyncio
    async def test_normal_reply_has_no_truncation_key(self, monkeypatch):
        """正常消息不带这个键。无条件写会让每条消息的 meta 里多两个 false，
        历史与重连响应里也就多两份噪声。"""
        sent, rows = await self._run(monkeypatch, [
            ChatChunk(text_delta="算好了"), ChatChunk(finish_reason="stop"),
        ])
        assistant = [r for r in rows if r["role"] == "assistant"][-1]
        assert "truncated" not in assistant["content"]


class TestFrontendWiring:
    """后端报了但前端不显示，等于没做。这几条守住接线。"""

    def _read(self, path: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / "web" / "src" / path).read_text()

    def test_store_accepts_truncated(self):
        assert "truncated" in self._read("stores/chat.ts")

    def test_message_component_renders_both_kinds(self):
        source = self._read("components/chat/ChatMessage.tsx")
        assert "message.truncated" in source
        # 两种成因能做的事相反：一种让用户说"继续"，另一种要把问题拆小。
        # 合并成一句"回答不完整"会把后者引向无效操作。
        assert "output:" in source and "turns:" in source

    def test_view_passes_truncated_from_sse(self):
        source = self._read("components/chat/ChatView.tsx")
        assert "data.truncated" in source

    def test_reconnect_restores_truncated(self):
        """重连恢复路径也要带上，否则刷新一次提示就没了。"""
        source = self._read("components/chat/ChatView.tsx")
        assert "r.content.truncated" in source

    def test_chat_service_forwards_both_flags(self):
        import app.services.chat_service as service

        source = inspect.getsource(service._run_turn_inner)
        assert "output_truncated" in source
        assert '"turns"' in source
