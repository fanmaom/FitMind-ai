"""LLM 用量记录与统计。"""

import uuid

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage import LLMUsage


def extract_tokens(usage: dict | None) -> tuple[int, int, int]:
    """从 usage 里取三个 token 数。

    字段名各家不同（OpenAI 用 prompt_tokens，Anthropic 用 input_tokens），
    取不到就当 0——有些兼容接口干脆不返回 usage，不能因此炸掉整个请求。
    """
    if not usage:
        return 0, 0, 0

    details = usage.get("prompt_tokens_details") or {}
    return (
        int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        int(details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0),
    )


async def record_usage(
    *,
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    model: str,
    usage: dict | None,
    latency_ms: int,
    tool_calls: int,
    degradation_level: int,
) -> None:
    inp, out, cached = extract_tokens(usage)
    session.add(LLMUsage(
        user_id=user_id, conversation_id=conversation_id, model=model,
        input_tokens=inp, output_tokens=out, cached_tokens=cached,
        latency_ms=latency_ms, tool_calls=tool_calls, degradation_level=degradation_level,
    ))


async def summarize_usage(session: AsyncSession, user_id: uuid.UUID) -> dict:
    count, total_in, total_out, total_cached, degraded = (await session.execute(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.input_tokens), 0),
            func.coalesce(func.sum(LLMUsage.output_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cached_tokens), 0),
            func.coalesce(func.sum(func.cast(LLMUsage.degradation_level > 0, Integer)), 0),
        ).where(LLMUsage.user_id == user_id),
    )).one()

    return {
        "request_count": count,
        "total_input": total_in,
        "total_output": total_out,
        "total_cached": total_cached,
        "cache_hit_rate": round(total_cached / total_in, 3) if total_in else 0.0,
        "degraded_count": degraded,
        "degradation_rate": round(degraded / count, 3) if count else 0.0,
    }
