"""降级阶梯 L1–L3。

L0 正常 → L1 同模型退避重试 → L2 切备用模型 → L3 缩减工具集重试。
L4（完全绕过 LLM）由 Agent 层在捕获到本模块最终抛出的异常后触发。
"""

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.llm.client import ChatChunk, ChatRequest, LLMError, LLMFatalError, LLMProvider
from app.core.logger import logger

# L3 保留的核心工具：覆盖"算热量、查进度、记训练"三条主干。
# 必须是真实注册的工具名，否则砍完一个都不剩（有测试盯着）。
CORE_TOOL_NAMES: list[str] = [
    "calc_energy_baseline",
    "calc_macros",
    "log_workout",
    "query_body_trend",
    "query_workout_history",
]


@dataclass
class FallbackProvider:
    primary: LLMProvider
    fallback: LLMProvider | None = None
    retries: int = 2
    backoff_s: tuple[float, ...] = field(default_factory=lambda: (0.2, 0.8))

    async def _attempt(
        self, provider: LLMProvider, req: ChatRequest, level: int,
    ) -> AsyncIterator[tuple[ChatChunk, int]]:
        produced = False
        async for chunk in provider.stream(req):
            if chunk.text_delta or chunk.tool_calls:
                produced = True
            yield chunk, level

        if not produced:
            # 推理模型把 max_tokens 全烧在思考上时，返回的是 HTTP 200 + 空正文
            # （finish_reason=length），不是报错。当成"这轮说完了"的后果是用户
            # 看到工具跑完了、却没有任何结论——比报错更难查。
            #
            # 这里抛出去是安全的：既然一个字、一个工具调用都没产出，下游还没
            # 拿到任何内容，重试不会重复输出。
            raise LLMError("模型返回空输出（多半是思考预算耗尽），本次尝试作废")

    def _shrink(self, req: ChatRequest) -> ChatRequest | None:
        """砍到核心工具集。已经足够精简时返回 None，避免白跑一次。"""
        if not req.tools:
            return None
        kept = [t for t in req.tools if t.get("function", {}).get("name") in CORE_TOOL_NAMES]
        if len(kept) >= len(req.tools):
            return None
        return ChatRequest(
            messages=req.messages, tools=kept, model=req.model, max_tokens=req.max_tokens,
        )

    async def stream_with_fallback(
        self, req: ChatRequest,
    ) -> AsyncIterator[tuple[ChatChunk, int]]:
        last_error: Exception | None = None

        # L0 + L1：主模型，首次 + retries 次退避重试
        for attempt in range(self.retries + 1):
            level = 0 if attempt == 0 else 1
            try:
                async for item in self._attempt(self.primary, req, level):
                    yield item
                return
            except LLMFatalError:
                raise  # 不可重试，立刻上抛，不浪费重试预算
            except LLMError as exc:
                last_error = exc
                logger.warning(f"L{level} 第 {attempt + 1} 次尝试失败：{exc}")
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])

        # L2：切备用模型
        if self.fallback is not None:
            try:
                logger.warning(f"L2 切换到备用模型 {self.fallback.model}")
                async for item in self._attempt(self.fallback, req, 2):
                    yield item
                return
            except LLMFatalError:
                raise
            except LLMError as exc:
                last_error = exc
                logger.warning(f"L2 备用模型也失败：{exc}")

        # L3：缩减工具集，用主模型再试一次。
        # 失败不一定是模型挂了——十几个工具 schema 一起喂进去，小模型会
        # 选错工具或反复横跳。砍到核心几个常常就通了，比直接跳到兜底温和。
        shrunk = self._shrink(req)
        if shrunk is not None:
            try:
                logger.warning(f"L3 工具集从 {len(req.tools)} 个缩减到 {len(shrunk.tools)} 个后重试")
                async for item in self._attempt(self.primary, shrunk, 3):
                    yield item
                return
            except LLMFatalError:
                raise
            except LLMError as exc:
                last_error = exc
                logger.warning(f"L3 仍失败：{exc}")

        raise last_error or LLMError("所有降级层级均失败")
