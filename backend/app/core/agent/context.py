"""Prompt 组装与 Token 预算。

排序即缓存策略：稳定内容在前，易变内容在后。Prompt 缓存是前缀匹配，
前面变一个字节，后面全部失效。
"""

from dataclasses import dataclass

from app.core.agent.glossary import prompt_label, render_value
from app.core.agent.prompts import SYSTEM_PROMPT
from app.core.memory.profile import PROFILE_FIELDS

MAX_HISTORY_TOKENS = 3000
KEEP_RECENT_TURNS = 6


@dataclass
class ContextBudget:
    system: int
    profile: int
    facts: int
    history: int
    total: int


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中文约 1 字 1 token，其余约 4 字符 1 token。

    只用于预算控制，不需要精确——精确值由 API 返回的 usage 记录。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1


def _render_profile(profile: dict) -> str:
    """把档案渲染成人话。

    不用 JSON dump：那样注入的是 weight_kg / cut / office 这类内部标识符，
    而模型会把提示里看到的写法照抄进回复——线上出现过"周中日间 office、其余 cook"。
    模型手上没有内部名字，就抄不出来。

    顺序按 PROFILE_FIELDS 的定义顺序，与 dict 插入顺序无关：档案每轮注入提示，
    渲染不稳定会打断缓存前缀，而且没有任何报错，只体现为账单偏高。
    """
    if not profile:
        return "用户档案：暂无。首次对话请引导用户补充身高、体重、目标。"

    lines = [
        f"- {prompt_label(key)}：{render_value(profile[key])}"
        for key in PROFILE_FIELDS if key in profile
    ]
    # 白名单之外的键正常不该存在（写入经过 validate_fields），留个兜底不丢信息。
    lines += [
        f"- {key}：{render_value(profile[key])}"
        for key in sorted(k for k in profile if k not in PROFILE_FIELDS)
    ]
    return "用户档案（每轮自动注入，无需重复询问）：\n" + "\n".join(lines)


def _render_facts(facts: list[str]) -> str:
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return f"关于这位用户你已知的事实（按相关度召回）：\n{lines}"


def compress_history(history: list[dict], keep_recent: int, max_tokens: int) -> list[dict]:
    """三段式压缩：最近 keep_recent 轮原样保留，更早的在超预算时从最旧开始丢弃。

    敢丢的前提是记忆系统已经把该记的抽取到 L2 事实层了——历史对话只负责
    短期连贯性，长期记忆是另一套机制的事。两个机制缺一个另一个就跛脚：
    只压缩不记忆会失忆，只记忆不压缩会爆上下文。
    """
    if not history:
        return []
    if len(history) <= keep_recent:
        return list(history)

    recent = history[-keep_recent:]
    older = history[:-keep_recent]

    used = sum(estimate_tokens(str(m.get("content", ""))) for m in recent)
    kept: list[dict] = []
    for msg in reversed(older):
        cost = estimate_tokens(str(msg.get("content", "")))
        if used + cost > max_tokens:
            break
        kept.insert(0, msg)
        used += cost

    return kept + recent


def build_context(
    *,
    profile: dict,
    facts: list[str],
    history: list[dict],
    user_text: str,
    today: str,
) -> tuple[list[dict], ContextBudget]:
    """组装本轮 messages。顺序即缓存策略，不要随意调整。"""
    profile_block = _render_profile(profile)
    facts_block = _render_facts(facts)

    # 1 系统提示：恒定，作为缓存前缀
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 2 档案 + 运行时信息 + 事实：放在缓存断点之后。
    #   "今天是几号"每天变，写进系统提示会打断缓存前缀。
    parts = [profile_block, f"今天是 {today}。"]
    if facts_block:
        parts.append(facts_block)
    messages.append({"role": "system", "content": "\n\n".join(parts)})

    # 3 压缩后的历史
    compressed = compress_history(history, KEEP_RECENT_TURNS, MAX_HISTORY_TOKENS)
    messages.extend(compressed)

    # 4 当前用户消息：永远最后
    messages.append({"role": "user", "content": user_text})

    budget = ContextBudget(
        system=estimate_tokens(SYSTEM_PROMPT),
        profile=estimate_tokens(profile_block),
        facts=estimate_tokens(facts_block),
        history=sum(estimate_tokens(str(m.get("content", ""))) for m in compressed),
        total=sum(estimate_tokens(str(m.get("content", ""))) for m in messages),
    )
    return messages, budget
