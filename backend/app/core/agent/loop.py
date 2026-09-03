"""Agent 主循环：调模型 → 执行工具 → 结果回灌 → 收敛。"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.agent.rule_fallback import try_rule_fallback
from app.core.llm.client import DEFAULT_MAX_TOKENS, ChatRequest, LLMError, LLMFatalError
from app.core.llm.with_fallback import FallbackProvider
from app.core.logger import logger
from app.core.tools.registry import ToolContext, ToolNotFoundError, ToolValidationError, registry

L5_MESSAGE = (
    "模型服务暂时不可用，这个问题我没法用内置规则回答。"
    "你刚才的消息已经保存，稍后点重试即可，不用重新输入。"
)

# 失败反馈会原样进模型上下文，而模型很容易把它整句转述给用户。所以反馈里
# 一律用工具的用户可见说法，并且明写"别转述"——用户不需要知道哪个工具炸了，
# 只需要知道这一步没算出来。
FEEDBACK_PREFIX = "[系统反馈，不要转述给用户，也不要提工具名]"


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
        max_output_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.provider = provider
        self.tool_ctx = tool_ctx
        self.max_turns = max_turns
        self.tool_timeout_s = tool_timeout_s
        self.max_output_tokens = max_output_tokens

    async def _recover_session(self, reason: str) -> bool:
        """把被中途取消的事务回滚掉，让 session 能继续用。

        asyncio.wait_for 超时会取消协程，而被取消的协程可能正卡在一条 SQL 上。
        asyncpg 连接被留在"事务已开始、语句未完成"的状态，SQLAlchemy 随后对
        **同一个 session** 的任何操作都抛 PendingRollbackError：

            Can't reconnect until invalid transaction is rolled back.

        后果远超"这一个工具没算出来"：本轮剩下的工具全部失败，连收尾时把助理
        消息落库的那次 commit 也失败——用户的整个回合凭空消失。而超时在
        _execute_tool 里被转成一句温和的文本反馈，把这个严重故障完全掩盖了。

        返回是否成功恢复。回滚本身也可能失败（连接真的断了），那种情况下
        session 已经不可用，只能把实情反馈给模型。
        """
        if self.tool_ctx is None:
            return True
        try:
            await self.tool_ctx.session.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{reason} 后回滚失败，session 已不可用：{exc}")
            return False
        logger.warning(f"{reason} 后已回滚事务，session 恢复可用")
        return True

    async def _execute_tool(self, name: str, args: dict) -> tuple[str, dict | None]:
        """执行工具，返回 (回灌给模型的文本, 给前端的卡片)。

        任何失败都转成文本反馈而不是上抛异常——参数错了让模型自己改，
        查无数据让模型换个说法回复。抛给用户一个 500 是最差的选择。
        """
        label = registry.label_of(name)
        try:
            result = await asyncio.wait_for(
                registry.invoke(name, args, self.tool_ctx), timeout=self.tool_timeout_s,
            )
        except ToolValidationError as exc:
            # 字段名必须留着，模型要靠它改参数；脱敏层拦在输出侧。
            # 参数校验发生在执行之前，没碰数据库，不需要回滚。
            return f"{FEEDBACK_PREFIX} {exc}", None
        except ToolNotFoundError as exc:
            # 这里反过来：模型编了个工具名，不点出来它改不过来。
            return f"{FEEDBACK_PREFIX} 工具不存在：{exc}", None
        except asyncio.TimeoutError:
            recovered = await self._recover_session(f"工具 {name} 超时")
            if not recovered:
                return (
                    f"{FEEDBACK_PREFIX} “{label}”执行超时，且数据连接已不可用。"
                    f"用自然语言告诉用户这一步没完成、稍后重试，不要再调用任何工具。",
                    None,
                )
            return (
                f"{FEEDBACK_PREFIX} “{label}”执行超时（{self.tool_timeout_s}s）。"
                f"用自然语言告诉用户这一步没算出来，换一种方式或稍后重试。", None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"工具 {name} 执行异常")
            # 工具执行到一半抛异常同样会留下未回滚的事务。数据库异常
            # （唯一约束冲突、死锁）都走这条路，不回滚后续照样连锁失败。
            await self._recover_session(f"工具 {name} 异常")
            return (
                f"{FEEDBACK_PREFIX} “{label}”执行失败：{exc}。"
                f"用自然语言告诉用户这一步没成，不要暴露报错原文。", None
            )

        card = None
        if isinstance(result, dict):
            card = result.pop("__card__", None)
        # 成功也要记一行。
        #
        # 原来只有失败才写日志，于是"模型压根没调这个工具"和"调了但没生效"在
        # 日志里长得一模一样——都是什么都没有。排查线上那次"说了记下了但档案还是
        # 空的"时，我只能靠翻数据库反推，因为日志里看不出模型到底调了什么。
        #
        # 只记工具名和结果里的关键标志位，不记完整入参：入参含用户的身体数据，
        # 日志不是存这些东西的地方。
        flags = ""
        if isinstance(result, dict):
            marks = [
                f"{key}={result[key]}"
                for key in ("written", "needs_confirmation", "deduplicated", "found")
                if key in result
            ]
            flags = f" {' '.join(marks)}" if marks else ""
        logger.info(f"工具执行 {name}{flags}")
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
                hit_output_limit = False

                async for chunk, lv in self.provider.stream_with_fallback(
                    ChatRequest(
                        messages=working, tools=tools, max_tokens=self.max_output_tokens,
                    ),
                ):
                    level = max(level, lv)
                    if chunk.text_delta:
                        text_parts.append(chunk.text_delta)
                        yield AgentEvent("text", {"text": chunk.text_delta})
                    if chunk.usage:
                        usage = chunk.usage
                    if chunk.tool_calls:
                        pending_calls = chunk.tool_calls
                    if chunk.finish_reason == "length" and text_parts:
                        # 说了一半被输出上限截断。空输出由降级层当失败重试，这里
                        # 是"有正文但没说完"——重试会重复已经推给用户的字，所以
                        # 只能如实告诉用户这句话没说完。
                        hit_output_limit = True
                        logger.warning(
                            f"本轮输出被 max_tokens={self.max_output_tokens} 截断，"
                            f"已产出 {len(''.join(text_parts))} 字",
                        )

                if not pending_calls:
                    yield AgentEvent("done", {
                        "degradation_level": level, "usage": usage,
                        "tool_calls": tool_call_count,
                        # 两种截断分开报：一种是"这句话没说完"（输出上限），
                        # 一种是"这件事没做完"（轮次用尽）。给用户的说法不同，
                        # 该采取的动作也不同。
                        "truncated": False,
                        "output_truncated": hit_output_limit,
                    })
                    return

                working.append(self._assistant_message("".join(text_parts), pending_calls))

                for tc in pending_calls:
                    tool_call_count += 1
                    label = registry.label_of(tc.name)
                    # label 是给前端状态行用的；name/input 留给日志，不出网关。
                    yield AgentEvent("tool_start", {
                        "name": tc.name, "label": label, "input": tc.arguments,
                    })
                    result_text, card = await self._execute_tool(tc.name, tc.arguments)
                    yield AgentEvent("tool_result", {
                        "name": tc.name, "label": label, "summary": result_text[:200],
                    })
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
                "output_truncated": False,
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
                "output_truncated": False,
            })
            return

        yield AgentEvent("error", {"degradation_level": 5, "message": L5_MESSAGE})
