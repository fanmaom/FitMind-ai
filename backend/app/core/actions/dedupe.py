"""待办判重：向量粗筛 + LLM 判定。

为什么不能只用向量：见 ``store.CANDIDATE_DISTANCE`` 的实测数据。教练建议
高度模板化，"把深蹲加到 120kg" 和 "把卧推加到 82.5kg" 的向量距离比同一条
建议的两种说法更近，没有阈值能分开这两类。向量只用来把明显无关的排除掉。
"""

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions.store import embed, find_candidates, insert_action
from app.core.llm.judge import judge_json
from app.core.llm.judge_cache import action_dedupe_cache
from app.core.logger import logger
from app.models.action_item import ActionItem

JUDGE_PROMPT = """判断这两条健身待办说的是不是同一件要做的事。

已有待办：{existing}
新待办：{candidate}

注意区分：动作、部位、食物、数字不同就是不同的事，即使句式一样。
例如"把深蹲加到 120kg"和"把卧推加到 82.5kg"是**不同**的两件事。
反之措辞、单位、时间词不同但指向同一个动作同一个目标的，是同一件事。

只输出一个 JSON：{{"same": true}} 或 {{"same": false}}，不要任何其他文字。"""


def _cache_key(existing: str, candidate: str) -> str:
    """两条文本的判定 key。

    用哈希而不是原文拼接：待办文本没有长度上限，直接当 dict key 会让缓存
    占用随文本长度增长。顺序保留（不排序）——虽然"是否同一件事"在语义上
    对称，但 prompt 里两者位置不同，模型的回答不保证一致，把两个方向当成
    同一个 key 会让缓存返回另一个方向的结果。
    """
    raw = f"{existing}\x00{candidate}".encode()
    return hashlib.sha256(raw).hexdigest()


async def _is_same_task(existing: str, candidate: str) -> bool:
    """判定两条待办是否同一件事。结果走缓存——单次调用实测 2.8–4.3 秒，
    而 job 退避重试会把整批待办重新判一遍。"""

    async def compute() -> bool:
        parsed = await judge_json(
            JUDGE_PROMPT.format(existing=existing, candidate=candidate),
        )
        return bool(parsed.get("same", False))

    result = await action_dedupe_cache.get_or_compute(
        _cache_key(existing, candidate), compute,
    )
    return bool(result)


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
    normalized = content.strip()
    for existing in await find_candidates(session, user_id, vector):
        # 文本完全相同就不必问模型了。judge 单次 2.8–4.3 秒，而这种情况在
        # job 退避重试里很常见——同一条建议被重新抽出来、和上次写进去的
        # 那条逐字比对。缓存也能挡住，但那要等第一次判完；这里直接短路。
        if existing.content.strip() == normalized:
            logger.info(f"待办逐字重复，跳过：{content[:40]}")
            return None
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
