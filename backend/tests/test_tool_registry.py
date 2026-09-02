"""工具注册机制。"""

import pytest
from pydantic import BaseModel, Field

from app.core.tools.registry import (
    ToolNotFoundError,
    ToolRegistry,
    ToolValidationError,
    tool,
)


class DemoInput(BaseModel):
    x: float = Field(gt=0, description="必须为正数")
    label: str = "default"


def _make_tool(name: str = "demo", description: str = "演示工具", **kw):
    kw.setdefault("label", "跑演示")

    @tool(name=name, description=description, **kw)
    async def fn(inp: DemoInput, ctx) -> dict:
        return {"doubled": inp.x * 2}

    return fn


class TestUserFacingLabel:
    """label 是这个工具的用户可见说法。内部 name 一旦出现在回复或状态行里，
    用户就会看到 plan_strength_cycle 这种东西——线上真实发生过。"""

    def test_label_is_on_the_spec(self):
        assert _make_tool(label="跑一下演示").__tool_spec__.label == "跑一下演示"

    def test_missing_label_is_a_type_error(self):
        """必填。给个默认值（比如退化成 name）等于把泄漏藏起来。"""
        with pytest.raises(TypeError):
            @tool(name="nolabel", description="没有 label")
            async def fn(inp: DemoInput, ctx) -> dict:
                return {}

    def test_blank_label_rejected(self):
        with pytest.raises(TypeError):
            _make_tool(label="   ")

    def test_label_of_falls_back_to_name_for_unknown_tool(self):
        """模型编了个工具名时，反馈里必须点出它编的那个名字，否则它改不过来。"""
        reg = ToolRegistry()
        assert reg.label_of("ghost_tool") == "ghost_tool"

    def test_every_registered_tool_has_a_chinese_label(self):
        from app.core.tools.registry import load_tools, registry

        load_tools()
        for spec in registry.all():
            assert spec.label.strip(), f"{spec.name} 没有 label"
            assert spec.label != spec.name, f"{spec.name} 的 label 就是内部名"
            assert "_" not in spec.label, f"{spec.name} 的 label 带下划线：{spec.label}"


class TestToolDecorator:
    def test_decorator_attaches_spec(self):
        fn = _make_tool(readonly=True)
        assert fn.__tool_spec__.name == "demo"
        assert fn.__tool_spec__.readonly is True
        assert fn.__tool_spec__.input_model is DemoInput

    def test_readonly_defaults_to_false(self):
        """默认按写操作对待——漏标 readonly 时宁可保守，不要让降级层误重试写入。"""
        assert _make_tool().__tool_spec__.readonly is False

    def test_requires_pydantic_input_annotation(self):
        with pytest.raises(TypeError):
            @tool(name="bad", label="坏工具", description="第一个参数没有模型注解")
            async def bad(inp, ctx) -> dict:  # type: ignore[no-untyped-def]
                return {}

    def test_rejects_non_basemodel_annotation(self):
        with pytest.raises(TypeError):
            @tool(name="bad2", label="坏工具", description="第一个参数注解不是 BaseModel")
            async def bad2(inp: dict, ctx) -> dict:
                return {}

    def test_rejects_zero_arg_function(self):
        with pytest.raises(TypeError):
            @tool(name="bad3", label="坏工具", description="没有参数")
            async def bad3() -> dict:
                return {}


