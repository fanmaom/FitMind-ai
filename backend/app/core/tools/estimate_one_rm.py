"""由次极限组估算 1RM。"""

from pydantic import BaseModel, Field

from app.core.domain.strength import estimate_1rm as _estimate
from app.core.tools.registry import ToolContext, tool


class EstimateOneRmInput(BaseModel):
    weight_kg: float = Field(gt=0, le=1000, description="该组使用的重量（kg）")
    reps: int = Field(ge=1, le=30, description="该组完成的次数，超过 30 次公式失准")


@tool(
    name="estimate_1rm",
    label="估算极限重量",
    description="用 Epley 公式由一组次极限训练估算单次最大重量(1RM)。"
                "排增力计划前需要 1RM 而用户只报了某组重量次数时调用。",
    readonly=True,
)
async def estimate_one_rm(inp: EstimateOneRmInput, ctx: ToolContext) -> dict:
    return {"one_rm": _estimate(inp.weight_kg, inp.reps)}
