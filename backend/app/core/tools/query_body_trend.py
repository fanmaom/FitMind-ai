"""查询体重趋势（返回聚合值）。"""

from datetime import date as date_type, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.tools.registry import ToolContext, tool
from app.models.body_metric import BodyMetric


class QueryBodyTrendInput(BaseModel):
    weeks: int = Field(default=8, ge=1, le=104, description="回溯多少周，默认 8 周")


@tool(
    name="query_body_trend",
    description="查询体重变化趋势，返回起止体重、总变化、周均变化率。"
                "回答「我掉了多少」「进度怎么样」时调用。",
    readonly=True,
)
async def query_body_trend(inp: QueryBodyTrendInput, ctx: ToolContext) -> dict:
    since = date_type.today() - timedelta(weeks=inp.weeks)
    rows = (await ctx.session.scalars(
        select(BodyMetric).where(BodyMetric.date >= since).order_by(BodyMetric.date),
    )).all()

    if len(rows) < 2:
        return {
            "weeks": inp.weeks, "record_count": len(rows),
            "note": f"最近 {inp.weeks} 周的体重记录不足 2 条，无法计算趋势。",
        }

    first, last = rows[0], rows[-1]
    span_days = (last.date - first.date).days
    total_change = last.weight_kg - first.weight_kg

    # 同日多条记录时 span 为 0，除法会炸——按 1 天兜底
    span_weeks = max(span_days, 1) / 7

    return {
        "weeks": inp.weeks,
        "record_count": len(rows),
        "first_date": first.date.isoformat(),
        "latest_date": last.date.isoformat(),
        "start_kg": first.weight_kg,
        "latest_kg": last.weight_kg,
        "total_change_kg": round(total_change, 2),
        "weekly_rate_kg": round(total_change / span_weeks, 3),
        "latest_body_fat_pct": last.body_fat_pct,
        "note": "",
    }
