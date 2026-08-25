"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.logs import router as logs_router
from app.api.v1.memory import router as memory_router
from app.core.config import get_settings
from app.core.logger import logger
from app.core.tools.registry import load_tools


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    load_tools()
    if settings.llm_configured:
        logger.info(f"启动中，LLM provider={settings.llm_provider} model={settings.llm_model}")
    else:
        logger.warning(
            "启动中，但 LLM 未配置（LLM_API_KEY / LLM_MODEL 为空）。"
            "对话功能将不可用；数据库、认证、记录功能正常。请在 .env 中补齐。",
        )
    yield
    logger.info("已关闭")


app = FastAPI(title="AI 健身助理", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(logs_router)
app.include_router(chat_router)
app.include_router(memory_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
