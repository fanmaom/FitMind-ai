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
from app.core.agent.scrub import TextScrubber
from app.core.config import get_settings
from app.core.database import async_session_maker, bind_rls_user
from app.core.llm.factory import build_provider
from app.core.llm.usage_recorder import record_usage
from app.core.llm.with_fallback import FallbackProvider
from app.core.jobs.queue import enqueue
from app.core.logger import logger
from app.core.memory.facts import recall
from app.core.memory.profile import load_profile
from app.core.tools.registry import ToolContext, registry
from app.models.message import Message
from app.services import interrupt as interrupt_registry

FAST_PATH_CARD_TYPE = {
    "log_workout": "workout_logged",
    "log_body_metric": "metric_logged",
}


async def load_history(session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    """取历史对话。

    interrupted 与 done 同等纳入：用户已经看见了那半句话，模型也必须看见，
    否则下一轮它会跟屏幕上还挂着的半句自相矛盾。streaming 状态仍排除——
    那是正在写、还没定稿的。
    """
    rows = (await session.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status.in_(("done", "interrupted")),
        )
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
    # 开局先清一次旗。中断请求可能在上一个回合**已经结束后**才到达，那面旗子
    # 会留在登记表里，把这个回合一启动就当场杀掉。这是那种「上次点了停止，
    # 之后第一条消息永远没反应」的诡异 bug。
    interrupt_registry.clear(conversation_id)
    try:
        async with _own_session(user_id) as session:
            async for event in _run_turn_inner(
                session, user_id, conversation_id, user_text, client_message_id,
            ):
                yield event
    finally:
        interrupt_registry.clear(conversation_id)


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

    async def finish(
        level: int, usage: dict | None, tool_calls: int, extra: dict,
        *, status: str = "done",
    ) -> dict:
        assistant.content = {"text": "".join(texts), "cards": cards}
        assistant.status = status
        await record_usage(
            session=session, user_id=user_id, conversation_id=conversation_id,
            model=settings.llm_model or "rule-fallback", usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_calls=tool_calls, degradation_level=level,
        )
        await session.commit()
        # 前端要能区分「停止了」和「说完了」，否则渲染不出角标、也分不清该不该重试
        event = "interrupted" if status == "interrupted" else "message_done"
        return {"event": event,
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
    recalled = await recall(session, user_id, user_text, k=5)
    history = await load_history(session, conversation_id)
    messages, budget = build_context(
        profile=profile,
        facts=[memory.content for memory in recalled],
        history=history,
        user_text=user_text,
        today=today,
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
        max_output_tokens=settings.llm_max_tokens,
    )

    level, usage, tool_calls = 0, None, 0
    interrupted = False
    # 脱敏放在这一层，而不是 loop 里：这里是文本同时"发给用户"和"落库"的唯一
    # 出口。落库的必须也是脱敏后的——重连要取回它，下一轮还要当历史喂回模型，
    # 只擦流不擦库等于把泄漏留在库里慢慢发酵。
    scrubber = TextScrubber()
    events = loop.run(
        messages=messages, tools=registry.to_json_schemas(),
        profile=profile, user_text=user_text,
    )
    try:
        async for ev in events:
            # 每个事件前查一次旗。粒度到单个 token，且 loop.py 零改动——
            # 中断是"不再消费"，不是"通知循环自己停"。
            if interrupt_registry.is_requested(conversation_id):
                interrupted = True
                logger.info(f"回合被用户中断 conversation={conversation_id}")
                break
            if ev.type == "text":
                safe = scrubber.feed(ev.data["text"])
                if safe:
                    texts.append(safe)
                    yield {"event": "text_delta", "data": {"text": safe}}
            elif ev.type == "tool_start":
                # 只发 label。name 会变成"正在调用 plan_meals…"，input 里是
                # 一串内部字段名——两样都没有理由送到浏览器。
                yield {"event": "tool_start", "data": {"label": ev.data["label"]}}
            elif ev.type == "tool_result":
                tool_calls += 1
                yield {"event": "tool_result", "data": {"label": ev.data["label"]}}
            elif ev.type == "card":
                cards.append(ev.data)
                yield {"event": "card", "data": ev.data}
            elif ev.type == "done":
                level, usage = ev.data["degradation_level"], ev.data.get("usage")
            elif ev.type == "error":
                level = ev.data["degradation_level"]
                # 先把扣在脱敏缓冲里的尾巴发出去，再接降级文案，否则顺序会颠倒
                held = scrubber.flush()
                if held:
                    texts.append(held)
                    yield {"event": "text_delta", "data": {"text": held}}
                texts.append(ev.data["message"])
                yield {"event": "error", "data": ev.data}
    finally:
        # `async for` + `break` 不会关闭异步生成器，Python 只在 GC 时才收。
        # 不显式关，底层那条 httpx 流就一直悬着，连接池慢慢干涸——这种泄漏
        # 平时完全看不出来，只在跑久了之后表现为"偶尔卡住"。
        await events.aclose()

    # 脱敏靠"扣住结尾等边界"实现，最后一段还在缓冲里。不 flush 就是吞掉一段话；
    # 中断路径同样要 flush——用户已经看见的半句必须和落库的一致。
    tail = scrubber.flush()
    if tail:
        texts.append(tail)
        yield {"event": "text_delta", "data": {"text": tail}}

    if interrupted:
        # 已经执行完的工具写入不回滚（记进去的训练不该凭空消失），只停后续步骤。
        # usage 通常拿不到：网关只在流末尾报 token 数，而中断的定义就是到不了
        # 那里。仍然记一行——请求确实发生过，延迟和降级档是真实的。
        yield await finish(level, usage, tool_calls, {"interrupted": True},
                           status="interrupted")
        # 刻意不投递抽取任务：从半句话里抽长期事实会污染 L2 记忆库，抽待办
        # 更糟——半截建议会变成一条用户根本没读完的待跟进事项。
        return

    yield await finish(level, usage, tool_calls, {})

    # 用户可见的回复已经完成；下面是重要但不紧急的异步抽取投递。
    #
    # 两个任务分开投，不合并成一个：事实抽取的判据是「用户明确说过的」，待办
    # 抽取的判据恰好相反——只要助理提出的。塞进同一个 prompt 两边会互相污染。
    # 分开之后 worker 各自独立事务、独立重试，待办抽取炸掉不会连坐 L2 记忆。
    try:
        conversation_text = f"用户：{user_text}\n助理：{''.join(texts)}"
        source = {
            "user_id": str(user_id),
            "conversation_text": conversation_text,
            "source_message_id": str(assistant.id),
        }
        await enqueue(session, "extract_memory", source, user_id=user_id)
        await enqueue(
            session,
            "extract_actions",
            {**source, "source_conversation_id": str(conversation_id)},
            user_id=user_id,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.warning(f"投递异步抽取任务失败，不影响本轮回复：{exc}")
