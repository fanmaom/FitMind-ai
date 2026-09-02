"""待办判重：向量粗筛 + LLM 判定。

为什么不能只用向量：见 ``store.CANDIDATE_DISTANCE`` 的实测数据。教练建议
高度模板化，"把深蹲加到 120kg" 和 "把卧推加到 82.5kg" 的向量距离比同一条
建议的两种说法更近，没有阈值能分开这两类。向量只用来把明显无关的排除掉。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions.store import embed, find_candidates, insert_action
from app.core.llm.judge import judge_json
from app.core.logger import logger
from app.models.action_item import ActionItem

JUDGE_PROMPT = """判断这两条健身待办说的是不是同一件要做的事。

已有待办：{existing}
新待办：{candidate}

注意区分：动作、部位、食物、数字不同就是不同的事，即使句式一样。
例如"把深蹲加到 120kg"和"把卧推加到 82.5kg"是**不同**的两件事。
反之措辞、单位、时间词不同但指向同一个动作同一个目标的，是同一件事。

只输出一个 JSON：{{"same": true}} 或 {{"same": false}}，不要任何其他文字。"""


async def _is_same_task(existing: str, candidate: str) -> bool:
    parsed = await judge_json(
        JUDGE_PROMPT.format(existing=existing, candidate=candidate),
    )
    return bool(parsed.get("same", False))


async def insert_if_new(
    session: AsyncSession,
    user_id: uuid.UUID,
    content: str,
    category: str,
    *,
    source_message_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
    source_quote: str | None = None,
) -> ActionItem | None:
    """判重后写入；被判为已有则返回 None。"""
    vector = await embed(content)
    for existing in await find_candidates(session, user_id, vector):
        try:
            same = await _is_same_task(existing.content, content)
        except Exception as exc:  # noqa: BLE001
            # 判定失败不能静默丢建议——用户看不到的东西无法自己纠正。
            # 宁可多显示一条近似的，让他自己划掉。
            logger.warning(f"待办判重失败，降级为不重复：{exc}")
            break
        if same:
            logger.info(f"待办重复，跳过：{content[:40]}（已有：{existing.content[:40]}）")
            return None

    return await insert_action(
        session, user_id, content, category,
        vector=vector,
        source_message_id=source_message_id,
        source_conversation_id=source_conversation_id,
        source_quote=source_quote,
    )
