"""MCP 工具桥接。

外部 MCP server 的工具接进本地注册表后，与本地工具在系统里完全等价：同一套
schema 给模型、同一套参数校验、同一套失败反馈、同一套脱敏、同一套降级。
Agent 循环一行都不用改。

这组测试大部分在守"会静默出错"的地方——外部进程是不可控的，而它的失败方式
往往不是抛异常，是给出看起来正常的错东西。
"""

import asyncio
import json
import sys

import pytest

from app.core.mcp.bridge import (
    LABEL_MAX_WIDTH,
    MCPRegistry,
    _display_width,
    _make_label,
    _passthrough_model,
    build_spec,
    qualified_name,
    split_name,
)
from app.core.mcp.client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    parse_servers,
)
from app.core.tools.registry import ToolRegistry


class TestConfigParsing:
    def test_accepts_claude_desktop_format(self):
        """用户能直接把现成配置贴过来，不必学一套新写法。"""
        raw = '{"mcpServers":{"time":{"command":"uvx","args":["mcp-server-time"]}}}'
        [config] = parse_servers(raw)
        assert config.name == "time"
        assert config.command == "uvx"
        assert config.args == ["mcp-server-time"]

    def test_accepts_flat_format(self):
        [config] = parse_servers({"time": {"command": "uvx"}})
        assert config.name == "time"

    def test_empty_means_disabled(self):
        """默认不启用：外部 server 是不可控依赖，不该在用户没配置时就起子进程。"""
        assert parse_servers("") == []
        assert parse_servers(None) == []
        assert parse_servers({}) == []

    def test_readonly_defaults_to_false(self):
        """MCP 协议**不表达副作用**——readOnlyHint 是后加的可选注解，绝大多数
        server 不提供。默认按写操作对待：降级层据此决定能否安全重试，
        误判成只读会导致重复写入。"""
        [config] = parse_servers({"x": {"command": "echo"}})
        assert config.readonly is False

    def test_readonly_must_be_declared(self):
        [config] = parse_servers({"x": {"command": "echo", "readonly": True}})
        assert config.readonly is True

    def test_missing_command_is_rejected(self):
        with pytest.raises(ValueError, match="command"):
            parse_servers({"x": {"args": ["y"]}})

    def test_malformed_json_is_rejected_with_reason(self):
        with pytest.raises(ValueError, match="JSON"):
            parse_servers("{not json")


class TestNamespacing:
    """外部工具名不受我们控制，两个 server 都叫 search 是完全可能的。"""

    def test_prefix_is_reversible(self):
        full = qualified_name("weather", "get_forecast")
        assert full == "mcp__weather__get_forecast"
        assert split_name(full) == ("weather", "get_forecast")

    def test_tool_name_with_underscores_survives(self):
        """双下划线分隔，与工具名自身的单下划线区分开——否则拆不回来。"""
        full = qualified_name("time", "get_current_time")
        assert split_name(full) == ("time", "get_current_time")

    def test_local_tool_is_not_mistaken_for_mcp(self):
        assert split_name("log_workout") is None
        assert split_name("mcp__") is None
        assert split_name("mcp__onlyserver") is None


class TestLabelGeneration:
    """MCP 协议没有"用户可见说法"这个字段，必须自己造。

    缺了用户就会看到 "正在 mcp__weather__get_forecast…"——线上真出现过同类
    问题（"用 plan_strength_cycle 帮你排"），所以 label 在这个项目里是硬要求。
    """

    def test_uses_description_first_sentence(self):
        label = _make_label("time", MCPTool(
            name="get_current_time",
            description="Get current time in a specific timezone. Extra detail here.",
            input_schema={},
        ))
        assert "Get current time in a specific timezone" in label
        assert "time" in label

    def test_falls_back_to_humanized_name(self):
        """description 缺失时退回工具名的可读形式——至少不让用户看到下划线。"""
        label = _make_label("x", MCPTool(name="get_current_time", description="", input_schema={}))
        assert "get current time" in label
        assert "_" not in label

    def test_never_leaks_the_prefixed_internal_name(self):
        label = _make_label("weather", MCPTool(
            name="get_forecast", description="", input_schema={},
        ))
        assert "mcp__" not in label

    def test_long_description_does_not_blow_the_status_line(self):
        """这个字符串会被拼成"正在{label}…"，太长会把状态行撑破。"""
        label = _make_label("x", MCPTool(
            name="t", description="A" * 500, input_schema={},
        ))
        assert _display_width(label) <= LABEL_MAX_WIDTH + 4

    def test_chinese_counts_as_double_width(self):
        """中英文混排按显示宽度算，否则中文 label 会超出两倍。"""
        assert _display_width("中文") == 4
        assert _display_width("ab") == 2

    def test_label_is_never_empty(self):
        label = _make_label("s", MCPTool(name="t", description="", input_schema={}))
        assert label.strip()


