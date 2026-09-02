"""会话滚动摘要的生成与读取。

摘要在回合结束后由 worker 异步更新，主链路只读已有的那份——这条路径在用户等
回复上，不能加同步 LLM 调用。代价是摘要滞后一轮，但滞后的那轮恰好还在
keep_recent 窗口里原样保留着，不构成缺口。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.llm.client import ChatRequest, LLMError
from app.core.llm.factory import build_provider
from app.core.logger import logger
from app.models.conversation_summary import ConversationSummary
from app.models.message import Message

# 摘要自身的长度上限。摘要是为了省 token，自己膨胀到几千字就本末倒置了。
# 这个数字也进 prompt，但模型经常无视，所以后面还有一道硬截断。
MAX_SUMMARY_CHARS = 600

# 单次摘要最多读多少条新消息。一次并进太多会让摘要质量下降（模型要在一个
# 请求里消化过长的输入），而且这是增量流程，追不上就下一轮继续追。
MAX_MESSAGES_PER_RUN = 40

# 每条消息截取的字符数。助理的计划说明可能上千字，全量喂进去会把预算烧在
# 细节上——而细节本来就该由卡片和 L3 日志承载。
PER_MESSAGE_CHARS = 300

SUMMARY_PROMPT = """把下面的健身教练对话压缩成一段简洁的中文摘要，用于给助理\
提供长期上下文。

只保留后续对话真正需要的信息：
- 用户当前在执行的计划及其前提（体重、热量、周数这类关键数字要保留原值）
- 中途做过的调整及原因
- 助理已经给过的结论，避免重复讨论
- 用户明确拒绝或否掉的方案

不要保留：寒暄、逐条训练记录的明细、已经过时的中间讨论。

不确定的信息不要写进去，也不要自行推断或补充数字。

{existing_block}新增对话：
{conversation}

直接输出摘要正文，不超过 {max_chars} 字，不要加标题、前言或解释。"""

EXISTING_BLOCK = """已有摘要（把新增内容合并进去，保留仍然有效的部分，\
删掉已被推翻的）：
{existing}

"""


async def load_summary(
    session: AsyncSession, conversation_id: uuid.UUID,
) -> ConversationSummary | None:
    return await session.scalar(
        select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation_id,
        ),
    )


def _render_messages(rows: list[Message]) -> str:
    speaker = {"user": "用户", "assistant": "助理"}
    lines = []
    for row in rows:
        text = str(row.content.get("text", "")).strip()
        if text:
            lines.append(f"{speaker.get(row.role, row.role)}：{text[:PER_MESSAGE_CHARS]}")
    return "\n".join(lines)


async def summarize(conversation: str, existing: str | None) -> str:
    """调模型生成摘要。空输出抛 LLMError 交给任务队列退避重试。

    空输出必须当失败处理：推理模型在思考预算耗尽时返回的是 HTTP 200 + 空正文，
    不是报错。当成"这段没什么可摘要的"会让摘要功能静默退化成"从不摘要"，
    而且日志里一行报错都没有——和事实抽取踩过的是同一个坑。
    """
    provider = build_provider()
    prompt = SUMMARY_PROMPT.format(
        existing_block=EXISTING_BLOCK.format(existing=existing) if existing else "",
        conversation=conversation,
        max_chars=MAX_SUMMARY_CHARS,
    )

    chunks: list[str] = []
    async for chunk in provider.stream(ChatRequest(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=get_settings().llm_max_tokens,
    )):
        if chunk.text_delta:
            chunks.append(chunk.text_delta)

    text = "".join(chunks).strip()
    if not text:
        raise LLMError("摘要返回空输出（多半是思考预算耗尽），交给任务队列重试")

    # 模型经常无视长度要求。硬截断，否则摘要会一轮轮膨胀，最后比它省下的
    # 历史还长。
    return text[:MAX_SUMMARY_CHARS]


async def handle_summarize_conversation(session: AsyncSession, payload: dict) -> None:
    """job handler：把会话中尚未被摘要覆盖的消息增量并入摘要。"""
    conversation_id = uuid.UUID(payload["conversation_id"])
    user_id = uuid.UUID(payload["user_id"])

    existing = await load_summary(session, conversation_id)
    covered = existing.covered_seq if existing else 0

    rows = (await session.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status.in_(("done", "interrupted")),
            Message.seq > covered,
        )
        .order_by(Message.seq)
        .limit(MAX_MESSAGES_PER_RUN),
    )).all()
    if not rows:
        return

    conversation = _render_messages(list(rows))
    if not conversation:
        # 全是空文本消息。仍然推进 covered_seq，否则每轮都会重新扫到它们。
        await _upsert(session, conversation_id, user_id,
                     existing.content if existing else "", rows[-1].seq)
        return

    content = await summarize(conversation, existing.content if existing else None)
    await _upsert(session, conversation_id, user_id, content, rows[-1].seq)
    logger.info(
        f"会话摘要已更新 conversation={conversation_id} "
        f"covered_seq={rows[-1].seq} 长度={len(content)}",
    )


async def _upsert(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    covered_seq: int,
) -> None:
    """按 conversation_id upsert。

    不用"先查再插"：并发的两个回合会同时查到不存在、然后各插一行，撞唯一
    约束。ON CONFLICT 把这个竞态交给数据库解决。

    covered_seq 取两者较大值：并发更新时后提交的那个可能覆盖范围反而更小，
    直接赋值会让 covered_seq 倒退，下一轮重复摘要同一段内容。
    """
    stmt = insert(ConversationSummary).values(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        user_id=user_id,
        content=content,
        covered_seq=covered_seq,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_conversation_summaries_conv",
            set_={
                "content": stmt.excluded.content,
                "covered_seq": func.greatest(
                    ConversationSummary.covered_seq, stmt.excluded.covered_seq,
                ),
            },
        ),
    )
