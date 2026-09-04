"""MCP 服务端：把这个助理的工具暴露给外部 MCP 客户端。

方向与 test_mcp.py 相反——那边是"我接别人的"，这边是"别人接我的"。

这组测试盯的是三类风险：
  1. 协议层：帧格式错了客户端整个不可用，而它只会报一句 "unexpected token"
  2. 越权：白名单漏了就等于把写操作暴露给一个 token 明文躺在配置文件里的入口
  3. 信息泄漏：异常里可能带 SQL 片段、表名、连接串
"""

import asyncio
import io
import json
import uuid

import pytest

from app.core.mcp.server import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_NAME,
    MCPServer,
)
from app.core.tools.registry import load_tools, registry


@pytest.fixture(autouse=True)
def _tools_loaded():
    load_tools()


@pytest.fixture
def server(seeded_user) -> MCPServer:
    return MCPServer(seeded_user)


def _req(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class TestHandshake:
    @pytest.mark.asyncio
    async def test_initialize_returns_protocol_and_capabilities(self, server):
        response = await server.handle(_req("initialize", {}))
        result = response["result"]
        assert result["protocolVersion"] == PROTOCOL_VERSION
        # 声明 tools 能力，客户端才会去调 tools/list
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == SERVER_NAME

    @pytest.mark.asyncio
    async def test_response_carries_the_same_id(self, server):
        response = await server.handle(_req("initialize", {}, request_id=42))
        assert response["id"] == 42
        assert response["jsonrpc"] == "2.0"

    @pytest.mark.asyncio
    async def test_notification_gets_no_response(self, server):
        """通知没有 id，回了响应客户端会当成协议错误。"""
        assert await server.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }) is None

    @pytest.mark.asyncio
    async def test_ping_is_answered(self, server):
        """规范里的保活方法。不认它，有些客户端会判定连接已死。"""
        response = await server.handle(_req("ping"))
        assert response["result"] == {}

    @pytest.mark.asyncio
    async def test_unknown_method_uses_standard_error_code(self, server):
        """用标准码而不是自定义：客户端据此决定要不要重试，
        自定义码它只能当成未知错误。"""
        response = await server.handle(_req("tools/nonexistent"))
        assert response["error"]["code"] == METHOD_NOT_FOUND