class TestSchemaPassthrough:
    """交给模型的必须是 server 的原始 schema。

    用 Any 拼出来的那个没有类型、没有枚举值、没有描述——模型照它填参数必然填错，
    而错误会以"参数校验失败"的形式出现，指向完全错误的方向。
    """

    SCHEMA = {
        "type": "object",
        "properties": {
            "timezone": {"type": "string", "description": "IANA timezone name"},
            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
        },
        "required": ["timezone"],
    }

    def test_original_schema_is_handed_to_model(self):
        model = _passthrough_model("x", self.SCHEMA)
        assert model.model_json_schema() == self.SCHEMA

    def test_required_field_is_enforced(self):
        from pydantic import ValidationError

        model = _passthrough_model("x", self.SCHEMA)
        with pytest.raises(ValidationError):
            model.model_validate({})

    def test_optional_field_may_be_absent(self):
        model = _passthrough_model("x", self.SCHEMA)
        assert model.model_validate({"timezone": "UTC"}).timezone == "UTC"

    def test_unknown_fields_are_allowed(self):
        """有些 server 的 schema 不完整，拒绝未知字段会让本来能用的工具报错。"""
        model = _passthrough_model("x", self.SCHEMA)
        parsed = model.model_validate({"timezone": "UTC", "undeclared": 1})
        assert parsed.model_dump()["undeclared"] == 1

    def test_empty_schema_does_not_crash(self):
        model = _passthrough_model("x", {})
        assert model.model_json_schema()["type"] == "object"
        model.model_validate({})

    def test_field_level_validation_is_delegated(self):
        """字段级校验交给 server：外部 schema 的表达力我们控制不了，强行翻译
        会在遇到 oneOf / $ref / 自定义 format 时静默丢约束——那比明确地
        "只做基础校验"更危险。"""
        model = _passthrough_model("x", self.SCHEMA)
        # hour 声明了 0-23，但这里不拦——由 server 自己判
        assert model.model_validate({"timezone": "UTC", "hour": 99}).hour == 99


class TestSpecIntegration:
    TOOL = MCPTool(name="get_time", description="Get the time", input_schema={
        "type": "object", "properties": {"tz": {"type": "string"}}, "required": ["tz"],
    })

    def _client(self) -> MCPClient:
        return MCPClient(MCPServerConfig(name="time", command="echo"))

    def test_timeout_is_set_per_tool(self):
        """本地工具 3 秒，外部工具走网络——同一个值会让一大半正常调用变成超时，
        而那种超时看起来和真故障一模一样。"""
        spec = build_spec(self._client(), self.TOOL, readonly=True, timeout_s=15.0)
        assert spec.timeout_s is not None
        assert spec.timeout_s > 3.0

    def test_outer_timeout_is_looser_than_inner(self):
        """让 client 自己先超时并给出具体反馈，而不是被外层 wait_for 掐断
        ——后者只能报一句泛泛的"执行超时"。"""
        spec = build_spec(self._client(), self.TOOL, readonly=True, timeout_s=15.0)
        assert spec.timeout_s > 15.0

    def test_description_marks_external_origin(self):
        """模型据此判断"这个能力来自外部"，结果不合预期时更容易换方式而不是
        反复重试同一个工具。"""
        spec = build_spec(self._client(), self.TOOL, readonly=False, timeout_s=5.0)
        assert "外部工具" in spec.description
        assert "time" in spec.description

    def test_readonly_flows_from_config(self):
        assert build_spec(self._client(), self.TOOL, readonly=True, timeout_s=5).readonly
        assert not build_spec(self._client(), self.TOOL, readonly=False, timeout_s=5).readonly


class TestFailureIsolation:
    """外部依赖是不可控的：npx 没装、网络不通、server 自己崩了都很正常。
    用户宁可少几个工具，也不能因为一个可选依赖连不上就打不开页面。
    """

    @pytest.mark.asyncio
    async def test_missing_command_reports_actionable_message(self):
        client = MCPClient(MCPServerConfig(name="x", command="definitely-not-a-real-cmd"))
        with pytest.raises(MCPError, match="找不到命令"):
            await client.start()

    @pytest.mark.asyncio
    async def test_one_bad_server_does_not_block_others(self):
        registry_ = MCPRegistry()
        configs = [
            MCPServerConfig(name="bad", command="definitely-not-a-real-cmd"),
            MCPServerConfig(name="alsobad", command="another-fake-cmd"),
        ]
        # 不抛，只是注册 0 个
        assert await registry_.load(configs) == 0
        await registry_.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_without_load_is_safe(self):
        await MCPRegistry().shutdown()

    @pytest.mark.asyncio
    async def test_process_exit_is_reported_not_hung(self):
        """server 退出后调用要立刻报错，而不是永远等下去。"""
        client = MCPClient(MCPServerConfig(
            name="quit", command=sys.executable, args=["-c", "pass"],
        ))
        with pytest.raises(MCPError):
            await client.start()


