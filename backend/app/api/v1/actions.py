"""待办项 API。"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from app.api.deps import RequestContext, get_context
from app.core.actions.store import insert_action, serialize
from app.core.memory.embedding import EmbeddingError
from app.models.action_item import VALID_STATUSES, ActionItem

router = APIRouter(prefix="/api/v1", tags=["actions"])

MAX_CONTENT_LENGTH = 500


@router.get("/action-items")
async def list_action_items(
    item_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(get_context),
) -> list[dict]:
    if item_status is not None and item_status not in VALID_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status 只能是 {'/'.join(VALID_STATUSES)}",
        )
    statement = select(ActionItem)
    if item_status:
        statement = statement.where(ActionItem.status == item_status)
    rows = await ctx.session.scalars(
        statement.order_by(ActionItem.created_at.desc()).offset(offset).limit(limit),
    )
    return [serialize(item) for item in rows]


@router.post("/action-items", status_code=201)
async def create_action_item(
    payload: dict = Body(...),
    ctx: RequestContext = Depends(get_context),
) -> dict:
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "content 不能为空")
    category = str(payload.get("category") or "general")

    try:
        # 手动录入不判重：用户亲手打出来的条目，他知道自己在写什么。
        item = await insert_action(
            ctx.session, ctx.user_id, content[:MAX_CONTENT_LENGTH], category,
        )
    except EmbeddingError as exc:
        # 向量列 NOT NULL，拿不到向量就写不进去。明确报 503 而不是 500——
        # 这是依赖不可用，重试有意义。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"向量服务不可用，稍后重试：{exc}",
        ) from exc
    await ctx.session.commit()
    return serialize(item)


@router.patch("/action-items/{item_id}")
async def update_action_item(
    item_id: uuid.UUID,
    payload: dict = Body(...),
    ctx: RequestContext = Depends(get_context),
) -> dict:
    next_status = payload.get("status")
    if next_status not in VALID_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status 只能是 {'/'.join(VALID_STATUSES)}",
        )

    item = await ctx.session.scalar(select(ActionItem).where(ActionItem.id == item_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "待办不存在")

    item.status = next_status
    # 改回 pending 视为「重新打开」，结算时间要清掉，否则面板会显示一条
    # 既未完成又有完成时间的条目。
    item.settled_at = None if next_status == "pending" else datetime.now(timezone.utc)
    await ctx.session.commit()
    return serialize(item)


@router.delete("/action-items/{item_id}", status_code=204)
async def delete_action_item(
    item_id: uuid.UUID,
    ctx: RequestContext = Depends(get_context),
) -> None:
    result = await ctx.session.execute(
        delete(ActionItem).where(ActionItem.id == item_id).returning(ActionItem.id),
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "待办不存在")
    await ctx.session.commit()
