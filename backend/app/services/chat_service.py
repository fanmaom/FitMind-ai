"""对话回合编排：落库 → 快路径 → Agent 循环 → 收尾。"""

import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import AsyncIterator

from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent.context import build_context
from app.core.agent.fast_path import try_fast_path
from app.core.agent.loop import AgentLoop
from app.core.config import get_settings
from app.core.database import async_session_maker, bind_rls_user
from app.core.llm.factory import build_provider
from app.core.llm.usage_recorder import record_usage
from app.core.llm.with_fallback import FallbackProvider
from app.core.logger import logger
from app.core.memory.profile import load_profile
from app.core.tools.registry import ToolContext, registry
from app.models.message import Message

FAST_PATH_CARD_TYPE = {
    "log_workout": "workout_logged",
    "log_body_metric": "metric_logged",
}


async def load_history(session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    rows = (await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.status == "done")
        .order_by(Message.created_at),
    )).all()
    return [
        {"role": r.role, "content": r.content.get("text", "")}
        for r in rows if r.content.get("text")
    ]


async def _find_duplicate(
    session: AsyncSession, user_id: uuid.UUID, client_message_id: str,
) -> Message | None:
    return await session.scalar(
        select(Message).where(
            Message.user_id == user_id,
            Message.client_message_id == client_message_id,
        ),
    )


async def run_turn(
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_text: str,
    client_message_id: str | None = None,
) -> AsyncIterator[dict]:
    """跑完一个回合，逐个 yield SSE 事件 {"event": str, "data": dict}。

    本函数自己持有 session，不复用请求的。

    这一点是必须的：Agent 循环跑在独立 task 上，而请求的 session 归请求作用域
    所有——SSE 一结束，get_db 就会把它关掉，此时后台任务还在用，两个协程同时
    操作同一条 asyncpg 连接会炸 "another operation is in progress"。
    循环脱离了请求的 task 却没脱离请求的 session，等于只做了一半。
    """
    async with _own_session(user_id) as session:
        async for event in _run_turn_inner(
            session, user_id, conversation_id, user_text, client_message_id,
        ):
            yield event


@asynccontextmanager
async def _own_session(user_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    session = async_session_maker()
    bind_rls_user(session, user_id)
    try:
        yield session
    finally:
        await session.close()


async def _run_turn_inner(
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_text: str,
    client_message_id: str | None,
) -> AsyncIterator[dict]:
    settings = get_settings()
    today = date.today().isoformat()
    started = time.monotonic()

    # 幂等：同一条消息重复提交只处理一次
    if client_message_id:
        existing = await _find_duplicate(session, user_id, client_message_id)
        if existing:
            yield {"event": "message_done",
                   "data": {"messageId": str(existing.id), "deduplicated": True}}
            return

    session.add(Message(
        conversation_id=conversation_id, user_id=user_id, role="user",
        content={"text": user_text}, status="done", client_message_id=client_message_id,
    ))
    # 助理消息先落库再推流：客户端断连时服务端跑完仍能落盘，
    # 重连是"取回已生成的"而不是"重新生成"
    assistant = Message(
        conversation_id=conversation_id, user_id=user_id, role="assistant",
        content={"text": "", "cards": []}, status="streaming",
    )
    session.add(assistant)
    await session.commit()

    yield {"event": "message_start", "data": {"messageId": str(assistant.id)}}

    tool_ctx = ToolContext(user_id=user_id, session=session)
    texts: list[str] = []
    cards: list[dict] = []

    async def finish(level: int, usage: dict | None, tool_calls: int, extra: dict) -> dict:
        assistant.content = {"text": "".join(texts), "cards": cards}
        assistant.status = "done"
        await record_usage(
            session=session, user_id=user_id, conversation_id=conversation_id,
            model=settings.llm_model or "rule-fallback", usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_calls=tool_calls, degradation_level=level,
        )
        await session.commit()
        return {"event": "message_done",
                "data": {"messageId": str(assistant.id), "degradedTo": level, **extra}}

    # 快路径：命中就不碰模型，全程几十毫秒
    hit = try_fast_path(user_text, today)
    if hit is not None:
        result = await registry.invoke(hit.tool_name, hit.arguments, tool_ctx)
        card = {"type": FAST_PATH_CARD_TYPE[hit.tool_name],
                "payload": {**hit.arguments, **result}}
        text = "这条记录已经存过了，没有重复写入。" if result.get("deduplicated") else "已记录。"
        texts.append(text)
        cards.append(card)
        yield {"event": "text_delta", "data": {"text": text}}
        yield {"event": "card", "data": card}
        yield await finish(0, None, 1, {"fastPath": True})
        return

    # 正常路径
    profile = await load_profile(session, user_id)
    history = await load_history(session, conversation_id)
    messages, budget = build_context(
        profile=profile, facts=[], history=history, user_text=user_text, today=today,
    )
    logger.info(
        f"上下文预算 total={budget.total} system={budget.system} "
        f"profile={budget.profile} history={budget.history}",
    )

    # 构造失败（最常见是 LLM 未配置）不上抛，传 None 让循环走 L4。
    # 降级决策统一由 Agent 循环负责，这里只管把情况传进去。
    try:
        provider = FallbackProvider(
            primary=build_provider(),
            fallback=(
                build_provider(settings.llm_fallback_model)
                if settings.llm_fallback_model else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"LLM provider 构造失败，本轮走降级：{exc}")
        provider = None
    loop = AgentLoop(
        provider, tool_ctx,
        max_turns=settings.agent_max_turns, tool_timeout_s=settings.agent_tool_timeout_s,
    )

    level, usage, tool_calls = 0, None, 0
    async for ev in loop.run(
        messages=messages, tools=registry.to_json_schemas(),
        profile=profile, user_text=user_text,
    ):
        if ev.type == "text":
            texts.append(ev.data["text"])
            yield {"event": "text_delta", "data": ev.data}
        elif ev.type == "tool_start":
            yield {"event": "tool_start", "data": ev.data}
        elif ev.type == "tool_result":
            tool_calls += 1
            yield {"event": "tool_result", "data": ev.data}
        elif ev.type == "card":
            cards.append(ev.data)
            yield {"event": "card", "data": ev.data}
        elif ev.type == "done":
            level, usage = ev.data["degradation_level"], ev.data.get("usage")
        elif ev.type == "error":
            level = ev.data["degradation_level"]
            texts.append(ev.data["message"])
            yield {"event": "error", "data": ev.data}

    yield await finish(level, usage, tool_calls, {})
