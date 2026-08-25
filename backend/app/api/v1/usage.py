"""当前用户的模型用量与可靠性指标。"""

from fastapi import APIRouter, Depends

from app.api.deps import RequestContext, get_context
from app.core.llm.usage_recorder import summarize_usage

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


@router.get("/summary")
async def usage_summary(ctx: RequestContext = Depends(get_context)) -> dict:
    return await summarize_usage(ctx.session, ctx.user_id)
