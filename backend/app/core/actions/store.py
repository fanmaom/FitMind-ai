"""待办项的持久化与近邻查询。

判重决策不在这里，见 ``dedupe.py``：向量只能给出候选，不能下结论。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.memory.embedding import embed_texts
from app.models.action_item import ActionItem

# 只是粗筛的召回门，不是判重线。实测（同一条卧推加重建议的不同说法 vs 其他建议）：
#
#   0.088 / 0.129  同一件事，改写措辞
#   0.314          同一件事，"kg" 换成 "公斤" 并加了时间词
#   0.254          **不同**动作的同款建议（深蹲加重 vs 卧推加重）
#   0.742+         不同类别的建议（补蛋白、早睡、采购）
#
# 关键是 0.254 < 0.314：教练建议高度模板化，"同模板不同动作"比"同动作不同
# 说法"更近，两类样本区间重叠，不存在能分开它们的阈值。所以向量只负责把
# 0.742 那一档排除掉，剩下的交给 LLM 判。
CANDIDATE_DISTANCE = 0.45

# 只跟未结项比。done 代表用户已经做完，同类建议下个周期再提是合理的
# （"这周把卧推加 2.5kg" 做完了，下周该再提一次）；ignored 代表用户明确
# 不要，再提就是骚扰，所以它必须参与判重。
OPEN_STATUSES = ("pending", "ignored")


async def embed(content: str) -> list[float]:
    return (await embed_texts([content]))[0]


async def find_candidates(
    session: AsyncSession,
    user_id: uuid.UUID,
    vector: list[float],
    limit: int = 3,
) -> list[ActionItem]:
    """取语义最近的几条未结待办作为判重候选。

    取 3 条而非 1 条：最近邻可能是"同模板不同动作"那类噪声，真正的重复项
    排在它后面。只看第一名会把真重复放过去。
    """
    distance = ActionItem.embedding.cosine_distance(vector)
    rows = (
        await session.execute(
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.status.in_(OPEN_STATUSES),
                distance < CANDIDATE_DISTANCE,
            )
            .order_by(distance)
            .limit(limit),
        )
    ).scalars()
    return list(rows)


async def insert_action(
    session: AsyncSession,
    user_id: uuid.UUID,
    content: str,
    category: str,
    *,
    vector: list[float] | None = None,
    source_message_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
    source_quote: str | None = None,
) -> ActionItem:
    """无条件写入。判重由调用方决定——手动录入的条目不该被判重。"""
    item = ActionItem(
        user_id=user_id,
        content=content,
        category=category,
        embedding=vector if vector is not None else await embed(content),
        source_message_id=source_message_id,
        source_conversation_id=source_conversation_id,
        source_quote=source_quote,
    )
    session.add(item)
    await session.flush()
    return item


def serialize(item: ActionItem) -> dict:
    return {
        "id": str(item.id),
        "content": item.content,
        "category": item.category,
        "status": item.status,
        "source_message_id": (
            str(item.source_message_id) if item.source_message_id else None
        ),
        "source_conversation_id": (
            str(item.source_conversation_id) if item.source_conversation_id else None
        ),
        "source_quote": item.source_quote,
        "created_at": item.created_at.isoformat(),
        "settled_at": item.settled_at.isoformat() if item.settled_at else None,
    }
