"""在回复完成后从对话中抽取长期事实。"""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.client import ChatRequest
from app.core.llm.factory import build_provider
from app.core.logger import logger
from app.core.memory.facts import insert_fact

MIN_CONFIDENCE = 0.7
VALID_CATEGORIES = {
    "injury", "preference", "scenario", "performance", "constraint", "general",
}

EXTRACT_PROMPT = """从下面这段健身助理与用户的对话中，抽取值得长期记住的事实。

只抽取满足以下全部条件的内容：
- 关于用户本人的稳定信息（伤病、偏好、忌口、场景、成绩、限制）
- 在未来的对话中仍然有用
- 用户明确表达过，不是你的推测

不要抽取：
- 一次性的数据记录（今天练了什么、体重多少）——那些已经存在日志里
- 助理自己说的话
- 泛泛的健身常识

以 JSON 数组输出，每项含 content（一句话，中文）、category
（injury/preference/scenario/performance/constraint/general）、
confidence（0-1，你对“用户确实表达过这件事”的把握）。
没有值得记的就输出 []。只输出 JSON，不要任何其他文字。

对话：
{conversation}"""


@dataclass
class ExtractedFact:
    content: str
    category: str
    confidence: float


def _parse(raw: str) -> list[ExtractedFact]:
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].removeprefix("json").strip() if len(parts) > 1 else ""
    try:
        items = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"事实抽取输出不是合法 JSON，本次跳过：{text[:120]}")
        return []
    if not isinstance(items, list):
        return []

    facts: list[ExtractedFact] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        category = item.get("category", "general")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        facts.append(ExtractedFact(
            content=str(item["content"])[:500],
            category=category if category in VALID_CATEGORIES else "general",
            confidence=max(0.0, min(1.0, confidence)),
        ))
    return facts


async def extract_facts(conversation_text: str) -> list[ExtractedFact]:
    provider = build_provider()
    chunks: list[str] = []
    async for chunk in provider.stream(ChatRequest(
        messages=[{
            "role": "user",
            "content": EXTRACT_PROMPT.format(conversation=conversation_text),
        }],
        max_tokens=1024,
    )):
        if chunk.text_delta:
            chunks.append(chunk.text_delta)
    return _parse("".join(chunks))


async def handle_extract_memory(session: AsyncSession, payload: dict) -> None:
    """抽取、过滤低置信事实并写入；事务由 worker 调用方管理。"""
    user_id = uuid.UUID(payload["user_id"])
    source_message_id = (
        uuid.UUID(payload["source_message_id"])
        if payload.get("source_message_id") else None
    )
    facts = await extract_facts(payload["conversation_text"])
    kept = [fact for fact in facts if fact.confidence >= MIN_CONFIDENCE]
    if len(kept) < len(facts):
        logger.info(
            f"事实抽取：{len(facts)} 条中 {len(facts) - len(kept)} 条低于置信度阈值被丢弃",
        )
    for fact in kept:
        # T24 接入 reconcile_fact；T23 先保证异步抽取/队列链路独立可用。
        await insert_fact(
            session, user_id, fact.content, fact.category,
            fact.confidence, source_message_id,
        )
