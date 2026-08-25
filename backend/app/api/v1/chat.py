"""对话路由。"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import RequestContext, get_context
from app.core.logger import logger
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.chat_service import run_turn

router = APIRouter(prefix="/api/v1", tags=["chat"])


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str | None = Field(default=None, max_length=64)


async def _get_conversation(ctx: RequestContext, conversation_id: uuid.UUID) -> Conversation:
    # RLS 已挡住跨租户读取，这里显式确认存在性以返回 404 而不是空列表
    conv = await ctx.session.scalar(
        select(Conversation).where(Conversation.id == conversation_id),
    )
    if conv is None:
        raise HTTPException(404, "会话不存在")
    return conv


@router.post("/conversations", status_code=201)
async def create_conversation(ctx: RequestContext = Depends(get_context)) -> dict:
    conv = Conversation(user_id=ctx.user_id, title="新对话")
    ctx.session.add(conv)
    await ctx.session.commit()
    return {"id": str(conv.id), "title": conv.title}


@router.get("/conversations")
async def list_conversations(ctx: RequestContext = Depends(get_context)) -> list[dict]:
    rows = await ctx.session.scalars(
        select(Conversation).order_by(Conversation.created_at.desc()),
    )
    return [{"id": str(c.id), "title": c.title,
             "created_at": c.created_at.isoformat()} for c in rows]


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
) -> list[dict]:
    await _get_conversation(ctx, conversation_id)
    rows = await ctx.session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at),
    )
    return [{"id": str(r.id), "role": r.role, "content": r.content,
             "status": r.status} for r in rows]


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    ctx: RequestContext = Depends(get_context),
) -> EventSourceResponse:
    await _get_conversation(ctx, conversation_id)

    # 只取出 user_id 交给后台任务。绝不能把 ctx.session 传进去——
    # 那个 session 归请求作用域所有，SSE 一结束就被 get_db 关闭，
    # 而后台任务还在用它。run_turn 自己开 session。
    user_id = ctx.user_id

    # Agent 循环跑在独立 task 上，SSE 连接只是它的观察者。
    #
    # 若把循环直接挂在请求的 task 上，客户端一断 CancelledError 会把正在跑
    # 的循环一起取消——"服务端跑完再落库"就成了空话，用户重连什么都拿不到。
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in run_turn(
                user_id, conversation_id, payload.text, payload.client_message_id,
            ):
                await queue.put(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("回合执行失败")
            await queue.put({
                "event": "error",
                "data": {"message": str(exc), "degradation_level": 5},
            })
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce())

    async def consume():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield {"event": event["event"],
                       "data": json.dumps(event["data"], ensure_ascii=False)}
        except asyncio.CancelledError:
            # 客户端断开：不取消 produce，让它跑完并落库
            logger.info("客户端断开，回合继续在后台执行")
            raise
        finally:
            if not task.done():
                logger.info("SSE 已关闭但回合仍在运行，交由后台完成")

    return EventSourceResponse(consume())
