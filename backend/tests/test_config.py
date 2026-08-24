"""配置校验测试：缺失或不合法的环境变量必须在构造阶段就失败。"""

import pytest
from pydantic import ValidationError


def test_settings_requires_database_url(monkeypatch):
    """缺 DATABASE_URL 时必须在构造阶段就报错，而不是等到第一次请求。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_short_jwt_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "tooshort")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_sync_driver(monkeypatch):
    """用同步驱动会让整个异步栈退化成阻塞，必须拦下。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 32)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_loads_valid_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 32)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

    from app.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_provider == "openai"
    assert settings.agent_max_turns == 6


def test_empty_llm_credentials_reported_as_unconfigured(monkeypatch):
    """空字符串会覆盖字段默认值——不能只靠"字段存在"判断已配置。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "z" * 32)
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")

    from app.core.config import Settings

    assert Settings(_env_file=None).llm_configured is False


def test_filled_llm_credentials_reported_as_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "z" * 32)
    monkeypatch.setenv("LLM_API_KEY", "sk-real")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

    from app.core.config import Settings

    assert Settings(_env_file=None).llm_configured is True
