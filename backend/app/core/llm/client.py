"""LLM 抽象层：数据结构与协议。

换厂商只改 .env 的 LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL，上层无感。
这层抽象同时是降级机制的载体——主模型挂了切备用模型走的是同一套代码。
"""

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


class LLMError(Exception):
    """可重试的失败：超时、限流、5xx、输出格式错误。"""


class LLMFatalError(Exception):
    """不可重试的失败：鉴权错误、请求非法。

    刻意不继承 LLMError —— 降级层靠 `except LLMError` 决定是否重试，
    若做成子类，401 会被反复重试三次，浪费时间还刷错误日志。
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatChunk:
    text_delta: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: dict | None = None


# 输出预算。推理模型会先烧思考 token，**思考也算在 max_tokens 里**，给小了
# 不是截断而是正文全空（finish_reason=length，HTTP 200，没有任何报错）。
#
# 实测当前配置的 hy3，在"工具结果回灌"那一轮：
#     max_tokens=2048 → reasoning_tokens 2048/2048，正文 0 字
#     max_tokens=8192 → reasoning_tokens 2123，正文正常，继续调工具
#
# 所以下限由"思考长度"决定，而不是"回答多长"。这是个上限而非用量，
# 调大不会多花钱。可用 LLM_MAX_TOKENS 覆盖。
DEFAULT_MAX_TOKENS = 8192


@dataclass
class ChatRequest:
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    model: str = ""
    max_tokens: int = DEFAULT_MAX_TOKENS


class LLMProvider(Protocol):
    model: str

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        ...
