"""目标冲突检测。纯函数，零依赖。

减脂要热量赤字，增力要盈余——两者同时推进在生理上基本不可行。
本模块把矛盾显式化并给出可执行路径，让助理能"顶用户一句"，
而不是什么都答应下来再给一份做不到的计划。
"""

from dataclasses import dataclass, field

from app.core.domain.macros import Goal

PHASE_LABEL = {"cut": "减脂期", "bulk": "增肌期", "maintain": "维持期"}


@dataclass(frozen=True)
class Option:
    key: str
    title: str
    summary: str
    # 用户点这个选项时，代替他说出的那句话。
    #
    # 没有这个字段时前端只能把 title 发出去，而 title 是给人看的短标签
    # （"先跑完当前周期"），模型收到它得自己猜要做什么——可能只是复述一遍，
    # 也可能问"你是想…吗"，用户点了按钮却换来一个反问。
    # 这里写成完整的第一人称指令，让模型有明确的动作可执行。
    prompt: str = ""

    def as_dict(self) -> dict:
        # prompt 缺失时退回 title，保证前端永远有话可发。
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "prompt": self.prompt or self.title,
        }


@dataclass(frozen=True)
class ConflictReport:
    has_conflict: bool
    reason: str = ""
    options: list[Option] = field(default_factory=list)


_CUT_VS_STRENGTH_OPTIONS = [
    Option(
        key="cut_then_bulk",
        title="先减后增",
        summary="先用 8 周减到目标体重，期间大重量动作按「保力」跑——强度维持在 85% 一区间"
                "保住神经适应，训练容量下调约 30%。减脂结束后转入增力期，再用 12-14 周"
                "推进目标重量。总时长更久，但两个目标都能到。",
        prompt="就按先减后增来。先帮我排减脂期的安排，把大重量动作按保力处理，"
               "并告诉我减脂结束大概什么时候可以转增力期。",
    ),
    Option(
        key="maintain_strength",
        title="保力减脂",
        summary="强度不降、训练容量砍半、蛋白提到 2.2g/kg。这样大概率能守住当前的力量水平，"
                "体重按每周 0.5kg 稳定下降。代价是本周期内力量不会有明显进步。",
        prompt="选保力减脂。帮我按强度不降、容量砍半、蛋白 2.2g/kg 重新算一下"
               "每天的热量和营养素，并说明这样每周大概减多少。",
    ),
]


def detect_conflict(goal: Goal, wants_strength_gain: bool, current_phase: str) -> ConflictReport:
    """检测目标之间、以及目标与当前周期之间的矛盾。

    两种冲突同时成立时先报生理上更硬的那个（赤字 vs 盈余），
    周期不匹配只是安排问题，改一下就行。
    """
    if goal == "cut" and wants_strength_gain:
        return ConflictReport(
            has_conflict=True,
            reason="减脂需要热量赤字，增力需要热量盈余，两者同时推进大概率两边都不成。",
            options=list(_CUT_VS_STRENGTH_OPTIONS),
        )

    # 转维持期在任何阶段都是合理选择，不算冲突
    if goal != "maintain" and current_phase in PHASE_LABEL and goal != current_phase:
        return ConflictReport(
            has_conflict=True,
            reason=f"你当前处于{PHASE_LABEL[current_phase]}，而请求的是"
                   f"{PHASE_LABEL.get(goal, goal)}方案。切换周期会打断当前进度。",
            options=[
                Option(
                    key="finish_current",
                    title="先跑完当前周期",
                    summary=f"把当前的{PHASE_LABEL[current_phase]}走完再切换，"
                            f"避免半途转向导致两个周期都不完整。我可以先给你看当前周期还剩多久、"
                            f"以及照现在的进度能到什么位置。",
                    prompt=f"我先把当前的{PHASE_LABEL[current_phase]}跑完。"
                           f"帮我看一下这个周期还剩多久、照现在的进度能到什么位置，"
                           f"然后告诉我什么时候适合转{PHASE_LABEL.get(goal, goal)}。",
                ),
                Option(
                    key="switch_now",
                    title="立刻切换",
                    summary=f"现在就转入{PHASE_LABEL.get(goal, goal)}。我会重新计算热量与营养素，"
                            f"并把当前周期标记为提前结束，之前的训练记录都保留。",
                    prompt=f"现在就切到{PHASE_LABEL.get(goal, goal)}。"
                           f"请把我的目标改成{goal}，重新计算每天的热量和营养素，"
                           f"当前周期按提前结束处理。",
                ),
            ],
        )

    return ConflictReport(has_conflict=False)
