"""冲突卡片的选项必须可点、可执行。

线上表现：用户点「先跑完当前周期」，页面毫无变化。三个问题叠加：

1. 点击只是 dispatch 一个全局事件，由 ChatComposer 把文字塞进输入框——用户的
   注意力在卡片上，根本不会注意到底部输入框多了几个字。交互上等同于没反应。
2. 发出去的是 title（"先跑完当前周期"），是给人看的短标签。模型收到它得自己
   猜要做什么，可能只是复述一遍，也可能反问"你是想…吗"——用户点了按钮却换来
   一个反问。
3. 生成中按钮不禁用、无任何反馈，用户会连点，每一下都排队发出一轮对话。
"""

import inspect
from pathlib import Path

import pytest

from app.core.domain.conflict import Option, detect_conflict

WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(relative: str) -> str:
    return (WEB_SRC / relative).read_text()


class TestOptionCarriesExecutablePrompt:
    """每个选项都要带一句完整的第一人称指令，而不是只有短标签。"""

    @pytest.mark.parametrize(("goal", "wants_strength", "phase"), [
        ("cut", True, "cut"),        # 减脂 + 增力：生理冲突
        ("bulk", False, "cut"),      # 减脂期要增肌方案：周期冲突
        ("cut", False, "bulk"),      # 增肌期要减脂方案
    ])
    def test_every_option_has_a_prompt(self, goal, wants_strength, phase):
        report = detect_conflict(goal, wants_strength, phase)
        assert report.has_conflict
        assert report.options
        for option in report.options:
            data = option.as_dict()
            assert data["prompt"], f"选项 {data['key']} 没有可执行指令"
            # 指令要比标签长得多——短标签模型没法照着做。
            assert len(data["prompt"]) > len(data["title"]) + 10, (
                f"{data['key']} 的 prompt 太短，和标题没有实质区别"
            )

    def test_prompt_falls_back_to_title(self):
        """漏写 prompt 时退回 title，保证前端永远有话可发。
        少一个可空字段就少一处"点了没反应"的可能。"""
        bare = Option(key="k", title="立刻切换", summary="…")
        assert bare.as_dict()["prompt"] == "立刻切换"

    def test_switch_now_prompt_states_the_target_goal(self):
        """「立刻切换」必须说清切到哪个目标，否则模型不知道要把 goal 改成什么。"""
        report = detect_conflict("bulk", False, "cut")
        switch = next(o for o in report.options if o.key == "switch_now")
        assert "bulk" in switch.prompt or "增肌" in switch.prompt

    def test_finish_current_prompt_asks_for_progress(self):
        """「先跑完当前周期」的 summary 承诺了"先给你看还剩多久"，
        prompt 里必须真的要求这件事——否则承诺落空。"""
        report = detect_conflict("bulk", False, "cut")
        finish = next(o for o in report.options if o.key == "finish_current")
        assert "还剩" in finish.prompt or "进度" in finish.prompt


class TestToolExposesPrompt:
    def test_tool_serialises_prompt_field(self):
        """用 as_dict 而不是 __dict__：后者不会填充 prompt 的兜底值。

        只看代码行——注释里正好也提到了 __dict__，连注释一起搜会误报。
        """
        import app.core.tools.check_plan_conflict as tool_module

        code = [
            line for line in inspect.getsource(tool_module.check_plan_conflict).splitlines()
            if not line.lstrip().startswith("#")
        ]
        source = "\n".join(code)
        assert "as_dict()" in source
        assert "__dict__" not in source

    @pytest.mark.asyncio
    async def test_card_payload_includes_prompts(self, db, seeded_user):
        from app.core.memory.profile import merge_profile
        from app.core.tools.check_plan_conflict import (
            CheckPlanConflictInput,
            check_plan_conflict,
        )
        from app.core.tools.registry import ToolContext

        await merge_profile(db, seeded_user, {"goal": "cut"})
        ctx = ToolContext(user_id=seeded_user, session=db)
        result = await check_plan_conflict(
            CheckPlanConflictInput(requested_goal="bulk"), ctx,
        )

        assert result["has_conflict"] is True
        options = result["__card__"]["payload"]["options"]
        assert options
        for option in options:
            assert option["prompt"], f"卡片里的 {option['key']} 缺少可执行指令"


class TestFrontendSendsOnClick:
    """点击必须直接发送，而不是填进输入框。"""

    def test_card_calls_a_callback_not_a_global_event(self):
        """全局事件的两端互不知情——卡片不知道有没有人在听，坏了也没有任何
        报错。这正是这个 bug 能活到线上的原因。"""
        source = _read("components/cards/CardRenderer.tsx")
        assert "onPick" in source
        assert "dispatchEvent" not in source, "还在用全局事件派发，点击不会真的发送"
        assert "fitness-option" not in source

    def test_composer_no_longer_listens_for_the_dead_event(self):
        """监听方留着就是死代码，下一个人会以为这条链路还在工作。"""
        source = _read("components/chat/ChatComposer.tsx")
        assert "addEventListener" not in source

    def test_card_sends_prompt_not_title(self):
        source = _read("components/cards/CardRenderer.tsx")
        assert "o.prompt" in source, "发的还是标题，模型得自己猜要做什么"

    def test_callback_is_wired_all_the_way_down(self):
        """三层都要接上：ChatView 传给 ChatMessage，ChatMessage 传给
        CardRenderer。漏一层的表现就是"点了没反应"。"""
        assert "onPick={send}" in _read("components/chat/ChatView.tsx")
        assert "onPick={onPick}" in _read("components/chat/ChatMessage.tsx")

    def test_buttons_are_disabled_while_generating(self):
        """不禁用的话用户会连点，每一下都排队发出一轮对话。"""
        source = _read("components/cards/CardRenderer.tsx")
        assert "disabled={busy" in source
        assert "正在回复" in source, "禁用了但没说原因，用户会以为按钮坏了"

    def test_send_guards_against_reentry(self):
        """重入防护要在 send 自己身上。原来只有 ChatComposer 一个入口、它自己
        判了 sending；现在卡片也能直接调 send，漏判一处就会让两轮对话叠在一起
        ——store 只认"最后一条消息"，后到的 text_delta 会追加到前一轮的气泡上。"""
        source = _read("components/chat/ChatView.tsx")
        assert "if (sending) return;" in source
