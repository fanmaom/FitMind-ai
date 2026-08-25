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


@dataclass
class ChatRequest:
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    model: str = ""
    max_tokens: int = 2048


class LLMProvider(Protocol):
    model: str

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        ...
