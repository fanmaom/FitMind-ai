"""应用配置。所有环境变量在此集中校验，缺失即启动失败。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 相对 __file__ 解析而非 CWD——否则从 backend/ 还是仓库根启动会得到不同结果。
# 两个位置都找，靠后的优先：仓库根的 .env 同时被 docker compose 的 env_file 使用。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ENV_FILES = (_BACKEND_DIR / ".env", _BACKEND_DIR.parent / ".env")


class Settings(BaseSettings):
    """应用配置。字段缺失或不合法时构造即抛 ValidationError。"""

    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    # 数据库
    database_url: str = Field(description="postgresql+asyncpg://...")
    worker_database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 认证
    jwt_secret: str = Field(min_length=32, description="至少 32 字符")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    # LLM
    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_base_url: str = ""
    llm_api_key: str = Field(description="模型服务 API Key")
    llm_model: str = "gpt-4o-mini"
    llm_fallback_model: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # Agent
    agent_max_turns: int = 6
    agent_tool_timeout_s: float = 3.0
    agent_history_window: int = 6

    @field_validator("database_url")
    @classmethod
    def _must_be_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url 必须使用 asyncpg 驱动（postgresql+asyncpg://）")
        return v

    @field_validator("worker_database_url")
    @classmethod
    def _worker_must_be_async_driver_or_empty(cls, v: str) -> str:
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError("worker_database_url 必须为空或使用 asyncpg 驱动")
        return v

    @property
    def llm_configured(self) -> bool:
        """模型服务是否已配置。

        空字符串会覆盖字段默认值，所以不能只靠"字段存在"判断。
        LLM 不配置时数据库、认证、记录功能仍可用，因此这里不在启动时硬失败，
        而是启动时告警 + build_provider() 时报明确错误。
        """
        return bool(self.llm_api_key.strip() and self.llm_model.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
