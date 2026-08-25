"""用户档案与事实记忆 API。"""

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from app.api.deps import RequestContext, get_context
from app.core.memory.profile import load_profile, merge_profile
from app.models.memory import Memory

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


@router.get("/memories")
async def list_memories(
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(get_context),
) -> list[dict]:
    statement = select(Memory).where(Memory.superseded_by.is_(None))
    if category:
        statement = statement.where(Memory.category == category)
    rows = await ctx.session.scalars(
        statement.order_by(Memory.created_at.desc()).offset(offset).limit(limit),
    )
    return [
        {
            "id": str(memory.id),
            "content": memory.content,
            "category": memory.category,
            "confidence": memory.confidence,
            "created_at": memory.created_at.isoformat(),
        }
        for memory in rows
    ]


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: uuid.UUID,
    ctx: RequestContext = Depends(get_context),
) -> None:
    result = await ctx.session.execute(
        delete(Memory).where(Memory.id == memory_id).returning(Memory.id),
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    await ctx.session.commit()
