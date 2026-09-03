"""碳循环计划表的解析。

真实文件是脏的，这组测试大部分在守具体的脏法。每一条断言背后都是一种"不会报错
但会把数据悄悄弄错"的情况——那比解析失败危险得多：失败用户会重试，错位他不会
知道，然后照着错的吃四周。
"""

import io

import pytest
from openpyxl import Workbook

from app.core.domain.plan_import import (
    PlanParseError,
    _row_field,
    parse_carb_cycle_workbook,
)

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _book(sheets: dict[str, list[list]]) -> bytes:
    """按 {表名: 行列表} 造一个工作簿。"""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _week_rows(
    *, header: tuple = WEEKDAYS, day_types=("中", "低", "高", "低", "中", "中", "高"),
    focus=("胸", "背", "腿", "休息", "胸", "背", "腿"),
    carbs=(140, 90, 301, 90, 140, 140, 301),
) -> list[list]:
    return [
        ["第1周", *header],
        ["碳日", *day_types],
        ["训练部位", *focus],
        ["碳水(g)", *carbs],
        ["蛋白质(g)", *([112] * 7)],
        ["脂肪(g)", *([56] * 7)],
        ["热量(kcal)", *([1512] * 7)],
        ["缺口/盈余(kcal)", *([-903] * 7)],
    ]


class TestRowLabelPriority:
    """行标签匹配必须按特异性排序，第一个命中即返回。

    这是实测踩出来的：样例表的 "缺口/盈余(kcal)" 同时含"缺口"和"kcal"。
    kcal 规则排在前面时，缺口那一行被认成热量、把真正的热量值覆盖掉，
    结果 kcal 和 balance_kcal 双双为 None——两个字段一起消失，而且不报错。
    """

    def test_balance_row_is_not_mistaken_for_calories(self):
        assert _row_field("缺口/盈余(kcal)") == "balance_kcal"
        assert _row_field("热量(kcal)") == "kcal"

    @pytest.mark.parametrize(("label", "expected"), [
        ("碳日", "day_type"),
        ("训练部位", "focus"),
        ("碳水(g)", "carb_g"),
        ("碳水化合物 (克)", "carb_g"),
        ("蛋白质(g)", "protein_g"),
        ("脂肪(g)", "fat_g"),
        ("热量(kcal)", "kcal"),
        ("卡路里", "kcal"),
        ("热量缺口", "balance_kcal"),
        ("每日赤字(kcal)", "balance_kcal"),
        ("无关的一行", None),
    ])
    def test_label_mapping(self, label, expected):
        assert _row_field(label) == expected

    def test_both_energy_fields_survive_together(self):
        """回归：两个字段要能同时解析出来。"""
        plan = parse_carb_cycle_workbook(_book({"第1周": _week_rows()}))
        day = plan.weeks[0].days[0]
        assert day.kcal == 1512.0
        assert day.balance_kcal == -903.0


class TestNegativeBalance:
    """缺口是热量差，减脂计划里本来就该是负数。

    统一按 0 起判上下界时，样例文件里全部 28 天的缺口都被当成越界丢掉了。
    """

    def test_negative_deficit_is_kept(self):
        plan = parse_carb_cycle_workbook(_book({"第1周": _week_rows()}))
        assert all(d.balance_kcal == -903.0 for d in plan.weeks[0].days)
        assert plan.warnings == []

    def test_absurd_values_are_dropped_with_warning(self):
        """一份写着每日 30000 kcal 的计划进了库，后面每次"今天该吃多少"
        都会答错。丢掉并告警，而不是原样导入。"""
        rows = _week_rows()
        rows[3] = ["碳水(g)", 99999, 90, 301, 90, 140, 140, 301]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].carb_g is None
        assert any("超出合理范围" in w for w in plan.warnings)

    def test_negative_macros_are_rejected(self):
        """碳水可以是 0，不能是负数。"""
        rows = _week_rows()
        rows[3] = ["碳水(g)", -50, 90, 301, 90, 140, 140, 301]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].carb_g is None


