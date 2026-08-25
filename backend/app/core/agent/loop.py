"""Agent 主循环：调模型 → 执行工具 → 结果回灌 → 收敛。"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.agent.rule_fallback import try_rule_fallback
from app.core.llm.client import ChatRequest, LLMError, LLMFatalError
from app.core.llm.with_fallback import FallbackProvider
from app.core.logger import logger
from app.core.tools.registry import ToolContext, ToolNotFoundError, ToolValidationError, registry

L5_MESSAGE = (
    "模型服务暂时不可用，这个问题我没法用内置规则回答。"
    "你刚才的消息已经保存，稍后点重试即可，不用重新输入。"
)


@dataclass
class AgentEvent:
    type: str  # text / tool_start / tool_result / card / done / error
    data: dict = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        provider: FallbackProvider | None,
        tool_ctx: ToolContext | None,
        max_turns: int = 6,
        tool_timeout_s: float = 3.0,
    ) -> None:
        self.provider = provider
        self.tool_ctx = tool_ctx
        self.max_turns = max_turns
        self.tool_timeout_s = tool_timeout_s

    async def _execute_tool(self, name: str, args: dict) -> tuple[str, dict | None]:
        """执行工具，返回 (回灌给模型的文本, 给前端的卡片)。

        任何失败都转成文本反馈而不是上抛异常——参数错了让模型自己改，
        查无数据让模型换个说法回复。抛给用户一个 500 是最差的选择。
        """
        try:
            result = await asyncio.wait_for(
                registry.invoke(name, args, self.tool_ctx), timeout=self.tool_timeout_s,
            )
        except ToolValidationError as exc:
            return str(exc), None
        except ToolNotFoundError as exc:
            return f"工具不存在：{exc}", None
        except asyncio.TimeoutError:
            return f"工具 {name} 执行超时（{self.tool_timeout_s}s），可以换一种方式或稍后重试。", None
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"工具 {name} 执行异常")
            return f"工具 {name} 执行失败：{exc}", None

        card = None
        if isinstance(result, dict):
            card = result.pop("__card__", None)
        return json.dumps(result, ensure_ascii=False, default=str), card

    def _assistant_message(self, text: str, tool_calls: list) -> dict:
        return {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }

    async def run(
        self, *, messages: list[dict], tools: list[dict], profile: dict, user_text: str,
    ) -> AsyncIterator[AgentEvent]:
        working = list(messages)
        level = 0
        usage: dict | None = None
        tool_call_count = 0

        # provider 为 None 表示连构造都失败了（最常见是 LLM 压根没配置）。
        # 那是"LLM 不可用"最极端的形式，没有理由不走兜底——直接进 L4，
        # 而不是把构造异常抛出去变成 L5。
        if self.provider is None:
            logger.warning("LLM provider 不可用，直接进入 L4 规则兜底")
            async for ev in self._degrade(user_text, profile, tool_call_count):
                yield ev
            return

        try:
            for _turn in range(self.max_turns):
                text_parts: list[str] = []
                pending_calls = None

                async for chunk, lv in self.provider.stream_with_fallback(
                    ChatRequest(messages=working, tools=tools),
                ):
                    level = max(level, lv)
                    if chunk.text_delta:
                        text_parts.append(chunk.text_delta)
                        yield AgentEvent("text", {"text": chunk.text_delta})
                    if chunk.usage:
                        usage = chunk.usage
                    if chunk.tool_calls:
                        pending_calls = chunk.tool_calls

                if not pending_calls:
                    yield AgentEvent("done", {
                        "degradation_level": level, "usage": usage,
                        "tool_calls": tool_call_count, "truncated": False,
                    })
                    return

                working.append(self._assistant_message("".join(text_parts), pending_calls))

                for tc in pending_calls:
                    tool_call_count += 1
                    yield AgentEvent("tool_start", {"name": tc.name, "input": tc.arguments})
                    result_text, card = await self._execute_tool(tc.name, tc.arguments)
                    yield AgentEvent("tool_result", {"name": tc.name, "summary": result_text[:200]})
                    if card:
                        yield AgentEvent("card", card)
                    working.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "name": tc.name, "content": result_text,
                    })

            # 轮次用尽：截断而非无限循环
            logger.warning(f"Agent 循环达到上限 {self.max_turns} 轮，截断")
            yield AgentEvent("done", {
                "degradation_level": level, "usage": usage,
                "tool_calls": tool_call_count, "truncated": True,
            })
            return

        except (LLMError, LLMFatalError) as exc:
            logger.warning(f"L1-L3 全部失败，转 L4 规则兜底：{exc}")

        async for ev in self._degrade(user_text, profile, tool_call_count):
            yield ev

    async def _degrade(
        self, user_text: str, profile: dict, tool_call_count: int,
    ) -> AsyncIterator[AgentEvent]:
        """L4 规则兜底 → 兜不住则 L5 诚实失败。"""
        answer = try_rule_fallback(user_text, profile)
        if answer.matched:
            yield AgentEvent("text", {"text": answer.text})
            if answer.card:
                yield AgentEvent("card", answer.card)
            yield AgentEvent("done", {
                "degradation_level": 4, "usage": None,
                "tool_calls": tool_call_count, "truncated": False,
            })
            return

        yield AgentEvent("error", {"degradation_level": 5, "message": L5_MESSAGE})
