"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.actions import router as actions_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.logs import router as logs_router
from app.api.v1.memory import router as memory_router
from app.api.v1.plans import router as plans_router
from app.api.v1.usage import router as usage_router
from app.core.config import get_settings
from app.core.logger import logger
from app.core.mcp.bridge import mcp_registry
from app.core.mcp.client import parse_servers
from app.core.tools.registry import load_tools
from app.services import interrupt


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    load_tools()
    # 外部 MCP 工具。放在 load_tools 之后：本地工具先占好名字，
    # 外部工具撞名时跳过而不是覆盖——本地的是我们能负责的那些。
    await _load_mcp(settings)
    # 中断信号的跨进程广播。起不来只告警——退化成单进程行为，本进程内的中断
    # 照常工作，比因为一个辅助通道连不上就让整个服务起不来要好。
    await interrupt.start_listener()
    if settings.llm_configured:
        logger.info(f"启动中，LLM provider={settings.llm_provider} model={settings.llm_model}")
    else:
        logger.warning(
            "启动中，但 LLM 未配置（LLM_API_KEY / LLM_MODEL 为空）。"
            "对话功能将不可用；数据库、认证、记录功能正常。请在 .env 中补齐。",
        )
    yield
    await mcp_registry.shutdown()
    await interrupt.stop_listener()
    logger.info("已关闭")


async def _load_mcp(settings) -> None:
    """加载外部 MCP server。配置有问题或连不上都只告警。

    整段包在 try 里：解析配置也可能抛（JSON 写错），而一个可选功能的配置笔误
    不该让整个服务起不来——那会让用户在完全无关的地方排查半天。
    """
    if not settings.mcp_servers.strip():
        return
    try:
        configs = parse_servers(settings.mcp_servers)
    except ValueError as exc:
        logger.warning(f"MCP 配置无法解析，已跳过全部外部工具：{exc}")
        return
    await mcp_registry.load(configs, timeout_s=settings.mcp_tool_timeout_s)


app = FastAPI(title="AI 健身助理", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in get_settings().cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(logs_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(actions_router)
app.include_router(plans_router)
app.include_router(usage_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
