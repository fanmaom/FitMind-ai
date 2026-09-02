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
    summary: int
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


def _render_summary(summary: str) -> str:
    """渲染更早对话的摘要。

    明确标注它是"更早的对话"而非当前事实——否则模型会把摘要里那些已经过时的
    中间结论当成现状复述。摘要天生滞后，这个标注是必要的。
    """
    if not summary.strip():
        return ""
    return f"更早的对话摘要（比下面的原文更旧，如与原文冲突以原文为准）：\n{summary.strip()}"


def compress_history(history: list[dict], keep_recent: int, max_tokens: int) -> list[dict]:
    """三段式压缩：最近 keep_recent 轮原样保留，更早的在超预算时从最旧开始丢弃。

    历史对话只负责短期连贯性，长期信息由另外两套机制承担：稳定事实进 L2，
    被丢弃段落的上下文进会话摘要（core/memory/summary.py）。三者缺一个其余
    就跛脚——只压缩不记忆会失忆，只记忆不压缩会爆上下文，只丢不摘则会忘记
    "这个计划当初是按什么前提排的"（那既不是稳定事实，也不是训练日志，
    L2 和 L3 两条通道都不收）。
    """
    return _compress(history, keep_recent, max_tokens)[0]


def _compress(
    history: list[dict], keep_recent: int, max_tokens: int,
) -> tuple[list[dict], bool]:
    """返回 (压缩后的历史, 是否丢弃了内容)。

    第二个返回值决定要不要注入摘要：没丢东西时注入摘要纯属浪费 token，
    而且摘要与原文重复会让模型在两份说法之间摇摆。
    """
    if not history:
        return [], False
    if len(history) <= keep_recent:
        return list(history), False

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

    return kept + recent, len(kept) < len(older)


def build_context(
    *,
    profile: dict,
    facts: list[str],
    history: list[dict],
    user_text: str,
    today: str,
    summary: str = "",
    keep_recent: int = KEEP_RECENT_TURNS,
    max_history_tokens: int = MAX_HISTORY_TOKENS,
) -> tuple[list[dict], ContextBudget]:
    """组装本轮 messages。顺序即缓存策略，不要随意调整。

    keep_recent 默认取模块常量，但调用方应传 settings.agent_history_window
    ——那个配置项存在、有文档、也能从环境变量读进来，却一直没有被接到这里，
    改它没有任何效果。这种"看起来能调、实际调不动"的配置比没有更糟。

    summary 只在压缩确实丢弃了内容时才注入。没丢就不注入：那样纯属浪费
    token，而且摘要与还在上下文里的原文重复，会让模型在两份说法之间摇摆。
    """
    profile_block = _render_profile(profile)
    facts_block = _render_facts(facts)

    # 1 系统提示：恒定，作为缓存前缀
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 3 先压缩历史，因为要知道有没有真的丢东西才能决定注不注入摘要
    compressed, dropped = _compress(history, keep_recent, max_history_tokens)
    summary_block = _render_summary(summary) if dropped else ""

    # 2 档案 + 运行时信息 + 事实 + 摘要：放在缓存断点之后。
    #   "今天是几号"每天变，写进系统提示会打断缓存前缀。
    parts = [profile_block, f"今天是 {today}。"]
    if facts_block:
        parts.append(facts_block)
    if summary_block:
        parts.append(summary_block)
    messages.append({"role": "system", "content": "\n\n".join(parts)})

    messages.extend(compressed)

    # 4 当前用户消息：永远最后
    messages.append({"role": "user", "content": user_text})

    budget = ContextBudget(
        system=estimate_tokens(SYSTEM_PROMPT),
        profile=estimate_tokens(profile_block),
        facts=estimate_tokens(facts_block),
        summary=estimate_tokens(summary_block),
        history=sum(estimate_tokens(str(m.get("content", ""))) for m in compressed),
        total=sum(estimate_tokens(str(m.get("content", ""))) for m in messages),
    )
    return messages, budget