class TestRegistry:
    def _registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(_make_tool(readonly=True).__tool_spec__)
        return reg

    def test_get_returns_spec(self):
        assert self._registry().get("demo").description == "演示工具"

    def test_get_unknown_raises(self):
        with pytest.raises(ToolNotFoundError):
            self._registry().get("nope")

    def test_duplicate_name_rejected(self):
        reg = self._registry()
        with pytest.raises(ValueError):
            reg.register(reg.get("demo"))

    def test_has_reports_membership(self):
        reg = self._registry()
        assert reg.has("demo") is True
        assert reg.has("nope") is False

    def test_json_schema_matches_openai_tool_format(self):
        s = self._registry().to_json_schemas()[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "demo"
        assert s["function"]["description"] == "演示工具"
        assert "x" in s["function"]["parameters"]["properties"]

    def test_schema_carries_field_descriptions(self):
        """字段描述是模型填对参数的主要依据，不能在转换中丢掉。"""
        s = self._registry().to_json_schemas()[0]
        assert s["function"]["parameters"]["properties"]["x"]["description"] == "必须为正数"


class TestDeterministicOrdering:
    """工具顺序必须稳定。顺序一变，prompt 前缀字节就变，缓存永远命中不了，
    而且没有任何报错——只体现为账单偏高。"""

    def _registry_with(self, names: list[str]) -> ToolRegistry:
        reg = ToolRegistry()
        for n in names:
            reg.register(_make_tool(name=n, description=f"{n} 工具").__tool_spec__)
        return reg

    def test_schemas_sorted_by_name(self):
        reg = self._registry_with(["zebra", "alpha", "middle"])
        assert [s["function"]["name"] for s in reg.to_json_schemas()] == \
            ["alpha", "middle", "zebra"]

    def test_insertion_order_does_not_matter(self):
        a = self._registry_with(["c", "a", "b"]).to_json_schemas()
        b = self._registry_with(["b", "c", "a"]).to_json_schemas()
        assert a == b

    def test_subset_filters_and_keeps_order(self):
        reg = self._registry_with(["c_tool", "a_tool", "b_tool"])
        names = [s["function"]["name"] for s in reg.subset(["b_tool", "a_tool"])]
        assert names == ["a_tool", "b_tool"]

    def test_subset_ignores_unknown_names(self):
        reg = self._registry_with(["a_tool"])
        assert [s["function"]["name"] for s in reg.subset(["a_tool", "ghost"])] == ["a_tool"]


class TestInvocation:
    def _registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(_make_tool().__tool_spec__)
        return reg

    @pytest.mark.asyncio
    async def test_valid_args_execute(self):
        assert await self._registry().invoke("demo", {"x": 3}, ctx=None) == {"doubled": 6.0}

    @pytest.mark.asyncio
    async def test_defaults_are_applied(self):
        reg = ToolRegistry()

        @tool(name="echo", label="回显", description="回显字段默认值")
        async def echo(inp: DemoInput, ctx) -> dict:
            return {"label": inp.label}

        reg.register(echo.__tool_spec__)
        assert await reg.invoke("echo", {"x": 1}, ctx=None) == {"label": "default"}

    @pytest.mark.asyncio
    async def test_invalid_args_raise_tool_validation_error(self):
        with pytest.raises(ToolValidationError):
            await self._registry().invoke("demo", {"x": -5}, ctx=None)

    @pytest.mark.asyncio
    async def test_error_message_is_feedable_to_model(self):
        """报错会被原样回灌给模型让它自行改参数，必须含字段名和收到的值。"""
        with pytest.raises(ToolValidationError) as exc:
            await self._registry().invoke("demo", {"x": -5}, ctx=None)
        msg = str(exc.value)
        assert "x" in msg
        assert "-5" in msg

    @pytest.mark.asyncio
    async def test_missing_required_field_reported(self):
        with pytest.raises(ToolValidationError) as exc:
            await self._registry().invoke("demo", {}, ctx=None)
        assert "x" in str(exc.value)

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self):
        with pytest.raises(ToolNotFoundError):
            await self._registry().invoke("ghost", {}, ctx=None)


class TestAutoLoad:
    def test_load_tools_is_idempotent(self):
        """lifespan 和测试都会调 load_tools，重复调用不能炸。"""
        from app.core.tools.registry import load_tools, registry

        load_tools()
        first = len(registry.all())
        load_tools()
        assert len(registry.all()) == first

    def test_load_tools_finds_files(self):
        from app.core.tools.registry import load_tools, registry

        load_tools()
        assert len(registry.all()) > 0, "自动扫描没有注册到任何工具"