class TestColumnAlignment:
    """列位置由表头的星期字符决定，不靠序号。

    样例文件第 1 周有 9 列而其余三周是 8 列，多出来的那列夹在"一"和"二"之间，
    像是有人在旁边试算了一版没删掉。按序号读会整周错位一天且不报错——数字都在
    合理范围内，导入完看起来一切正常。
    """

    def test_stray_column_does_not_shift_days(self):
        rows = [
            ["第1周", "一", None, "二", "三", "四", "五", "六", "日"],
            ["碳日", "中", None, "低", "高", "低", "中", "中", "高"],
            ["训练部位", "胸+三头+腹", None, "背+二头", "腿+肩", "休息", "胸", "背", "腿"],
            ["碳水(g)", 140, 138.4, 90, 301, 90, 140, 140, 301],
            ["蛋白质(g)", 112, 111.2, 112, 112, 112, 112, 112, 112],
        ]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        days = plan.weeks[0].days

        assert len(days) == 7, "多出来的那列被当成了一天"
        assert [d.carb_g for d in days] == [140, 90, 301, 90, 140, 140, 301]
        assert days[0].focus == "胸+三头+腹"
        assert days[1].focus == "背+二头", "周二拿到了试算列的数据，整周错位了"

    @pytest.mark.parametrize("header", [
        ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
        ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"),
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        ("一", "二", "三", "四", "五", "六", "天"),
    ])
    def test_weekday_aliases(self, header):
        plan = parse_carb_cycle_workbook(_book({"第1周": _week_rows(header=header)}))
        assert len(plan.weeks[0].days) == 7

    def test_duplicate_weekday_keeps_first(self):
        """重复列通常是右边那份草稿。"""
        rows = [
            ["第1周", "一", "一", "二", "三", "四", "五", "六", "日"],
            ["碳水(g)", 140, 999, 90, 301, 90, 140, 140, 301],
        ]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].carb_g == 140

    def test_partial_week_is_accepted(self):
        """只写了 5 天的计划也要能导入——不是每个人都排满 7 天。"""
        rows = [
            ["第1周", "一", "二", "三", "四", "五"],
            ["碳日", "中", "低", "高", "低", "中"],
            ["碳水(g)", 140, 90, 301, 90, 140],
        ]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert [d.day for d in plan.weeks[0].days] == [1, 2, 3, 4, 5]


class TestValueParsing:
    @pytest.mark.parametrize(("raw", "expected"), [
        (140, 140.0),
        (140.5, 140.5),
        ("140", 140.0),
        ("140g", 140.0),
        ("约 140 克", 140.0),
        ("1500-1600", 1500.0),
        ("1,512", 1512.0),
        ("", None),
        ("-", None),
        ("待定", None),
        (None, None),
    ])
    def test_numbers_tolerate_units_and_noise(self, raw, expected):
        rows = _week_rows()
        rows[3] = ["碳水(g)", raw, 90, 301, 90, 140, 140, 301]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].carb_g == expected

    @pytest.mark.parametrize(("raw", "expected"), [
        ("高", "high"), ("高碳", "high"), ("HIGH", "high"),
        ("中", "medium"), ("中碳", "medium"),
        ("低", "low"), ("低碳", "low"),
        ("休息", "rest"),
    ])
    def test_day_type_normalised(self, raw, expected):
        """归一到英文是为了和 plan_cut_phase 生成的 payload 对齐，
        否则同一张卡片要处理两套取值。"""
        rows = _week_rows(day_types=(raw, "低", "高", "低", "中", "中", "高"))
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].day_type == expected

    def test_unknown_day_type_is_kept_as_is(self):
        """认不出的写法原样保留：丢掉等于把用户填的信息静默删除。"""
        rows = _week_rows(day_types=("超高碳", "低", "高", "低", "中", "中", "高"))
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].day_type == "超高碳"

    def test_missing_day_type_defaults_to_medium(self):
        rows = _week_rows(day_types=("", "低", "高", "低", "中", "中", "高"))
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.weeks[0].days[0].day_type == "medium"


