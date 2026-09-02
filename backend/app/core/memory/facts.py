"""L2 事实层：写入与带距离阈值的语义召回。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
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
    query: str | list[str],
    k: int = 5,
) -> list[Memory]:
    """召回有效且相关的事实；向量服务失败时安全降级为空记忆。

    query 可以是多条。多条时取**每条记忆到各 query 的最小距离**排序，等价于
    「命中任意一路检索即可」，而不是把多路结果各取 top-k 再拼起来。

    后者是更容易想到的写法，但有个隐蔽的坑：拼接需要为每一路单独分配名额，
    而名额怎么分都不对——续问时通常某一路信息量明显更高，均分会让弱的那路
    挤掉强的那路的有效结果。取最小距离则天然不需要分名额，k 个位置始终留给
    全局最相关的 k 条。

    同一条记忆同时被两路命中时也只会出现一次——距离取 min 后仍是一行。
    """
    queries = [query] if isinstance(query, str) else [q for q in query if q.strip()]
    if k <= 0 or not queries:
        return []

    try:
        # 一次批量调用拿全部 query 向量：多路检索不该变成多次网络往返，
        # 这条召回在用户等回复的主链路上。
        query_vectors = await embed_texts(queries)
    except EmbeddingError as exc:
        logger.warning(f"召回失败，本轮无记忆注入：{exc}")
        return []

    now = datetime.now(timezone.utc)
    distances = [Memory.embedding.cosine_distance(vector) for vector in query_vectors]
    # func.least 需要至少两个参数，单 query 时直接用该表达式。
    distance = distances[0] if len(distances) == 1 else func.least(*distances)
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
