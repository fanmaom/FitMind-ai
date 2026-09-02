"""记录体重与体脂。"""

import hashlib
from datetime import date as date_type

from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.tools.registry import ToolContext, tool
from app.models.body_metric import BodyMetric


class LogBodyMetricInput(BaseModel):
    date: date_type = Field(description="测量日期，格式 YYYY-MM-DD")
    weight_kg: float = Field(gt=0, le=500, description="体重（kg）")
    body_fat_pct: float | None = Field(
        default=None, gt=0, lt=70, description="体脂率百分比，可不填",
    )


@tool(
    name="log_body_metric",
    label="记录体重体脂",
    description="记录体重（可选体脂率）。用户报告称重结果时调用。"
                "同一天同样数值重复调用不会重复写入。",
    readonly=False,
)
async def log_body_metric(inp: LogBodyMetricInput, ctx: ToolContext) -> dict:
    raw = f"{ctx.user_id}|{inp.date.isoformat()}|{inp.weight_kg}|{inp.body_fat_pct}"
    key = hashlib.sha256(raw.encode()).hexdigest()[:64]

    stmt = (
        pg_insert(BodyMetric)
        .values(user_id=ctx.user_id, date=inp.date, weight_kg=inp.weight_kg,
                body_fat_pct=inp.body_fat_pct, idempotency_key=key)
        .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
        .returning(BodyMetric.id)
    )
    row = (await ctx.session.execute(stmt)).scalar_one_or_none()
    await ctx.session.commit()

    return {
        "id": str(row) if row else None,
        "deduplicated": row is None,
        "date": inp.date.isoformat(),
        "weight_kg": inp.weight_kg,
        "body_fat_pct": inp.body_fat_pct,
    }