class TestToolsList:
    @pytest.mark.asyncio
    async def test_lists_tools_with_schema(self, server):
        tools = (await server.handle(_req("tools/list")))["result"]["tools"]
        assert tools
        for tool in tools:
            assert tool["name"]
            assert tool["description"]
            # inputSchema 是模型填参数的唯一依据，缺了它工具就没法用
            assert tool["inputSchema"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_description_marks_the_owning_system(self, server):
        """外部客户端的模型只看 description。不标明来源，它会拿 calc_macros
        去算与健身无关的东西。"""
        tools = (await server.handle(_req("tools/list")))["result"]["tools"]
        assert all(f"[{SERVER_NAME}]" in t["description"] for t in tools)

    @pytest.mark.asyncio
    async def test_schema_is_the_real_one(self, server):
        """schema 必须是 Pydantic 导出的那份，不是占位符——带上枚举值和范围
        才能让对方的模型填对参数。"""
        tools = {t["name"]: t for t in (await server.handle(_req("tools/list")))["result"]["tools"]}
        macros = tools["calc_macros"]["inputSchema"]
        assert "tdee" in macros["properties"]
        assert "goal" in macros["properties"]


class TestReadonlyWhitelist:
    """默认只暴露只读工具。

    理由不是"怕出 bug"，是风险与收益不对称：token 明文躺在客户端配置文件里，
    泄漏后果是"数据被改"还是"数据被读"，差别很大。
    """

    @pytest.mark.asyncio
    async def test_write_tools_are_hidden_by_default(self, server):
        names = {t["name"] for t in (await server.handle(_req("tools/list")))["result"]["tools"]}
        for write_tool in ("log_workout", "log_body_metric", "update_profile"):
            assert write_tool not in names, f"{write_tool} 是写操作，默认不该暴露"

    @pytest.mark.asyncio
    async def test_readonly_tools_are_visible(self, server):
        names = {t["name"] for t in (await server.handle(_req("tools/list")))["result"]["tools"]}
        for read_tool in ("calc_macros", "query_workout_history", "get_plan_day"):
            assert read_tool in names

    @pytest.mark.asyncio
    async def test_whitelist_is_derived_not_handwritten(self, server):
        """按 ToolSpec.readonly 自动筛。手写名单迟早和新增工具脱节——
        脱节的方向还很糟：新工具默认不在名单里，看起来"安全"，
        于是没人发现它压根没暴露。"""
        exposed = {s.name for s in server._exposed_specs()}
        expected = {
            s.name for s in registry.all()
            if s.readonly and not s.name.startswith("mcp__")
        }
        assert exposed == expected

    @pytest.mark.asyncio
    async def test_hidden_tool_cannot_be_called(self, server):
        """光在清单里藏起来不够——直接按名字调也必须拒绝。"""
        response = await server.handle(_req("tools/call", {
            "name": "update_profile",
            "arguments": {"updates": {"weight_kg": 1}, "confirmed": True},
        }))
        assert response["result"]["isError"] is True

    @pytest.mark.asyncio
    async def test_hidden_and_nonexistent_look_the_same(self, server):
        """两者要给出同一种说法。

        区分了就等于告诉调用方"这个工具存在但你不能用"，是一条不必要的信息
        泄漏——对方能靠它探出这个系统有哪些写操作。

        比的是措辞模板而不是完整文本：消息里会带上对方请求的那个名字，
        那部分本来就不同（也应该不同，否则对方不知道是哪个调用失败了）。
        """
        hidden = await server.handle(_req("tools/call", {
            "name": "update_profile", "arguments": {},
        }))
        missing = await server.handle(_req("tools/call", {
            "name": "no_such_tool_at_all", "arguments": {},
        }))

        def template(response: dict, name: str) -> str:
            return response["result"]["content"][0]["text"].replace(name, "<NAME>")

        assert template(hidden, "update_profile") == template(missing, "no_such_tool_at_all")
        # 而且措辞不能暗示"存在但被禁用"
        text = hidden["result"]["content"][0]["text"]
        for leak in ("禁用", "不允许", "无权", "只读"):
            assert leak not in text, f"措辞泄漏了工具存在的事实：{text}"

    @pytest.mark.asyncio
    async def test_write_mode_exposes_them(self, seeded_user):
        writable = MCPServer(seeded_user, readonly_only=False)
        names = {t["name"] for t in (await writable.handle(_req("tools/list")))["result"]["tools"]}
        assert "log_workout" in names

    @pytest.mark.asyncio
    async def test_external_mcp_tools_are_never_forwarded(self, seeded_user):
        """从别的 server 借来的工具不转出去：会形成一条谁也说不清的调用链，
        而且那些 server 的副作用我们无从判断。"""
        from pydantic import BaseModel

        from app.core.tools.registry import ToolSpec

        class Args(BaseModel):
            pass

        async def handler(inp, ctx):
            return {}

        borrowed = ToolSpec(
            name="mcp__other__search", label="借来的", description="d",
            input_model=Args, handler=handler, readonly=True, needs_confirm=False,
        )
        registry.register(borrowed)
        try:
            server = MCPServer(seeded_user)
            names = {
                t["name"]
                for t in (await server.handle(_req("tools/list")))["result"]["tools"]
            }
            assert "mcp__other__search" not in names
        finally:
            registry._specs.pop("mcp__other__search", None)  # noqa: SLF001


class TestToolCall:
    @pytest.mark.asyncio
    async def test_successful_call_returns_text_content(self, server):
        response = await server.handle(_req("tools/call", {
            "name": "calc_macros",
            "arguments": {"tdee": 2500, "goal": "cut", "weight_kg": 82},
        }))
        result = response["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["protein_g"] > 0

    @pytest.mark.asyncio
    async def test_card_is_stripped(self, server):
        """卡片是本系统前端的东西，对外部客户端没有意义。"""
        response = await server.handle(_req("tools/call", {
            "name": "check_plan_conflict",
            "arguments": {"requested_goal": "bulk", "wants_strength_gain": True},
        }))
        text = response["result"]["content"][0]["text"]
        assert "__card__" not in text

    @pytest.mark.asyncio
    async def test_bad_arguments_return_is_error_not_rpc_error(self, server):
        """参数错误是"工具执行失败"，对方的模型应该看到它并自行改参数重试。
        回 JSON-RPC error 会被当成传输层故障，模型不会尝试修正。"""
        response = await server.handle(_req("tools/call", {
            "name": "calc_macros", "arguments": {"tdee": -1, "goal": "cut", "weight_kg": 82},
        }))
        assert "error" not in response
        assert response["result"]["isError"] is True

    @pytest.mark.asyncio
    async def test_error_text_does_not_leak_internals(self, server, monkeypatch):
        """异常里可能带 SQL 片段、表名、连接串。全貌留在日志，回给客户端
        的只有类型名。"""
        async def exploding(*_a, **_kw):
            raise RuntimeError("connection to server at 10.0.0.5 failed: FATAL password")

        monkeypatch.setattr(registry, "invoke", exploding)
        response = await server.handle(_req("tools/call", {
            "name": "calc_macros",
            "arguments": {"tdee": 2500, "goal": "cut", "weight_kg": 82},
        }))
        text = response["result"]["content"][0]["text"]
        assert response["result"]["isError"] is True
        assert "10.0.0.5" not in text
        assert "password" not in text
        assert "RuntimeError" in text

    @pytest.mark.asyncio
    async def test_missing_arguments_key_is_tolerated(self, server):
        """有些客户端在无参工具上会省掉 arguments。"""
        response = await server.handle(_req("tools/call", {"name": "calc_macros"}))
        assert response["result"]["isError"] is True  # 参数不全，但不能崩


class TestIdentityAndIsolation:
    """身份从 token 来，隔离靠 RLS。没有 user_id 一行都读不到。"""

    @pytest.mark.asyncio
    async def test_reads_are_scoped_to_the_token_owner(self, db, seeded_user):
        """A 的数据不能被 B 的 server 读到。"""
        from datetime import date

        from app.models.workout_log import WorkoutLog

        db.add(WorkoutLog(
            user_id=seeded_user, date=date.today(), exercise="卧推",
            sets=[{"weight": 80, "reps": 5}], idempotency_key=uuid.uuid4().hex,
        ))
        await db.commit()

        mine = MCPServer(seeded_user)
        response = await mine.handle(_req("tools/call", {
            "name": "query_workout_history",
            "arguments": {"exercise": "卧推", "weeks": 4},
        }))
        assert "卧推" in response["result"]["content"][0]["text"]

        stranger = MCPServer(uuid.uuid4())
        response = await stranger.handle(_req("tools/call", {
            "name": "query_workout_history",
            "arguments": {"exercise": "卧推", "weeks": 4},
        }))
        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload.get("session_count", 0) == 0, "另一个用户读到了不属于他的训练记录"

    @pytest.mark.asyncio
    async def test_each_call_uses_a_fresh_session(self, server):
        """长驻进程里复用一个 session 会累积未回滚的事务状态，
        而这里没有 Web 框架的请求边界来兜底。"""
        import inspect

        source = inspect.getsource(MCPServer._call)
        assert "session.close()" in source
        assert "_make_context" in source


class TestFraming:
    """帧格式错了客户端整个不可用，而它只会报一句 "unexpected token"。"""

    async def _run(self, server: MCPServer, lines: list[str]) -> list[dict]:
        reader = asyncio.StreamReader()
        for line in lines:
            reader.feed_data(line.encode())
        reader.feed_eof()
        out = io.StringIO()
        await server.serve(reader, out)
        return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]

    @pytest.mark.asyncio
    async def test_one_json_per_line(self, server):
        responses = await self._run(server, [
            json.dumps(_req("initialize", {}, 1)) + "\n",
            json.dumps(_req("tools/list", None, 2)) + "\n",
        ])
        assert [r["id"] for r in responses] == [1, 2]

    @pytest.mark.asyncio
    async def test_blank_lines_are_skipped(self, server):
        responses = await self._run(server, [
            "\n", "  \n", json.dumps(_req("ping", None, 7)) + "\n",
        ])
        assert len(responses) == 1
        assert responses[0]["id"] == 7

    @pytest.mark.asyncio
    async def test_malformed_json_still_gets_a_response(self, server):
        """不回的话客户端会一直等到超时。"""
        responses = await self._run(server, ["{not json\n"])
        assert responses[0]["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_non_object_message_is_rejected(self, server):
        responses = await self._run(server, ["[1,2,3]\n"])
        assert "error" in responses[0]

    @pytest.mark.asyncio
    async def test_one_bad_message_does_not_kill_the_loop(self, server):
        """退出会让客户端失去所有工具，而问题可能只是一条畸形请求。"""
        responses = await self._run(server, [
            "{broken\n",
            json.dumps(_req("ping", None, 9)) + "\n",
        ])
        assert len(responses) == 2
        assert responses[1]["id"] == 9

    @pytest.mark.asyncio
    async def test_notification_produces_no_line(self, server):
        responses = await self._run(server, [
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
            json.dumps(_req("ping", None, 3)) + "\n",
        ])
        assert len(responses) == 1

    @pytest.mark.asyncio
    async def test_stdin_close_ends_the_loop(self, server):
        """客户端关闭子进程时的正常退出路径。"""
        assert await self._run(server, []) == []


class TestStdoutStaysClean:
    """stdout 是协议通道，一行日志就能毁掉它。

    logger 默认写 stdout，而 load_tools() 在注册工具时就会打一行 INFO。那一行
    会直接混进协议流，客户端只会报 "unexpected token"——而真正的原因是一条毫不
    相关的日志。这是这个功能最容易漏、后果最严重的一点。
    """

    def test_route_to_stderr_exists(self):
        from app.core.logger import route_to_stderr

        assert callable(route_to_stderr)

    def test_entrypoint_routes_logs_before_importing_business_code(self):
        """顺序是关键：route_to_stderr() 必须在导入业务模块之前执行。
        晚了的话 load_tools 那行日志已经写进 stdout 了。
        """
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "app" / "mcp_stdio.py"
        lines = [
            line.strip() for line in source.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        route_at = next(i for i, l in enumerate(lines) if l == "route_to_stderr()")
        server_import_at = next(
            i for i, l in enumerate(lines) if "from app.core.mcp.server import" in l
        )
        assert route_at < server_import_at, (
            "route_to_stderr() 在导入业务模块之后才调用，"
            "load_tools 的日志已经污染了 stdout"
        )

    def test_route_to_stderr_actually_redirects(self, capsys):
        """真的改道了，而不是只加了一个 sink。"""
        from app.core.logger import logger, route_to_stderr

        route_to_stderr()
        try:
            logger.info("这行必须去 stderr")
            captured = capsys.readouterr()
            assert "这行必须去 stderr" not in captured.out
            assert "这行必须去 stderr" in captured.err
        finally:
            # 恢复默认，别影响其他测试
            import importlib

            import app.core.logger as logger_module

            importlib.reload(logger_module)
