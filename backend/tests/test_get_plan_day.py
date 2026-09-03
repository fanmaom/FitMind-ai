"""查询计划当日安排。

导入计划的价值不在存档，而在助理能照着它回答"今天吃多少、练什么"。没有这个
工具，导入的 28 天数据就只是一条谁也不会去看的 JSONB。
"""

import uuid
from datetime import date

import pytest

from app.core.tools.get_plan_day import GetPlanDayInput, get_plan_day
from app.core.tools.get_plan_detail import GetPlanDetailInput, get_plan_detail
from app.core.tools.registry import ToolContext
from app.models.plan import Plan


def _payload(weeks: int = 2, source: str = "import") -> dict:
    day_types = ("medium", "low", "high", "low", "medium", "medium", "high")
    focus = ("胸+三头", "背+二头", "腿+肩", "休息", "胸", "背", "腿")
    carbs = (140, 90, 301, 90, 140, 140, 301)
    return {
        "source": source,
        "carb_cycle": True,
        "weeks": [
            {
                "week": week,
                "days": [
                    {
                        "day": index + 1,
                        "day_type": day_types[index],
                        "focus": focus[index],
                        "carb_g": carbs[index],
                        "protein_g": 112,
                        "fat_g": 56,
                        "kcal": 1512,
                        "balance_kcal": -903,
                    }
                    for index in range(7)
                ],
            }
            for week in range(1, weeks + 1)
        ],
    }


@pytest.fixture
async def ctx(db, seeded_user) -> ToolContext:
    return ToolContext(user_id=seeded_user, session=db)


@pytest.fixture
async def plan(db, seeded_user) -> Plan:
    row = Plan(user_id=seeded_user, type="cut", payload=_payload(), status="active")
    db.add(row)
    await db.commit()
    return row


class TestGetPlanDay:
    @pytest.mark.asyncio
    async def test_returns_today_by_default(self, ctx, plan):
        """用户问「今天吃多少」时不该还要自己数今天是周几。"""
        result = await get_plan_day(GetPlanDayInput(), ctx)
        assert result["found"] is True
        assert result["weekday"] == date.today().isoweekday()

    @pytest.mark.asyncio
    async def test_explicit_weekday(self, ctx, plan):
        result = await get_plan_day(GetPlanDayInput(weekday=3), ctx)
        assert result["day_type"] == "high"
        assert result["carb_g"] == 301
        assert result["focus"] == "腿+肩"

    @pytest.mark.asyncio
    async def test_explicit_week_and_weekday(self, ctx, plan):
        result = await get_plan_day(GetPlanDayInput(week=2, weekday=4), ctx)
        assert result["week"] == 2
        assert result["day_type"] == "low"
        assert result["focus"] == "休息"

    @pytest.mark.asyncio
    async def test_summary_is_human_readable(self, ctx, plan):
        """给一句现成的摘要能显著降低模型把数字说反的概率——碳水和脂肪在
        低碳日反向变化，最容易搞错。"""
        result = await get_plan_day(GetPlanDayInput(week=1, weekday=2), ctx)
        summary = result["summary"]
        assert "第1周周二" in summary
        assert "低碳日" in summary
        assert "碳水 90g" in summary
        assert "脂肪 56g" in summary

    @pytest.mark.asyncio
    async def test_reports_source(self, ctx, plan):
        """用户导入的和助理生成的要能分辨——回答时说法不同。"""
        result = await get_plan_day(GetPlanDayInput(weekday=1), ctx)
        assert result["source"] == "import"

    @pytest.mark.asyncio
    async def test_no_plan_suggests_next_step(self, ctx):
        """空结果要给出路，而不是只说"没有"。"""
        result = await get_plan_day(GetPlanDayInput(), ctx)
        assert result["found"] is False
        assert "上传" in result["note"] or "排" in result["note"]

    @pytest.mark.asyncio
    async def test_week_out_of_range_states_the_range(self, ctx, plan):
        result = await get_plan_day(GetPlanDayInput(week=9, weekday=1), ctx)
        assert result["found"] is False
        assert "第 1–2 周" in result["note"]

    @pytest.mark.asyncio
    async def test_missing_weekday_in_partial_week(self, db, seeded_user, ctx):
        """只排了 5 天的计划，问周六要说清没有而不是报错。"""
        payload = _payload(weeks=1)
        payload["weeks"][0]["days"] = payload["weeks"][0]["days"][:5]
        db.add(Plan(user_id=seeded_user, type="cut", payload=payload, status="active"))
        await db.commit()

        result = await get_plan_day(GetPlanDayInput(weekday=6), ctx)
        assert result["found"] is False
        assert "周六" in result["note"]

    @pytest.mark.asyncio
    async def test_ignores_archived_plans(self, db, seeded_user, ctx):
        db.add(Plan(
            user_id=seeded_user, type="cut", payload=_payload(), status="archived",
        ))
        await db.commit()
        result = await get_plan_day(GetPlanDayInput(), ctx)
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_prefers_latest_plan(self, db, seeded_user, ctx):
        """用户手上通常只有一份在跑；刚导入的那份就是"当前那份"。"""
        old = Plan(user_id=seeded_user, type="cut", payload=_payload(source="generated"))
        db.add(old)
        await db.commit()
        new = Plan(user_id=seeded_user, type="cut", payload=_payload(source="import"))
        db.add(new)
        await db.commit()

        result = await get_plan_day(GetPlanDayInput(weekday=1), ctx)
        assert result["plan_id"] == str(new.id)

    @pytest.mark.asyncio
    async def test_other_users_plan_is_invisible(self, db, seeded_user, plan):
        stranger = ToolContext(user_id=uuid.uuid4(), session=db)
        result = await get_plan_day(GetPlanDayInput(), stranger)
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_is_readonly(self):
        """查询工具被标成 readonly=False 会让它在 L3 降级时被裁掉，
        而这个工具恰恰是降级时最该保留的——它不依赖模型算数。"""
        from app.core.tools.registry import load_tools, registry

        load_tools()
        assert registry.get("get_plan_day").readonly is True


class TestGetPlanDetailAcceptsAllTypes:
    """get_plan_detail 原来硬编码 type=="strength"，于是减脂计划拿着正确的
    plan_id 也查不到，只会得到一句"计划不存在，或你没有权限"——一句会把人
    引向错误方向的提示（用户会以为是权限问题）。

    限定 user_id 就够了，那才是这里真正要防的东西。
    """

    @pytest.mark.asyncio
    async def test_cut_plan_is_readable(self, ctx, plan):
        result = await get_plan_detail(
            GetPlanDetailInput(plan_id=plan.id, week=1), ctx,
        )
        assert result["found"] is True
        assert result["plan_type"] == "cut"
        assert len(result["days"]) == 7

    @pytest.mark.asyncio
    async def test_strength_plan_still_readable(self, db, seeded_user, ctx):
        row = Plan(
            user_id=seeded_user, type="strength",
            payload={"scheme": "5x5", "weeks": [{"week": 1, "lifts": {}}]},
        )
        db.add(row)
        await db.commit()

        result = await get_plan_detail(GetPlanDetailInput(plan_id=row.id, week=1), ctx)
        assert result["found"] is True
        assert result["scheme"] == "5x5"

    @pytest.mark.asyncio
    async def test_other_users_plan_still_denied(self, db, plan):
        stranger = ToolContext(user_id=uuid.uuid4(), session=db)
        result = await get_plan_detail(
            GetPlanDetailInput(plan_id=plan.id, week=1), stranger,
        )
        assert result["found"] is False
