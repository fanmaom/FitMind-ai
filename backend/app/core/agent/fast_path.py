"""结构化输入快路径。

「卧推 80kg 5x5」这类高频强结构输入直接正则解析入库，一次模型调用都不用，
延迟从两秒降到几十毫秒。

只处理高置信度输入；任何歧义一律返回 None 交给模型。
宁可漏判（多花一次模型调用），不可误判（把目标当成记录写进数据库）。
"""

import re
from dataclasses import dataclass

# 训练：[今天] 动作 [重量kg|自重] 组数x次数
WORKOUT_RE = re.compile(
    r"^(?:今天\s*)?(?P<exercise>[一-鿿 A-Za-z]{2,12}?)\s*"
    r"(?:(?P<weight>\d+(?:\.\d+)?)\s*(?:kg|KG|Kg|公斤)|(?P<bodyweight>自重))?\s*"
    r"(?P<sets>\d{1,2})\s*(?:x|X|×|\*|组)\s*(?P<reps>\d{1,3})\s*(?:次|reps|rep)?\s*$",
)

# 体重：必须出现"体重"或"称"，避免把"减到75公斤"这种目标误判为记录
BODYWEIGHT_RE = re.compile(
    r"(?:体重|称[了一]?下?)\s*[:：]?\s*(?P<kg>\d{2,3}(?:\.\d+)?)\s*(?:kg|KG|Kg|公斤)?",
)

# 出现这些词说明在说目标或计划，不是当下的记录
GOAL_HINTS = ("想", "要", "目标", "打算", "希望", "减到", "增到", "计划", "准备", "争取")


@dataclass
class FastPathHit:
    tool_name: str
    arguments: dict


def try_fast_path(text: str, today: str) -> FastPathHit | None:
    stripped = text.strip()

    # 目标类表述一律交给模型，绝不当成记录写库。
    #
    # 这个检查必须在所有模式匹配之前。放在后面是不够的——训练正则会先匹配
    # 成功并直接返回，"希望深蹲能到 140kg 5x5" 会被解析成动作名"希望深蹲能到"
    # 的一次真实训练写进数据库。误判比漏判危险得多：漏判只是多花一次模型
    # 调用，误判是往用户的训练史里塞了一条他没做过的记录。
    if any(hint in stripped for hint in GOAL_HINTS):
        return None

    m = WORKOUT_RE.match(stripped)
    if m:
        exercise = m.group("exercise").strip()
        weight = float(m.group("weight")) if m.group("weight") else 0.0
        sets_n, reps = int(m.group("sets")), int(m.group("reps"))
        if exercise and sets_n >= 1 and reps >= 1:
            return FastPathHit(
                tool_name="log_workout",
                arguments={
                    "date": today,
                    "exercise": exercise,
                    "sets": [{"weight": weight, "reps": reps} for _ in range(sets_n)],
                },
            )

    m = BODYWEIGHT_RE.search(stripped)
    if m:
        return FastPathHit(
            tool_name="log_body_metric",
            arguments={"date": today, "weight_kg": float(m.group("kg"))},
        )

    return None
