"""工具超时/异常后的 session 恢复。

asyncio.wait_for 超时会取消协程，而被取消的协程可能正卡在一条 SQL 上。
asyncpg 连接被留在"事务已开始、语句未完成"的状态，SQLAlchemy 随后对同一个
session 的任何操作都抛 PendingRollbackError。

实测（真实 PostgreSQL）：

    → 发起会超时的查询（timeout=0.3s）
    ✓ 按预期超时
    → 超时后用同一个 session 再查一次
    ✗ PendingRollbackError: Can't reconnect until invalid transaction is
      rolled back.
    → 再试第二次
    ✗ 同样失败

后果远超"这一个工具没算出来"：本轮剩下的工具全部失败，连收尾时把助理消息
落库的那次 commit 也失败——用户的整个回合凭空消失。而超时在 _execute_tool
里被转成一句温和的文本反馈，把这个严重故障完全掩盖了。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app.core.agent.loop import AgentLoop
from app.core.database import async_session_maker, bind_rls_user
from app.core.llm.client import ChatChunk, ToolCall
from app.core.llm.with_fallback import FallbackProvider
from app.core.tools.registry import ToolContext
from app.models.workout_log import WorkoutLog

from tests.test_agent_loop import ScriptedProvider


class _RecordingSession:
    """记录 rollback 调用次数的假 session。"""

    def __init__(self, rollback_fails: bool = False):
        self.rollbacks = 0
        self.rollback_fails = rollback_fails

    async def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise RuntimeError("连接已断开")


def _loop_with_session(provider, session, **kw) -> AgentLoop:
    ctx = ToolContext(user_id=uuid.uuid4(), session=session)
    return AgentLoop(FallbackProvider(provider, backoff_s=(0.0, 0.0)), ctx, **kw)


async def _drain(loop: AgentLoop) -> list:
    return [
        event async for event in loop.run(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            profile={}, user_text="hi",
        )
    ]


def _one_tool_then_answer(name: str = "query_workout_history") -> ScriptedProvider:
    return ScriptedProvider([
        [ChatChunk(
            tool_calls=[ToolCall(id="c1", name=name, arguments={})],
            finish_reason="tool_calls",
        )],
        [ChatChunk(text_delta="好了"), ChatChunk(finish_reason="stop")],
    ])


class TestRollbackOnTimeout:
    @pytest.mark.asyncio
    async def test_timeout_triggers_rollback(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def hanging_invoke(name, args, ctx):
            await asyncio.sleep(10)

        monkeypatch.setattr(reg_mod.registry, "invoke", hanging_invoke)

        session = _RecordingSession()
        loop = _loop_with_session(
            _one_tool_then_answer(), session, tool_timeout_s=0.01,
        )
        await _drain(loop)
        assert session.rollbacks == 1, (
            "工具超时后没有回滚。同一 session 的后续操作会全部抛 "
            "PendingRollbackError，包括把助理回复落库的那次 commit"
        )

    @pytest.mark.asyncio
    async def test_exception_also_triggers_rollback(self, monkeypatch):
        """数据库异常（唯一约束冲突、死锁）都走这条路，
        不回滚后续照样连锁失败。"""
        from app.core.tools import registry as reg_mod

        async def failing_invoke(name, args, ctx):
            raise RuntimeError("死锁")

        monkeypatch.setattr(reg_mod.registry, "invoke", failing_invoke)

        session = _RecordingSession()
        await _drain(_loop_with_session(_one_tool_then_answer(), session))
        assert session.rollbacks == 1

    @pytest.mark.asyncio
    async def test_validation_error_does_not_rollback(self, monkeypatch):
        """参数校验发生在执行之前，没碰数据库。多余的回滚会把同一事务里
        前面工具做的事撤掉。"""
        from app.core.tools import registry as reg_mod
        from app.core.tools.registry import ToolValidationError

        async def invalid_invoke(name, args, ctx):
            raise ToolValidationError("参数校验失败 —— weight_kg: 必须大于 0")

        monkeypatch.setattr(reg_mod.registry, "invoke", invalid_invoke)

        session = _RecordingSession()
        await _drain(_loop_with_session(_one_tool_then_answer(), session))
        assert session.rollbacks == 0

    @pytest.mark.asyncio
    async def test_success_does_not_rollback(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def ok_invoke(name, args, ctx):
            return {"ok": True}

        monkeypatch.setattr(reg_mod.registry, "invoke", ok_invoke)

        session = _RecordingSession()
        await _drain(_loop_with_session(_one_tool_then_answer(), session))
        assert session.rollbacks == 0

    @pytest.mark.asyncio
    async def test_failed_rollback_tells_model_to_stop(self, monkeypatch):
        """回滚本身也可能失败（连接真的断了）。那种情况下 session 已不可用，
        必须让模型停手，而不是继续调工具、每个都失败。"""
        from app.core.tools import registry as reg_mod

        async def hanging_invoke(name, args, ctx):
            await asyncio.sleep(10)

        monkeypatch.setattr(reg_mod.registry, "invoke", hanging_invoke)

        session = _RecordingSession(rollback_fails=True)
        loop = _loop_with_session(
            _one_tool_then_answer(), session, tool_timeout_s=0.01,
        )
        events = await _drain(loop)
        feedback = next(e for e in events if e.type == "tool_result")
        assert "不要再调用任何工具" in feedback.data["summary"]

    @pytest.mark.asyncio
    async def test_no_tool_ctx_is_safe(self, monkeypatch):
        """降级路径和部分单测传 tool_ctx=None，不能因此崩掉。"""
        from app.core.tools import registry as reg_mod

        async def failing_invoke(name, args, ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(reg_mod.registry, "invoke", failing_invoke)

        loop = AgentLoop(
            FallbackProvider(_one_tool_then_answer(), backoff_s=(0.0, 0.0)),
            tool_ctx=None,
        )
        events = await _drain(loop)
        assert events[-1].type == "done"


class TestRealSessionSurvivesTimeout:
    """用真实数据库验证：超时之后 session 还能继续干活。

    上面那些用假 session 只验证了"调了 rollback"，这一条验证"调了之后
    真的能用"——两者不是一回事。
    """

    @pytest.mark.asyncio
    async def test_session_usable_after_tool_timeout(self, monkeypatch, seeded_user):
        from app.core.tools import registry as reg_mod

        session = async_session_maker()
        bind_rls_user(session, seeded_user)
        try:
            async def slow_sql(name, args, ctx):
                await ctx.session.execute(text("SELECT pg_sleep(3)"))

            monkeypatch.setattr(reg_mod.registry, "invoke", slow_sql)

            loop = _loop_with_session(
                _one_tool_then_answer(), session, tool_timeout_s=0.05,
            )
            loop.tool_ctx = ToolContext(user_id=seeded_user, session=session)
            events = await _drain(loop)
            assert events[-1].type == "done"

            # 关键断言：超时之后还能查库。修复前这里抛 PendingRollbackError。
            rows = await session.scalars(select(WorkoutLog).limit(1))
            assert rows is not None
        finally:
            await session.close()
