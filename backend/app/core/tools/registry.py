"""工具注册表。

一个工具一个文件，用 @tool 装饰；启动时扫描本包完成注册。
新增工具的成本 = 新增一个文件，别处零改动——这是"系统可扩展性"的落点。
"""

import importlib
import inspect
import pkgutil
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, get_type_hints

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import logger


class ToolNotFoundError(KeyError):
    """请求了未注册的工具。"""


class ToolValidationError(ValueError):
    """模型给的参数不合 schema。

    这不是异常情况而是一种反馈：错误文本会被原样回灌给模型，让它自行改参数重试。
    所以文本必须点名字段和收到的值，含糊的报错等于没有。
    """


@dataclass
class ToolContext:
    """工具执行上下文。

    所有数据访问都要经过它——租户边界在这一层强制，而不靠每个工具自觉。
    """

    user_id: uuid.UUID
    session: AsyncSession


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    readonly: bool
    needs_confirm: bool
    # 这个工具专属的超时。None 表示用 Agent 的默认值。
    #
    # 存在的理由：本地工具全是纯计算或一次查库，3 秒很宽松；而外部 MCP 工具在
    # 另一个进程里、往往还要走网络，同一个 3 秒会让一大半正常调用变成超时，
    # 而那种超时看起来和真故障一模一样。
    timeout_s: float | None = None


def tool(
    name: str,
    description: str,
    *,
    label: str,
    readonly: bool = False,
    needs_confirm: bool = False,
):
    """把一个协程函数标记为工具。

    签名必须是 `async def fn(inp: SomePydanticModel, ctx: ToolContext)`。

    label 是这个工具的**用户可见**说法，必填。name 是内部标识符，任何会被用户
    看到的地方（前端状态行、失败反馈、流式脱敏）都只能用 label——线上出现过
    "用 `plan_strength_cycle` 帮你排" 这种回复，就是因为当时没有 label 可用。
    动词短语，前端会拼成"正在{label}…"。

    readonly 默认 False：漏标时按写操作对待，宁可保守——降级层据此决定
    能否安全重试，误判成只读会导致重复写入。
    """

    def decorator(fn: Callable[..., Awaitable[Any]]):
        if not label.strip():
            raise TypeError(f"工具 {name} 必须提供非空 label（用户可见的说法）")

        params = list(inspect.signature(fn).parameters)
        if not params:
            raise TypeError(f"工具 {name} 必须接受 (inp, ctx) 两个参数，当前没有参数")

        hints = get_type_hints(fn)
        input_model = hints.get(params[0])
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise TypeError(
                f"工具 {name} 的第一个参数 `{params[0]}` 必须标注为 Pydantic BaseModel 子类，"
                f"当前是 {input_model!r}",
            )

        fn.__tool_spec__ = ToolSpec(  # type: ignore[attr-defined]
            name=name,
            label=label,
            description=description,
            input_model=input_model,
            handler=fn,
            readonly=readonly,
            needs_confirm=needs_confirm,
        )
        return fn

    return decorator


def _format_validation_error(exc: ValidationError) -> str:
    """把 Pydantic 报错压成一行，直接可回灌给模型。"""
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{field}: {err['msg']}（收到 {err.get('input')!r}）")
    return "参数校验失败 —— " + "；".join(parts)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具名重复：{spec.name}")
        self._specs[spec.name] = spec

    def has(self, name: str) -> bool:
        return name in self._specs

    def get(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise ToolNotFoundError(f"未注册的工具：{name}")
        return self._specs[name]

    def find(self, name: str) -> ToolSpec | None:
        """取 spec，未注册时返回 None 而不是抛。

        给"想读某个属性、读不到就用默认值"的场景用（比如按工具区分超时）。
        用 get 会要求调用方接异常，那太重了。
        """
        return self._specs.get(name)

    def label_of(self, name: str) -> str:
        """用户可见的说法。未注册时返回原名——那说明模型编了个工具名，
        这种情况下反馈文本里必须点出它编的那个名字，否则它改不过来。"""
        spec = self._specs.get(name)
        return spec.label if spec else name

    def all(self) -> list[ToolSpec]:
        # 按 name 排序。顺序不稳定会让 prompt 前缀字节每次不同，
        # 缓存永远命中不了，而且没有任何报错——只体现为账单偏高。
        return [self._specs[k] for k in sorted(self._specs)]

    def _to_schema(self, spec: ToolSpec) -> dict:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_model.model_json_schema(),
            },
        }

    def to_json_schemas(self) -> list[dict]:
        return [self._to_schema(s) for s in self.all()]

    def subset(self, names: list[str]) -> list[dict]:
        """降级 L3 用：只暴露核心工具。顺序同样按 name 排序。"""
        wanted = set(names)
        return [self._to_schema(s) for s in self.all() if s.name in wanted]

    async def invoke(self, name: str, raw_args: dict, ctx: ToolContext | None) -> Any:
        spec = self.get(name)
        try:
            parsed = spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            raise ToolValidationError(_format_validation_error(exc)) from exc
        return await spec.handler(parsed, ctx)


registry = ToolRegistry()


def load_tools() -> None:
    """扫描 app.core.tools 包，自动注册所有 @tool 函数。

    幂等：lifespan 和测试都会调用，重复调用不应报错。
    """
    import app.core.tools as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name == "registry":
            continue
        module = importlib.import_module(f"{pkg.__name__}.{mod_info.name}")
        for obj in vars(module).values():
            spec = getattr(obj, "__tool_spec__", None)
            if spec is not None and not registry.has(spec.name):
                registry.register(spec)

    logger.info(f"已注册 {len(registry.all())} 个工具：{[s.name for s in registry.all()]}")