class TestBasicsSheet:
    BASICS = [
        ["基础信息", None],
        ["当前体重(kg)", 91],
        ["目标体重(kg)", 86],
        ["身高(cm)", 180],
        ["年龄(岁)", 25],
        ["性别", "男"],
        ["基础代谢(kcal)", 1915],
        ["每日总消耗(kcal)", 2415],
    ]

    def test_profile_fields_extracted(self):
        plan = parse_carb_cycle_workbook(
            _book({"基础信息": self.BASICS, "第1周": _week_rows()}),
        )
        assert plan.profile == {
            "weight_kg": 91.0, "target_kg": 86.0,
            "height_cm": 180.0, "age": 25, "sex": "male",
        }

    def test_age_is_an_integer(self):
        """profile 白名单里 age 是整数，写成 25.0 会在档案里显示成"25.0 岁"。"""
        plan = parse_carb_cycle_workbook(
            _book({"基础信息": self.BASICS, "第1周": _week_rows()}),
        )
        assert isinstance(plan.profile["age"], int)

    def test_unmapped_rows_are_kept_in_basics(self):
        """基础代谢、每日总消耗这些进不了 profile 白名单，但对用户有意义，
        原样留在 basics 里。"""
        plan = parse_carb_cycle_workbook(
            _book({"基础信息": self.BASICS, "第1周": _week_rows()}),
        )
        assert plan.basics["每日总消耗(kcal)"] == 2415

    @pytest.mark.parametrize(("raw", "expected"), [
        ("男", "male"), ("male", "male"), ("女", "female"), ("female", "female"),
    ])
    def test_sex_normalised(self, raw, expected):
        basics = [*self.BASICS[:5], ["性别", raw]]
        plan = parse_carb_cycle_workbook(
            _book({"基础信息": basics, "第1周": _week_rows()}),
        )
        assert plan.profile["sex"] == expected

    def test_unknown_sex_is_omitted_not_guessed(self):
        basics = [*self.BASICS[:5], ["性别", "其他"]]
        plan = parse_carb_cycle_workbook(
            _book({"基础信息": basics, "第1周": _week_rows()}),
        )
        assert "sex" not in plan.profile

    def test_works_without_basics_sheet(self):
        plan = parse_carb_cycle_workbook(_book({"第1周": _week_rows()}))
        assert plan.profile == {}
        assert plan.day_count == 7


class TestWeekNumbering:
    def test_week_number_from_sheet_title(self):
        book = _book({
            "第1周": _week_rows(), "第2周": _week_rows(), "第3周": _week_rows(),
        })
        plan = parse_carb_cycle_workbook(book)
        assert [w.week for w in plan.weeks] == [1, 2, 3]

    def test_duplicate_week_numbers_are_renumbered_with_warning(self):
        """静默改号会让用户对不上自己的原表。"""
        plan = parse_carb_cycle_workbook(
            _book({"第1周": _week_rows(), "第1周 副本": _week_rows()}),
        )
        assert [w.week for w in plan.weeks] == [1, 2]
        assert any("重复" in w for w in plan.warnings)

    def test_untitled_sheets_fall_back_to_position(self):
        plan = parse_carb_cycle_workbook(
            _book({"Sheet A": _week_rows(), "Sheet B": _week_rows()}),
        )
        assert [w.week for w in plan.weeks] == [1, 2]


class TestFailureModes:
    """失败必须说清是哪里的问题——这些消息直接给用户看。"""

    def test_not_an_xlsx(self):
        with pytest.raises(PlanParseError, match="xlsx"):
            parse_carb_cycle_workbook(b"this is not a spreadsheet")

    def test_empty_bytes(self):
        with pytest.raises(PlanParseError):
            parse_carb_cycle_workbook(b"")

    def test_no_recognisable_week_sheet(self):
        book = _book({"随便": [["姓名", "张三"], ["备注", "无"]]})
        with pytest.raises(PlanParseError, match="周计划"):
            parse_carb_cycle_workbook(book)

    def test_sheet_without_weekday_header_is_skipped_with_warning(self):
        book = _book({
            "第1周": _week_rows(),
            "备注": [["说明", "记得多喝水"], ["联系", "教练微信"]],
        })
        plan = parse_carb_cycle_workbook(book)
        assert len(plan.weeks) == 1
        assert any("没有识别到星期表头" in w for w in plan.warnings)

    def test_only_basics_sheet_is_an_error(self):
        """只有基础信息、没有任何周计划，不能算导入成功。"""
        book = _book({"基础信息": [["当前体重(kg)", 91]]})
        with pytest.raises(PlanParseError, match="周计划"):
            parse_carb_cycle_workbook(book)


