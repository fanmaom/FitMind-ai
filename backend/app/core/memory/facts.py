"""L2 事实层：写入与带距离阈值的语义召回。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import logger
from app.core.memory.embedding import EmbeddingError, embed_texts
from app.models.memory import Memory

# 实测相关记忆余弦距离约 0.31，无关内容从约 0.69 起，中间存在明显间隔。
# 召回不能只取 top-k：没有阈值时，无关记忆也必然会占满 k 个位置。
MAX_RECALL_DISTANCE = 0.55


async def insert_fact(
    session: AsyncSession,
    user_id: uuid.UUID,
    content: str,
    category: str,
    confidence: float,
    source_message_id: uuid.UUID | None,
) -> Memory:
    vector = (await embed_texts([content]))[0]
    memory = Memory(
        user_id=user_id,
        content=content,
        category=category,
        embedding=vector,
        confidence=confidence,
        source_message_id=source_message_id,
    )
    session.add(memory)
    await session.flush()
    return memory


async def recall(
    session: AsyncSession,
    user_id: uuid.UUID,
    query: str,
    k: int = 5,
) -> list[Memory]:
    """召回有效且相关的事实；向量服务失败时安全降级为空记忆。"""
    if k <= 0:
        return []

    try:
        query_vector = (await embed_texts([query]))[0]
    except EmbeddingError as exc:
        logger.warning(f"召回失败，本轮无记忆注入：{exc}")
        return []

    now = datetime.now(timezone.utc)
    distance = Memory.embedding.cosine_distance(query_vector)
    rows = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.user_id == user_id,
                Memory.superseded_by.is_(None),
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                distance < MAX_RECALL_DISTANCE,
            )
            .order_by(distance)
            .limit(k),
        )
    ).all()
    return [row[0] for row in rows]
