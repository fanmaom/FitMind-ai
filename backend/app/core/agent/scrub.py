"""流式输出脱敏：把内部标识符换成人话。

系统提示里已经写了"不许提工具名"，但提示只是要求，模型不保证遵守——线上真实
样本里就有 "用 `plan_strength_cycle` 或下肢保护性安排"。"绝不泄漏内部实现"是
产品承诺，得有一层确定性代码来兑现，这就是这一层。

难点全在流式：`log_workout` 会被切成任意几段吐出来，逐段替换必然漏。做法是
**先按边界扣住可能没写完的尾巴，再替换已经确定的部分**——顺序反了就会把
`log_workout_v2` 的前半截当成 `log_workout` 换掉。
"""

import re

from app.core.agent.glossary import ENUM_LABELS, PAYLOAD_LABELS, field_label
from app.core.logger import logger
from app.core.memory.profile import PROFILE_FIELDS
from app.core.tools.registry import registry

# 标识符字符 + 反引号。反引号必须一起扣住：先把它发出去，等标识符到了才发现
# 要替换，那个孤零零的 ` 已经收不回来了（前端是纯文本渲染，会原样显示）。
_TAIL = re.compile(r"[A-Za-z0-9_`]*$")

# 词表外的 snake_case。不猜着替换——替错比露出来更糟——但要记日志，
# 靠日志把词表补全。
_UNKNOWN = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")


def build_vocab() -> dict[str, str]:
    """内部标识符 → 中文替换文本。

    工具名从注册表现取，不另立一张表：新增工具的成本仍然是"新增一个文件"，
    而不是"新增一个文件并记得改脱敏词表"——后者迟早会漏。

    枚举值（cut / office / male）也进词表。它们没法只靠输入侧堵住：工具的
    JSON Schema 必须把值域交给模型，模型才填得对参数，所以它一定认识这些词。
    匹配是大小写敏感的——枚举值一律小写，英文行文里的 Office / Cut 不会被碰到。
    """
    vocab = {spec.name: spec.label for spec in registry.all()}
    for key in PROFILE_FIELDS:
        vocab.setdefault(key, field_label(key))
    for table in (ENUM_LABELS, PAYLOAD_LABELS):
        for key, label in table.items():
            vocab.setdefault(key, label)
    return vocab


class TextScrubber:
    """一个回合一个实例——扣住的尾巴是这一条回复的状态。"""

    def __init__(self, vocab: dict[str, str] | None = None) -> None:
        self._vocab = build_vocab() if vocab is None else vocab
        self._pending = ""
        self._reported: set[str] = set()
        # 长的排前面：交替匹配取第一个成功的分支，短键在前会切出半截。
        keys = sorted(self._vocab, key=len, reverse=True)
        # 前后都要显式排除标识符字符：中文在 Unicode 下也算 \w，用 \b 判边界
        # 时 "用log_workout" 之间没有边界，整条正则会失效。
        alternation = "|".join(re.escape(k) for k in keys)
        self._pattern = re.compile(
            rf"`?(?<![A-Za-z0-9_])({alternation})(?![A-Za-z0-9_])`?",
        ) if keys else None

    def feed(self, delta: str) -> str:
        """吃进一段增量，返回可以立即发给用户的部分。"""
        if not delta:
            return ""
        self._pending += delta
        cut = _TAIL.search(self._pending).start()
        safe, self._pending = self._pending[:cut], self._pending[cut:]
        return self._scrub(safe)

    def flush(self) -> str:
        """回复结束时把扣住的尾巴放出来。不调就等于吞掉一段话。"""
        tail, self._pending = self._pending, ""
        if self._is_half_written(tail):
            # 流在标识符中间断了——最常见是用户点了停止。这半截替不掉（词表里
            # 没有），原样发出去就是泄漏，所以丢掉：丢的是几个字符的内部名字，
            # 而这个回合本来就是被截断的。
            logger.info(f"回合在标识符中间结束，丢弃半截标识符：{tail}")
            return ""
        return self._scrub(tail)

    def _is_half_written(self, tail: str) -> bool:
        candidate = tail.lstrip("`")
        # 下划线是判据：真写完的英文词（carb、cook）不能因为"是某个键的前缀"
        # 就被丢掉，那会吞掉用户该看的字。
        if "_" not in candidate or candidate in self._vocab:
            return False
        return any(key.startswith(candidate) for key in self._vocab)

    def _scrub(self, text: str) -> str:
        if not text:
            return ""
        if self._pattern is not None:
            text = self._pattern.sub(lambda m: f"「{self._vocab[m.group(1)]}」", text)
        for found in _UNKNOWN.findall(text):
            if found not in self._reported:
                self._reported.add(found)
                logger.warning(f"回复里出现词表外的内部标识符，未脱敏：{found}")
        return text