class TestPayloadShape:
    """payload 要和 plan_cut_phase 生成的形状兼容，这样同一个卡片组件和
    同一个查询工具能同时处理"助理生成的"和"用户导入的"计划。"""

    def test_payload_matches_generated_plan_shape(self):
        plan = parse_carb_cycle_workbook(_book({"第1周": _week_rows()}))
        payload = plan.to_payload()

        assert payload["source"] == "import"
        assert payload["carb_cycle"] is True
        assert isinstance(payload["weeks"], list)
        day = payload["weeks"][0]["days"][0]
        assert day["day"] == 1
        assert day["day_type"] == "medium"

    def test_flat_plan_is_not_marked_as_carb_cycling(self):
        rows = _week_rows(day_types=("中",) * 7)
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        assert plan.to_payload()["carb_cycle"] is False

    def test_absent_fields_are_omitted_not_null(self):
        """"这一天没写脂肪"和"这一天脂肪是 0"是两件事。留 None 进 JSONB
        会让前端逐个判空。"""
        rows = [
            ["第1周", *WEEKDAYS],
            ["碳日", "中", "低", "高", "低", "中", "中", "高"],
            ["碳水(g)", 140, 90, 301, 90, 140, 140, 301],
        ]
        plan = parse_carb_cycle_workbook(_book({"第1周": rows}))
        day = plan.to_payload()["weeks"][0]["days"][0]
        assert "carb_g" in day
        assert "fat_g" not in day
        assert "focus" not in day

    def test_day_count_counts_every_day(self):
        book = _book({"第1周": _week_rows(), "第2周": _week_rows()})
        assert parse_carb_cycle_workbook(book).day_count == 14


class TestRealWorldFile:
    """用真实文件跑一遍。样例是网上流传的碳循环模板，脏在第 1 周多一列试算数据。

    文件不在仓库里（属于用户的个人素材），拿不到就跳过——但只要它在，
    就必须逐格对得上，不能"大致正确"。
    """

    PATH = "/Users/haoyifan/Downloads/凯圣王碳循环计划_91kg.xlsx"

    @pytest.fixture
    def real_bytes(self) -> bytes:
        from pathlib import Path

        path = Path(self.PATH)
        if not path.exists():
            pytest.skip("真实样例文件不在本机")
        return path.read_bytes()

    def test_parses_without_warnings(self, real_bytes):
        plan = parse_carb_cycle_workbook(real_bytes)
        assert len(plan.weeks) == 4
        assert plan.day_count == 28
        assert plan.warnings == [], f"真实文件解析出告警：{plan.warnings}"

    def test_first_week_matches_source_exactly(self, real_bytes):
        """第 1 周是有多余列的那一周，逐格核对。"""
        days = parse_carb_cycle_workbook(real_bytes).weeks[0].days
        assert [d.day_type for d in days] == [
            "medium", "low", "high", "low", "medium", "medium", "high",
        ]
        assert [d.carb_g for d in days] == [140, 90, 301, 90, 140, 140, 301]
        assert [d.kcal for d in days] == [1512, 1897, 1976, 1897, 1512, 1512, 1976]
        assert [d.balance_kcal for d in days] == [-903, -518, -439, -518, -903, -903, -439]
        assert days[0].focus == "胸+三头+腹"
        assert days[3].focus == "休息"

    def test_profile_extracted_from_real_file(self, real_bytes):
        plan = parse_carb_cycle_workbook(real_bytes)
        assert plan.profile == {
            "weight_kg": 91.0, "target_kg": 86.0,
            "height_cm": 180.0, "age": 25, "sex": "male",
        }
