"""把 MCP server 的工具桥接进本地注册表。

接进来之后，MCP 工具和本地工具**在系统里完全等价**：同一套 JSON Schema 交给
模型、同一套参数校验、同一套失败反馈、同一套流式脱敏、同一套降级策略。
Agent 循环一行都不用改。

## 三件事必须在这一层解决

**1. 名字冲突与来源可辨。**
外部 server 的工具名不受我们控制，两个 server 都叫 `search` 是完全可能的。
加 `mcp__<server>__` 前缀做命名空间。前缀同时起到第二个作用：出问题时从工具名
一眼看出是哪个外部 server，不必去翻配置。

**2. label 是本地的产物，不是外部给的。**
MCP 协议没有"用户可见说法"这个概念。而这个项目里 label 是硬性要求——前端状态行、
失败反馈、流式脱敏全都只用 label。所以必须在这里生成一个：用 server 名 + 工具名
拼一个中文说法，宁可粗糙也不能没有，否则用户会看到
"正在 mcp__weather__get_forecast…"。

**3. Schema 不能直接用。**
注册表要 Pydantic 模型（它负责校验和生成 schema），而 MCP 给的是裸 JSON Schema。
不做完整的 schema → model 编译（那是个大工程，且外部 schema 什么都可能有），
而是包一个"透传模型"：校验交给 server 自己做，我们只保证结构合法。

这是个有意的取舍：本地工具的参数校验很严（Pydantic 逐字段），MCP 工具的校验松
（只查必填项）。因为外部 schema 的表达力我们控制不了——强行翻译会在遇到
oneOf / $ref / 自定义 format 时静默丢约束，那比明确地"只做基础校验"更危险。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from app.core.logger import logger
from app.core.mcp.client import MCPClient, MCPError, MCPServerConfig, MCPTool
from app.core.tools.registry import ToolContext, ToolSpec, registry

# 工具名前缀。双下划线分隔，与工具名本身的单下划线区分开——
# 拆分时才能可靠地还原出 server 名。
NAME_PREFIX = "mcp"
SEPARATOR = "__"

# MCP 工具调用的超时。比本地工具（3s）宽松得多：外部 server 往往要走网络，
# 3 秒会让一大半正常调用变成超时。但也不能太长——它在用户等回复的路径上。
MCP_TOOL_TIMEOUT_S = 15.0


def qualified_name(server: str, tool: str) -> str:
    return f"{NAME_PREFIX}{SEPARATOR}{server}{SEPARATOR}{tool}"


def split_name(qualified: str) -> tuple[str, str] | None:
    """从带前缀的名字还原 (server, tool)。不是 MCP 工具时返回 None。"""
    if not qualified.startswith(f"{NAME_PREFIX}{SEPARATOR}"):
        return None
    rest = qualified[len(NAME_PREFIX) + len(SEPARATOR):]
    server, sep, tool = rest.partition(SEPARATOR)
    if not sep or not server or not tool:
        return None
    return server, tool


# label 的长度上限，按显示宽度算（一个汉字约等于两个英文字符）。
#
# 60 是量出来的，不是拍的：真实 server 的描述首句普遍在 30–45 个英文字符
# （"Get current time in a specific timezone" 是 39），再加上 "（server）"
# 后缀的宽度。定 40 会让绝大多数描述放不下、全部退回工具名，那就等于白写了
# 读描述这段逻辑。
LABEL_MAX_WIDTH = 60


def _display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _humanize(name: str) -> str:
    """把工具名转成可读短语：get_current_time → get current time。

    这不是翻译，只是让它别看起来像个标识符。真正的中文化需要一张人工词表，
    而外部工具是任意的，词表注定跟不上——所以退而求其次：至少不让用户看到
    下划线和一眼就知道是内部名字的东西。
    """
    return name.replace("_", " ").replace("-", " ").strip()


def _make_label(server: str, tool: MCPTool) -> str:
    """生成用户可见的说法。

    MCP 协议没有这个字段，只能自己造。而这个项目里 label 是硬性要求——前端状态行、
    失败反馈、流式脱敏全都只用它，缺了用户就会看到
    "正在 mcp__weather__get_forecast…"。

    优先用 description 的第一句：它通常是 "Get current time in a specific
    timezone" 这样的短语，比工具名可读。放不下时退回工具名的可读形式。

    两种情况下 label 都会是英文——外部 server 基本只写英文描述。这是已知的
    不完美：中文化需要一张人工词表，而外部工具是任意的，词表注定跟不上。
    至少它不再是带 mcp__ 前缀和下划线的内部标识符。
    """
    suffix = f"（{server}）"
    budget = LABEL_MAX_WIDTH - _display_width(suffix)

    text = (tool.description or "").strip().replace("\n", " ")
    if text:
        # 按中英文两种句号切第一句
        first = text.split(". ")[0].split("。")[0].strip().rstrip(".")
        if first and _display_width(first) <= budget:
            return f"{first}{suffix}"

    readable = _humanize(tool.name)
    if _display_width(readable) <= budget:
        return f"{readable}{suffix}"
    # 连工具名都放不下：截断并加省略号，仍然优于暴露完整内部名。
    return f"{readable[: max(1, budget - 1)]}…{suffix}"


def _passthrough_model(name: str, schema: dict) -> type[BaseModel]:
    """为 MCP 工具造一个透传参数模型。

    只做两件事：把 server 声明的 JSON Schema 原样交给模型（这是模型填对参数的
    唯一依据），以及检查必填项在不在。字段级校验交给 server——外部 schema 的
    表达力我们控制不了，强行翻译会在遇到 oneOf / $ref 时静默丢约束。
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    fields: dict[str, Any] = {}
    for key in (properties or {}):
        # 全部按 Any 收，必填的没有默认值、选填的默认 None。
        fields[key] = (Any, ...) if key in required else (Any, None)

    model = create_model(  # type: ignore[call-overload]
        f"MCPArgs_{name}",
        __config__=ConfigDict(
            # 允许 schema 里没声明的字段：有些 server 的 schema 不完整，
            # 拒绝未知字段会让本来能用的工具报参数错误。
            extra="allow",
        ),
        **fields,
    )

    # 覆盖 schema 生成：交给模型的必须是 server 的原始 schema，不是我们
    # 用 Any 拼出来的那个（那个没有类型、没有枚举值，模型填不对参数）。
    original = dict(schema) if isinstance(schema, dict) else {}
    original.setdefault("type", "object")

    def _schema(*_args, **_kwargs) -> dict:
        return original

    model.model_json_schema = _schema  # type: ignore[method-assign]
    return model


