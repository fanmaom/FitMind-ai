"""在回复完成后从助理的建议中沉淀待跟进事项。"""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions.dedupe import insert_if_new
from app.core.config import get_settings
from app.core.llm.client import ChatRequest, LLMError
from app.core.llm.factory import build_provider
from app.core.logger import logger

MIN_CONFIDENCE = 0.7
MAX_ITEMS_PER_TURN = 3
VALID_CATEGORIES = {"training", "nutrition", "recovery", "general"}

EXTRACT_PROMPT = """从下面这段健身助理与用户的对话中，抽取助理建议用户去做的、可执行的事项。

只抽取满足以下全部条件的内容：
- 由**助理**提出，不是用户自己的想法
- 是一件用户能去做完、做完后能勾掉的具体事（有动作、有对象）
- 出了这段对话仍然需要跟进

不要抽取：
- 助理对既有数据的陈述、计算结果、解释说明
- 用户已经在这轮对话里做完的事（记录训练、更新体重——那些已经落库了）
- "保持规律作息"、"注意补充水分"这类无从判断做完没做完的泛泛叮嘱
- 助理提的问题（那是在问信息，不是让用户去做事）

以 JSON 数组输出，最多 {max_items} 项，按重要性排序。每项含：
- content：一句话中文，祈使句，带上关键数字（如"把卧推工作重量加到 82.5kg"）
- category：training（训练执行）/ nutrition（饮食）/ recovery（恢复与睡眠）/ general
- quote：助理原文中对应的那一句，照抄，不要改写
- confidence：0-1，你对"助理确实要求用户去做这件事"的把握

没有值得跟进的就输出 []。只输出 JSON，不要任何其他文字。

对话：
{conversation}"""


@dataclass
class ExtractedAction:
    content: str
    category: str
    quote: str | None
    confidence: float


def _parse(raw: str) -> list[ExtractedAction]:
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].removeprefix("json").strip() if len(parts) > 1 else ""
    try:
        items = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"待办抽取输出不是合法 JSON，本次跳过：{text[:120]}")
        return []
    if not isinstance(items, list):
        return []

    actions: list[ExtractedAction] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        category = item.get("category", "general")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        quote = item.get("quote")
        actions.append(ExtractedAction(
            content=str(item["content"])[:500],
            category=category if category in VALID_CATEGORIES else "general",
            quote=str(quote)[:500] if quote else None,
            confidence=max(0.0, min(1.0, confidence)),
        ))
    # 模型无视 max_items 是常事，截断兜底：一轮对话冒出十条待办，
    # 面板会直接失去可用性。
    return actions[:MAX_ITEMS_PER_TURN]


async def extract_actions(conversation_text: str) -> list[ExtractedAction]:
    provider = build_provider()
    chunks: list[str] = []
    async for chunk in provider.stream(ChatRequest(
        messages=[{
            "role": "user",
            "content": EXTRACT_PROMPT.format(
                conversation=conversation_text, max_items=MAX_ITEMS_PER_TURN,
            ),
        }],
        # 与事实抽取同理：写死的小预算会被推理模型的思考吃光，正文变成空字符串。
        max_tokens=get_settings().llm_max_tokens,
    )):
        if chunk.text_delta:
            chunks.append(chunk.text_delta)

    raw = "".join(chunks)
    if not raw.strip():
        raise LLMError("待办抽取返回空输出（多半是思考预算耗尽），交给任务队列重试")
    return _parse(raw)


async def handle_extract_actions(session: AsyncSession, payload: dict) -> None:
    """抽取、过滤低置信项并去重写入；事务由 worker 调用方管理。"""
    user_id = uuid.UUID(payload["user_id"])
    source_message_id = (
        uuid.UUID(payload["source_message_id"])
        if payload.get("source_message_id") else None
    )
    source_conversation_id = (
        uuid.UUID(payload["source_conversation_id"])
        if payload.get("source_conversation_id") else None
    )
    actions = await extract_actions(payload["conversation_text"])
    kept = [action for action in actions if action.confidence >= MIN_CONFIDENCE]
    if len(kept) < len(actions):
        logger.info(
            f"待办抽取：{len(actions)} 条中 {len(actions) - len(kept)} 条低于置信度阈值被丢弃",
        )
    for action in kept:
        await insert_if_new(
            session, user_id, action.content, action.category,
            source_message_id=source_message_id,
            source_conversation_id=source_conversation_id,
            source_quote=action.quote,
        )
