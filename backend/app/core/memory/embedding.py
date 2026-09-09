"""Embedding 客户端；与对话模型共用 OpenAI 兼容网关。"""

import httpx

from app.core.config import get_settings

_TIMEOUT_SECONDS = 30.0


class EmbeddingError(RuntimeError):
    """embedding 服务不可用或响应不合法。"""


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化，返回顺序与输入一致。"""
    if not texts:
        return []

    settings = get_settings()
    url = f"{settings.effective_embedding_base_url.rstrip('/')}/embeddings"
    payload = {"model": settings.embedding_model, "input": texts}
    # OpenAI text-embedding-3 与百炼 text-embedding-v4 都支持显式维度。
    # 固定为数据库向量列的维度，切换供应商时无需重建 pgvector 表。
    if settings.llm_provider == "qwen" or settings.embedding_model.startswith("text-embedding-3"):
        payload["dimensions"] = settings.embedding_dim

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.effective_embedding_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        if len(vectors) != len(texts):
            raise ValueError(f"期望 {len(texts)} 条向量，实际收到 {len(vectors)} 条")
        if any(len(vector) != settings.embedding_dim for vector in vectors):
            raise ValueError(f"向量维度应为 {settings.embedding_dim}")
        return vectors
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError(f"embedding 服务不可用：{exc}") from exc