def build_spec(
    client: MCPClient, tool: MCPTool, *, readonly: bool, timeout_s: float,
) -> ToolSpec:
    server = client.config.name
    full_name = qualified_name(server, tool.name)

    async def handler(inp: BaseModel, ctx: ToolContext | None) -> dict:  # noqa: ARG001
        # ctx 用不上：MCP server 在我们的进程外，没有 session 可传，租户隔离
        # 也无从谈起——这一点在 README 的安全说明里必须写清楚。
        arguments = inp.model_dump(exclude_none=True)
        try:
            text = await client.call_tool(tool.name, arguments, timeout_s=timeout_s)
        except MCPError as exc:
            # 转成 ToolValidationError 之外的普通异常，交给 loop 的兜底：
            # 它会转成文本反馈回灌给模型，而不是把 500 抛给用户。
            raise RuntimeError(str(exc)) from exc
        # 统一包一层 dict：loop 会 json.dumps 结果，裸字符串会被加上引号，
        # 模型读到 "\"晴，26度\"" 这种带转义的内容。
        return {"result": text, "source": server}

    return ToolSpec(
        name=full_name,
        label=_make_label(server, tool),
        # 描述前面缀上来源。模型据此判断"这个能力来自外部"，在结果不合预期时
        # 更容易选择换一种方式，而不是反复重试同一个工具。
        description=(
            f"[外部工具 · {server}] {tool.description}"
            if tool.description else f"[外部工具 · {server}] {tool.name}"
        ),
        input_model=_passthrough_model(full_name, tool.input_schema),
        handler=handler,
        readonly=readonly,
        needs_confirm=False,
        # 超时写进 spec，Agent 循环据此为这个工具单独设闸。不写的话它会套用
        # 本地工具的 3 秒，外部工具几乎必然超时。
        #
        # 比 handler 内部的超时略宽一点：让 client 自己先超时并给出"某某超时"
        # 这种具体反馈，而不是被外层 wait_for 直接掐断——后者只能报一句
        # 泛泛的"执行超时"。
        timeout_s=timeout_s + 2.0,
    )


class MCPRegistry:
    """管理所有 MCP server 的生命周期。

    进程内单例，随 lifespan 启停。
    """

    def __init__(self) -> None:
        self._clients: list[MCPClient] = []

    @property
    def clients(self) -> list[MCPClient]:
        return list(self._clients)

    async def load(
        self, configs: list[MCPServerConfig], *, timeout_s: float = MCP_TOOL_TIMEOUT_S,
    ) -> int:
        """连接所有 server 并把它们的工具注册进本地注册表。

        返回注册成功的工具数。**任何单个 server 的失败都不影响其他 server，
        也不影响服务启动**——外部依赖是不可控的，npx 没装、网络不通、server 崩了
        都很正常，用户宁可少几个工具也不能因此打不开页面。
        """
        registered = 0
        for config in configs:
            try:
                registered += await self._load_one(config, timeout_s)
            except Exception as exc:  # noqa: BLE001 - 外部进程什么都可能抛
                logger.warning(
                    f"MCP server「{config.name}」加载失败，已跳过：{exc}",
                )
        if registered:
            logger.info(f"MCP 工具共注册 {registered} 个")
        return registered

    async def _load_one(self, config: MCPServerConfig, timeout_s: float) -> int:
        client = MCPClient(config)
        await client.start()
        # 记进列表要在 start 之后、list_tools 之前：握手成功就说明有子进程要收，
        # 后面哪一步失败都得走 shutdown 把它关掉，否则留下僵尸进程。
        self._clients.append(client)

        tools = await client.list_tools()
        count = 0
        for tool in tools:
            spec = build_spec(
                client, tool, readonly=config.readonly, timeout_s=timeout_s,
            )
            if registry.has(spec.name):
                logger.warning(f"MCP 工具名冲突，已跳过：{spec.name}")
                continue
            registry.register(spec)
            count += 1
        logger.info(
            f"MCP server「{config.name}」注册 {count} 个工具："
            f"{[t.name for t in tools]}",
        )
        return count

    async def shutdown(self) -> None:
        clients, self._clients = self._clients, []
        # 并发关闭：每个都要等 terminate，串行的话 N 个 server 就是 N × 5 秒，
        # 容器停机宽限期撑不住。
        await asyncio.gather(
            *(c.close() for c in clients), return_exceptions=True,
        )


mcp_registry = MCPRegistry()
