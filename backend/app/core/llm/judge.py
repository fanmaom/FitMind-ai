"""一问一答式 JSON 判定调用。

服务端的判定点（记忆冲突消解、待办判重）都是同一个形状：给模型一段提示，
要一个极短的 JSON 回来。推理模型让这件事变得不平凡——它会先烧思考预算，
预算耗尽时返回**空字符串**而不是报错，而且思考长度有方差：同一个提示、同一个
max_tokens，可能这次出结果、下次出空。

实测（当前配置的模型，同一对输入）：

    max_tokens=  64 → ''
    max_tokens= 128 → ''
    max_tokens= 512 → '{"relation": "update"}'
    max_tokens=1024 → ''
    max_tokens=2048 → '{"relation": "update"}'

所以不能靠调大数字解决，必须重试。给小了的后果不是截断，而是整个判定静默
失效——调用方通常有个 except 兜底，于是功能悄悄退化成「从不判定」，没有任何
报错。这个模块把重试和空输出检测收在一处，两个判定点共用。
"""

import json

from app.core.llm.client import ChatRequest
from app.core.llm.factory import build_provider
from app.core.logger import logger

DEFAULT_MAX_TOKENS = 1024
DEFAULT_ATTEMPTS = 3


class JudgeError(RuntimeError):
    """判定调用未能拿到可解析的 JSON。"""


def _unfence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        return parts[1].removeprefix("json").strip() if len(parts) > 1 else ""
    return text


async def _once(prompt: str, max_tokens: int) -> str:
    provider = build_provider()
    parts: list[str] = []
    async for chunk in provider.stream(ChatRequest(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )):
        if chunk.text_delta:
            parts.append(chunk.text_delta)
    return _unfence("".join(parts))


async def judge_json(
    prompt: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    attempts: int = DEFAULT_ATTEMPTS,
) -> dict:
    """要一个 JSON 对象回来；空输出与不合法 JSON 都重试。

    重试之间不加退避：空输出不是限流也不是故障，是思考预算恰好用光，
    立刻重来就是最优策略。
    """
    last: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            text = await _once(prompt, max_tokens)
        except Exception as exc:  # noqa: BLE001
            last = f"调用失败：{exc}"
            logger.warning(f"判定调用第 {attempt}/{attempts} 次失败：{exc}")
            continue

        if not text:
            last = "模型返回空输出（推理预算耗尽）"
            logger.warning(f"判定调用第 {attempt}/{attempts} 次返回空输出，重试")
            continue

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            last = f"输出不是合法 JSON：{text[:120]}"
            logger.warning(f"判定调用第 {attempt}/{attempts} 次输出不合法，重试")
            continue

        if not isinstance(parsed, dict):
            last = f"输出不是 JSON 对象：{text[:120]}"
            continue
        return parsed

    raise JudgeError(f"{attempts} 次尝试均未拿到判定结果，最后一次：{last}")