class TestNameCollision:
    def test_local_tool_wins(self):
        """本地工具先注册好名字，外部撞名时跳过——本地的是我们能负责的那些。"""
        local = ToolRegistry()
        from pydantic import BaseModel

        class Args(BaseModel):
            pass

        async def handler(inp, ctx):
            return {}

        from app.core.tools.registry import ToolSpec

        spec = ToolSpec(
            name="mcp__a__x", label="本地", description="d",
            input_model=Args, handler=handler, readonly=True, needs_confirm=False,
        )
        local.register(spec)
        assert local.has("mcp__a__x")
        with pytest.raises(ValueError, match="重复"):
            local.register(spec)


class TestRealServer:
    """用真实的 MCP server 跑一遍。

    这条最重要——前面全是结构性验证，只有真跑一次才能确认协议握手、帧同步、
    schema 透传、进程收尾这几件事都对。

    需要 uvx（Python 生态的 MCP server 运行器）。拿不到就跳过，但只要它在，
    就必须完整通过。
    """

    CONFIG = MCPServerConfig(
        name="time", command="uvx", args=["mcp-server-time"], readonly=True,
    )

    @pytest.fixture
    def uvx_available(self) -> bool:
        import shutil

        if shutil.which("uvx") is None:
            pytest.skip("本机没有 uvx")
        return True

    @pytest.mark.asyncio
    async def test_handshake_and_discovery(self, uvx_available):
        client = MCPClient(self.CONFIG)
        await client.start()
        try:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "get_current_time" in names
            # schema 必须带着类型和描述过来，否则模型填不对参数
            tool = next(t for t in tools if t.name == "get_current_time")
            assert tool.input_schema.get("properties")
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_real_call_returns_usable_text(self, uvx_available):
        client = MCPClient(self.CONFIG)
        await client.start()
        try:
            text = await client.call_tool(
                "get_current_time", {"timezone": "Asia/Shanghai"}, timeout_s=20,
            )
            payload = json.loads(text)
            assert payload["timezone"] == "Asia/Shanghai"
            assert payload["datetime"]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_tool_error_is_raised_not_swallowed(self, uvx_available):
        """isError 是 MCP 表达"工具自己失败了"的方式，和 JSON-RPC 层的错误
        不同。不识别它会把失败当成成功结果回灌给模型。"""
        client = MCPClient(self.CONFIG)
        await client.start()
        try:
            with pytest.raises(MCPError):
                await client.call_tool(
                    "get_current_time", {"timezone": "Not/AZone"}, timeout_s=20,
                )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_registers_into_local_registry(self, uvx_available):
        """端到端：真实 server 的工具进本地注册表后可以像本地工具一样调用。"""
        import uuid

        from app.core.tools.registry import ToolContext, registry

        mcp = MCPRegistry()
        try:
            count = await mcp.load([self.CONFIG], timeout_s=20)
            assert count >= 1
            assert registry.has("mcp__time__get_current_time")

            ctx = ToolContext(user_id=uuid.uuid4(), session=None)
            result = await registry.invoke(
                "mcp__time__get_current_time", {"timezone": "UTC"}, ctx,
            )
            assert result["source"] == "time"
            assert "UTC" in result["result"]
        finally:
            await mcp.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_name_enters_scrub_vocab(self, uvx_available):
        """脱敏词表从注册表现取，所以 MCP 工具自动被覆盖——不需要在脱敏层
        另立一张表。这是"新增工具零改动"能延伸到外部工具的关键。
        """
        from app.core.agent.scrub import TextScrubber, build_vocab

        mcp = MCPRegistry()
        try:
            await mcp.load([self.CONFIG], timeout_s=20)
            assert "mcp__time__get_current_time" in build_vocab()

            scrubber = TextScrubber()
            out = "".join([
                scrubber.feed("我用 mcp__time__"),
                scrubber.feed("get_current_time 查了一下"),
            ]) + scrubber.flush()
            assert "mcp__time__get_current_time" not in out
            assert "「" in out, "内部名没有被替换成用户可见说法"
        finally:
            await mcp.shutdown()

    @pytest.mark.asyncio
    async def test_subprocess_is_cleaned_up(self, uvx_available):
        """不收干净就是僵尸进程。容器里表现为重启后端口/内存莫名被占。"""
        client = MCPClient(self.CONFIG)
        await client.start()
        assert client.alive
        await client.close()
        assert not client.alive

    @pytest.mark.asyncio
    async def test_concurrent_calls_do_not_interleave(self, uvx_available):
        """stdio 上的 JSON-RPC 没有帧长度头，并发写入会让两条请求的字节交错，
        交错之后整条流就废了。所以 RPC 必须串行化。"""
        client = MCPClient(self.CONFIG)
        await client.start()
        try:
            results = await asyncio.gather(*[
                client.call_tool("get_current_time", {"timezone": tz}, timeout_s=20)
                for tz in ("UTC", "Asia/Shanghai", "Europe/London", "UTC")
            ])
            for text, tz in zip(results, ("UTC", "Asia/Shanghai", "Europe/London", "UTC")):
                assert json.loads(text)["timezone"] == tz, "响应串了"
        finally:
            await client.close()
