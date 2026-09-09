"""按配置构造 LLMProvider。"""

from app.core.config import get_settings
from app.core.llm.client import LLMProvider
from app.core.llm.openai_provider import OpenAIProvider

def build_provider(model: str | None = None) -> LLMProvider:
    """构造主模型或指定模型的 provider。

    LLM 未配置时在这里报明确错误——空模型名发出去只会换回一个看不懂的
    上游报错。配置校验放在真正要用的时刻，这样数据库、认证、记录这些
    不依赖模型的功能不受影响（见 config.llm_configured）。
    """
    settings = get_settings()

    if not settings.llm_configured:
        raise RuntimeError(
            "LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL。"
            "对话功能依赖模型服务，其余功能不受影响。",
        )

    target = model or settings.llm_model
    base_url = settings.effective_llm_base_url

    if settings.llm_provider in {"openai", "qwen"}:
        return OpenAIProvider(base_url=base_url, api_key=settings.llm_api_key, model=target)

    if settings.llm_provider == "anthropic":
        from app.core.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(base_url=base_url, api_key=settings.llm_api_key, model=target)

    raise ValueError(f"未知的 LLM_PROVIDER：{settings.llm_provider}")
