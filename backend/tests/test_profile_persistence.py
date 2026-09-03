"""说了"记下了"就必须真的记下。

线上表现：用户说「我不爱香菜，所有的餐食都要避开这个」，助理回复"忌口记下了……
忌口已写入档案（香菜，全餐避开）"，但档案面板显示"未填写"，数据库里 profiles
表压根没有 dislikes 字段。

这是最糟的一类失败：**用户以为记住了**。下次他不会再说一遍，而助理早就忘了。

根因不在代码，在系统提示——它只写了"不要暴露工具名"，却从没写"用户说出偏好时
必须调用工具写档案"。模型于是在文字里宣称记住，一个工具都没调。

排查时还发现第二个问题：工具执行成功不写日志，于是"模型没调工具"和"调了但没
生效"在日志里长得一模一样——都是什么都没有。只能靠翻数据库反推。
"""

import inspect

import pytest

from app.core.agent.prompts import SYSTEM_PROMPT
from app.core.memory.profile import PROFILE_FIELDS, SENSITIVE_FIELDS


class TestPromptRequiresPersistence:
    def test_prompt_forbids_claiming_without_writing(self):
        """光说"记下了"不写库是欺骗用户，提示里必须点明。"""
        assert "记下了" in SYSTEM_PROMPT
        assert "必须真的记下" in SYSTEM_PROMPT or "必须在同一轮里调用" in SYSTEM_PROMPT

    @pytest.mark.parametrize("keyword", [
        "忌口", "伤病", "目标体重", "训练年限", "用餐场景",
    ])
    def test_prompt_lists_what_must_be_persisted(self, keyword):
        """逐项列出来，而不是笼统说"重要信息要记"。模型对"重要"的判断和用户
        不一致——它会认为"不爱香菜"只是聊天。"""
        assert keyword in SYSTEM_PROMPT, f"提示里没提到要记录{keyword}"

    def test_prompt_states_write_before_calculate(self):
        """跳过写入直接算，这一轮结果对了，但下一轮用户的偏好就丢了。"""
        assert "先把新信息写进档案" in SYSTEM_PROMPT

    def test_every_persistable_field_is_covered(self):
        """提示里提到的字段要能覆盖档案白名单里那些"用户会主动说出来"的。

        漏掉的字段就是下一个"说了记下了但没记"——所以这条把清单钉住，
        新增字段时会被提醒同步提示。
        """
        # 这些字段用户会在对话里自然提到，必须在提示里被点名
        user_stated = {
            "dislikes": ("忌口",),
            "injuries": ("伤病",),
            "target_kg": ("目标体重",),
            "goal": ("减脂", "增肌"),
            "height_cm": ("身高",),
            "weight_kg": ("体重",),
            "age": ("年龄",),
            "sex": ("性别",),
            "activity": ("活动量",),
            "training_years": ("训练年限",),
            "training_split": ("训练分化",),
            "equipment": ("器械",),
            "lifts": ("最大重量",),
            "meal_scenarios": ("用餐场景",),
        }
        missing = [
            field for field, hints in user_stated.items()
            if not any(hint in SYSTEM_PROMPT for hint in hints)
        ]
        assert not missing, f"这些档案字段没在提示里被点名，会重演同一个 bug：{missing}"

        unknown = set(user_stated) - set(PROFILE_FIELDS)
        assert not unknown, f"测试里写了白名单外的字段：{unknown}"

    def test_prompt_mentions_confirmation_flow(self):
        """敏感字段要先确认。提示里不说，模型会以为工具失败了然后放弃。"""
        assert "确认" in SYSTEM_PROMPT
        assert SENSITIVE_FIELDS, "敏感字段集合空了，确认流程失去意义"

    def test_prompt_stays_constant(self):
        """提示是缓存前缀，不能含易变内容——否则每轮字节都不同，缓存永远
        命中不了。"""
        import datetime

        year = str(datetime.date.today().year)
        assert year not in SYSTEM_PROMPT
        assert "今天" not in SYSTEM_PROMPT


class TestDislikesIsWritableWithoutConfirmation:
    """忌口不是敏感字段，用户一说就该直接落库。

    如果它进了 SENSITIVE_FIELDS，模型必须先问一轮"确认要记吗"——对"我不吃香菜"
    这种明确表述来说是多余的往返。
    """

    def test_dislikes_in_whitelist(self):
        assert "dislikes" in PROFILE_FIELDS

    def test_dislikes_not_sensitive(self):
        assert "dislikes" not in SENSITIVE_FIELDS

    @pytest.mark.asyncio
    async def test_writes_straight_through(self, db, seeded_user):
        from app.core.tools.registry import ToolContext
        from app.core.tools.update_profile import UpdateProfileInput, update_profile

        ctx = ToolContext(user_id=seeded_user, session=db)
        result = await update_profile(
            UpdateProfileInput(updates={"dislikes": ["香菜"]}), ctx,
        )
        assert result["written"] is True
        assert result.get("needs_confirmation") is not True
        assert result["profile"]["dislikes"] == ["香菜"]

    @pytest.mark.asyncio
    async def test_visible_through_api_immediately(self, db, seeded_user):
        """面板读的是这个接口。写完立刻可见，才不会出现"助理说记了、
        面板显示未填写"。"""
        from app.core.memory.profile import load_profile, merge_profile

        await merge_profile(db, seeded_user, {"dislikes": ["香菜", "内脏"]})
        assert (await load_profile(db, seeded_user))["dislikes"] == ["香菜", "内脏"]


class TestToolExecutionIsLogged:
    """成功也要记一行。

    只记失败时，"模型压根没调这个工具"和"调了但没生效"在日志里长得一模一样
    ——都是什么都没有。排查线上那次只能靠翻数据库反推。
    """

    def test_success_path_logs(self):
        from app.core.agent.loop import AgentLoop

        source = inspect.getsource(AgentLoop._execute_tool)
        # 找成功返回之前的那行日志
        assert "logger.info(f\"工具执行" in source, "工具执行成功没有任何日志"

    def test_log_carries_outcome_flags(self):
        """光记工具名不够——update_profile 返回 written=False 时也算"调过了"，
        必须能从日志看出它到底写没写。"""
        from app.core.agent.loop import AgentLoop

        source = inspect.getsource(AgentLoop._execute_tool)
        for flag in ("written", "needs_confirmation"):
            assert flag in source, f"日志没有带上 {flag}，看不出工具是否真的生效"

    def test_log_does_not_dump_arguments(self):
        """入参含用户的身体数据，日志不是存这些东西的地方。"""
        from app.core.agent.loop import AgentLoop

        code = [
            line for line in inspect.getsource(AgentLoop._execute_tool).splitlines()
            if "logger.info" in line
        ]
        assert code, "没找到成功路径的日志"
        assert not any("args" in line for line in code), "日志里打了完整入参"
