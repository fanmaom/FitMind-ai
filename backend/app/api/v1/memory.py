"""用户档案与记忆 API。"""

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.deps import RequestContext, get_context
from app.core.memory.profile import load_profile, merge_profile

router = APIRouter(prefix="/api/v1", tags=["memory"])


@router.get("/profile")
async def get_profile(ctx: RequestContext = Depends(get_context)) -> dict:
    return await load_profile(ctx.session, ctx.user_id)


@router.patch("/profile")
async def patch_profile(
    updates: dict = Body(...),
    ctx: RequestContext = Depends(get_context),
) -> dict:
    try:
        return await merge_profile(ctx.session, ctx.user_id, updates)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
