"""创建开箱即用的演示账号与 8 周数据：python scripts/seed_demo.py。"""

import asyncio
import hashlib
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session_maker, bind_rls_user
from app.core.memory.profile import merge_profile
from app.core.security import hash_password
from app.models.body_metric import BodyMetric
from app.models.memory import Memory
from app.models.user import User
from app.models.workout_log import WorkoutLog
from scripts.seed_foods import seed as seed_foods

DEMO_EMAIL = "demo@fitmind.cn"
DEMO_PASSWORD = "demo123456"


async def seed_demo() -> uuid.UUID:
    await seed_foods()
    async with async_session_maker() as session:
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            user = User(email=DEMO_EMAIL, password_hash=hash_password(DEMO_PASSWORD))
            session.add(user)
        # 查用户会先开启一个尚未绑定 RLS 的事务；无论用户是否已存在都先结束它，
        # 再绑定身份，确保后续事务的 after_begin 事件能注入 app.user_id。
        await session.commit()
        bind_rls_user(session, user.id)
        await merge_profile(session, user.id, {
            "height_cm": 178, "weight_kg": 79.6, "age": 30, "sex": "male",
            "activity": "moderate", "target_kg": 75, "goal": "cut",
            "phase_started_on": (date.today() - timedelta(days=56)).isoformat(),
            "training_split": "上下肢分化，每周 4 练", "equipment": "健身房",
            "lifts": {"深蹲": 130, "卧推": 95, "硬拉": 160},
            "injuries": ["右肩曾有轻微不适"], "dislikes": ["香菜"],
            "meal_scenarios": {"weekday_lunch": "office", "default": "cook"},
        })
        start = date.today() - timedelta(weeks=7)
        for week in range(8):
            day = start + timedelta(weeks=week)
            sets = [{"weight": 75 + week * 2.5, "reps": 5, "rpe": 7.5}] * 5
            key = hashlib.sha256(f"demo-workout-{week}".encode()).hexdigest()
            await session.execute(pg_insert(WorkoutLog).values(
                user_id=user.id, date=day, exercise="卧推", sets=sets,
                idempotency_key=key,
            ).on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"]))
            weight = round(82 - week * 0.35, 1)
            metric_key = hashlib.sha256(f"demo-metric-{week}".encode()).hexdigest()
            await session.execute(pg_insert(BodyMetric).values(
                user_id=user.id, date=day, weight_kg=weight,
                body_fat_pct=round(20 - week * 0.25, 1), idempotency_key=metric_key,
            ).on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"]))
        # 即使连接角色可绕过 RLS，也只能用当前演示用户的数据做幂等判断。
        existing = (await session.scalars(
            select(Memory).where(Memory.user_id == user.id),
        )).all()
        if not existing:
            contents = [("工作日午餐通常在公司解决", "scenario"),
                        ("不吃香菜", "preference"),
                        ("右肩做过顶推举时需要充分热身", "injury")]
            for index, (content, category) in enumerate(contents):
                vector = [0.0] * 1536
                vector[index] = 1.0
                session.add(Memory(user_id=user.id, content=content, category=category,
                                   embedding=vector, confidence=0.95))
        await session.commit()
        return user.id


if __name__ == "__main__":
    user_id = asyncio.run(seed_demo())
    print(f"演示账号已就绪：{DEMO_EMAIL} / {DEMO_PASSWORD}（user_id={user_id}）")
