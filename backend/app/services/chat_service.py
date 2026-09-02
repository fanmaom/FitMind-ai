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
from app.core.memory.query import build_context_query
from app.core.tools.registry import ToolContext, registry
from app.models.message import Message
from app.services import interrupt as interrupt_registry

FAST_PATH_CARD_TYPE = {
    "log_workout": "workout_logged",
    "log_body_metric": "metric_logged",
}

# 从库里搬多少条消息进内存的硬上限。
#
# 压缩逻辑最终只保留最近 agent_history_window 轮，更早的按 token 预算取舍，
# 所以取回全部消息是纯浪费——搬运和逐条 token 估算的成本随会话长度线性增长，
# 用得上的永远只是末尾那一小段。
#
# 留出远高于窗口的余量（窗口 6 轮 → 这里 60 条），因为两者的单位不同：
# 窗口按"轮"算，一轮可能是 user + assistant 两条，工具密集的回合更多；
# 而且过滤掉空文本消息之后条数还会减少。给足余量，让取舍继续由 token
# 预算决定，而不是被这道闸提前截断。
HISTORY_FETCH_LIMIT = 60


async def load_history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    exclude_id: uuid.UUID | None = None,
    limit: int = HISTORY_FETCH_LIMIT,
) -> list[dict]:
    """取历史对话，最多 limit 条（取最新的）。

    interrupted 与 done 同等纳入：用户已经看见了那半句话，模型也必须看见，
    否则下一轮它会跟屏幕上还挂着的半句自相矛盾。streaming 状态仍排除——
    那是正在写、还没定稿的。

    exclude_id 用来剔除本轮那条 user 消息。它在调用这里之前就已经以
    status="done" 落库了（先落库再推流是有意的），所以会被上面的筛选条件
    正常取到——而 build_context 末尾还会再拼一次当前消息，结果是同一句话
    在 prompt 里出现两遍。这个 bug 不报错、不影响功能，只表现为白烧 token
    和模型偶尔把用户的话当成说了两遍来回应。

    limit 是数据库层的硬上限，与 compress_history 的 token 预算是两道
    不同的闸：这里管"从库里搬多少行进内存"，那里管"往 prompt 里放多少"。
    没有这一道时，一个聊了几百轮的会话每轮都要把全部消息取出来、再逐条
    估算 token，然后绝大部分被压缩逻辑丢掉——搬运和估算的成本随会话长度
    线性增长，而真正用得上的永远只有末尾那一小段。
    """
    stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.status.in_(("done", "interrupted")),
    )
    if exclude_id is not None:
        stmt = stmt.where(Message.id != exclude_id)

    # 倒序取最新 limit 条，再翻回正序。正序 + LIMIT 会取到最旧的那几条，
    # 那是完全相反的结果：模型会看到开头几句、看不到刚刚说了什么。
    #
    # 排序键是 seq 而不是 created_at：后者在同一事务内的多条消息上完全相同
    # （now() 返回事务开始时刻），排序结果不确定——助理的回复可能排在用户的
    # 提问之前。详见 models/message.py。
    rows = (await session.scalars(
        stmt.order_by(Message.seq.desc()).limit(limit),
    )).all()
    return [
        {"role": r.role, "content": r.content.get("text", "")}
        for r in reversed(rows) if r.content.get("text")
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

    user_message = Message(
        conversation_id=conversation_id, user_id=user_id, role="user",
        content={"text": user_text}, status="done", client_message_id=client_message_id,
    )
    session.add(user_message)
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
    # 历史必须先加载：续问句（「那午饭呢？」）自身没有可检索的实体，
    # 召回要靠它拼上文才能命中。
    # 排除本轮那条 user 消息——它已经落库了，不排除会在 prompt 里出现两遍。
    #
    # 取多少条：窗口按"轮"算，一轮至少 user + assistant 两条，所以下限是
    # 窗口的两倍；同时不低于 HISTORY_FETCH_LIMIT，避免把窗口调小的人顺带
    # 失去按 token 预算保留更早对话的能力。把窗口调很大时这里跟着放大，
    # 否则这道闸会变成一个没人知道的隐性截断。
    fetch_limit = max(HISTORY_FETCH_LIMIT, settings.agent_history_window * 2)
    history = await load_history(
        session, conversation_id, exclude_id=user_message.id, limit=fetch_limit,
    )
    context_query = build_context_query(user_text, history)
    # 两路一起送进去：当前句 + 上文。两路分开而不是拼成一条——拼接会稀释短句里
    # 的关键词，实测反而丢召回（见 core/memory/query.py 的实测表格）。
    # 取最小距离等价于"命中任意一路即可"，所以严格不劣于原来的单路召回。
    queries = [user_text] if context_query is None else [user_text, context_query]
    recalled = await recall(session, user_id, queries, k=5)
    if context_query is not None:
        logger.info(f"续问句启用上文补充检索，共 {len(queries)} 路，召回 {len(recalled)} 条")
    messages, budget = build_context(
        profile=profile,
        facts=[memory.content for memory in recalled],
        history=history,
        user_text=user_text,
        today=today,
        keep_recent=settings.agent_history_window,
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
