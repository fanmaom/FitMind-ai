"""事实记忆冲突消解。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.judge import judge_json
from app.core.logger import logger
from app.core.memory.embedding import embed_texts
from app.core.memory.facts import insert_fact
from app.models.memory import Memory

# 消解处理“同一件事”，比召回“相关内容”的 0.55 阈值更严格。
SIMILARITY_THRESHOLD = 0.35

JUDGE_PROMPT = """判断两条关于同一用户的记忆之间的关系。

旧记忆：{old}
新记忆：{new}

从以下四种关系中选一个：
- supersede：新的与旧的矛盾，或描述了旧情况的变化，旧的应当失效
- update：说的是同一件事，新的信息更完整，应替换旧的
- duplicate：完全同义，无需新增
- independent：两件不同的事，各自保留

只输出一个 JSON：{{"relation": "..."}}，不要任何其他文字。"""

VALID_RELATIONS = {"supersede", "update", "duplicate", "independent"}


async def _judge(old_content: str, new_content: str) -> str:
    parsed = await judge_json(
        JUDGE_PROMPT.format(old=old_content, new=new_content),
    )
    relation = parsed.get("relation", "independent")
    return relation if relation in VALID_RELATIONS else "independent"


async def _find_similar(
    session: AsyncSession,
    user_id: uuid.UUID,
    content: str,
) -> Memory | None:
    vector = (await embed_texts([content]))[0]
    distance = Memory.embedding.cosine_distance(vector)
    now = datetime.now(timezone.utc)
    row = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.user_id == user_id,
                Memory.superseded_by.is_(None),
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                distance < SIMILARITY_THRESHOLD,
            )
            .order_by(distance)
            .limit(1),
        )
    ).first()
    return row[0] if row else None


async def reconcile_fact(
    session: AsyncSession,
    user_id: uuid.UUID,
    content: str,
    category: str,
    confidence: float,
    source_message_id: uuid.UUID | None,
) -> Memory | None:
    """消解后写入；重复返回 None，其余返回新建记忆。"""
    similar = await _find_similar(session, user_id, content)
    if similar is None:
        return await insert_fact(
            session, user_id, content, category, confidence, source_message_id,
        )

    try:
        relation = await _judge(similar.content, content)
    except Exception as exc:  # noqa: BLE001
        # 判定失败不能静默丢记忆，宁可保留两条供用户后续清理。
        logger.warning(f"冲突判定失败，降级为直接新增：{exc}")
        relation = "independent"

    if relation == "duplicate":
        logger.info(f"记忆重复，跳过：{content[:40]}")
        return None

    created = await insert_fact(
        session, user_id, content, category, confidence, source_message_id,
    )
    if relation in ("supersede", "update"):
        similar.superseded_by = created.id
        logger.info(f"记忆 {relation}：{similar.content[:30]} → {content[:30]}")
    await session.flush()
    return created
