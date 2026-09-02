"""术语表：内部标识符 → 用户能懂的说法。

同一份翻译要给五个地方用：系统提示的硬性规则、工具失败反馈、降级文案、
流式输出脱敏、前端工具状态行。分散定义必然对不齐，所以在这里定义一次。

字段中文名不另立一张表，而是从 ``PROFILE_FIELDS`` 的说明里截出来——那份说明
是写给模型看的（带单位和取值域），两张表并行早晚会漂移。
"""

import re

from app.core.memory.profile import PROFILE_FIELDS

# 枚举值。模型会把这些原样写进回复（"周中日间 office、其余 cook"），
# 所以它们和工具名一样属于内部标识符。
ENUM_LABELS: dict[str, str] = {
    # goal
    "cut": "减脂",
    "bulk": "增肌",
    "maintain": "维持",
    # sex
    "male": "男",
    "female": "女",
    # activity
    "sedentary": "久坐",
    "light": "轻度活动",
    "moderate": "中等活动",
    "active": "较高活动",
    "very_active": "高强度活动",
    # 用餐场景
    "cook": "自己做",
    "office": "带饭或公司简餐",
    "eatout": "外食",
    # 增力周期方案
    "linear": "线性递增",
    "wave": "波浪式",
}

# meal_scenarios 的键。模型可以自己编，认识的翻译、不认识的原样保留。
SLOT_LABELS: dict[str, str] = {
    "default": "默认",
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "weekday_breakfast": "工作日早餐",
    "weekday_lunch": "工作日午餐",
    "weekday_dinner": "工作日晚餐",
    "weekend_breakfast": "周末早餐",
    "weekend_lunch": "周末午餐",
    "weekend_dinner": "周末晚餐",
}

# 工具返回体和卡片载荷里的键。模型看得见，也就有可能抄进回复。
PAYLOAD_LABELS: dict[str, str] = {
    "plan_id": "计划编号",
    "protein_g": "蛋白质",
    "carb_g": "碳水",
    "fat_g": "脂肪",
    "deficit_kcal": "热量缺口",
    "body_fat_pct": "体脂率",
    "total_volume_kg": "总容量",
    "weekly_change_kg": "每周体重变化",
    "kcal_per_100g": "每 100g 热量",
}

_ANNOTATION = re.compile(r"（[^）]*）")


def prompt_label(key: str) -> str:
    """注入提示时用的字段名：保留单位，去掉取值域说明。

    单位得留着——"当前体重：82" 里的 82 是什么单位，模型只能猜；取值域必须去掉，
    否则 "当前目标：cut 减脂 / bulk 增肌" 这串东西会被模型整句抄给用户。
    """
    raw = PROFILE_FIELDS.get(key)
    if not raw:
        return key
    return re.split(r"[：，]", raw)[0].strip() or key


def field_label(key: str) -> str:
    """档案字段的用户可读名称。

    未知字段返回原样：宁可露出一个字段名，也不能把话说漏一截——空串会让
    "缺少 X" 变成 "缺少 "，用户完全不知道该补什么。
    """
    if key not in PROFILE_FIELDS:
        return key
    return _ANNOTATION.sub("", prompt_label(key)).strip() or key


def enum_label(value: object) -> str:
    """枚举值的中文说法；不认识的原样返回。"""
    if isinstance(value, str):
        return ENUM_LABELS.get(value, value)
    return str(value)


def _slot_label(key: object) -> str:
    return SLOT_LABELS.get(key, str(key)) if isinstance(key, str) else str(key)


def render_value(value: object) -> str:
    """把档案里的值渲染成人话。

    dict 一律按键排序：档案每轮注入提示，序列化顺序不稳定会打断缓存前缀，
    而且不会报错，只体现为账单偏高。
    """
    if value is None or value == [] or value == {}:
        return "无"
    if isinstance(value, dict):
        return "；".join(
            f"{_slot_label(k)} {render_value(value[k])}" for k in sorted(value, key=str)
        )
    if isinstance(value, (list, tuple)):
        return "、".join(render_value(v) for v in value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return enum_label(value)
