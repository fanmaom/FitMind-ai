"""历史加载的数据库层上限，与「配置项必须真的生效」。"""

import uuid

import pytest

from app.core.agent.context import build_context
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.chat_service import HISTORY_FETCH_LIMIT, load_history


async def _make_conversation(db, user_id, count: int) -> tuple[uuid.UUID, list[str]]:
    conv = Conversation(user_id=user_id, title="t")
    db.add(conv)
    await db.flush()

    texts = []
    for index in range(count):
        text = f"第{index}条"
        texts.append(text)
        db.add(Message(
            conversation_id=conv.id, user_id=user_id,
            role="user" if index % 2 == 0 else "assistant",
            content={"text": text}, status="done",
        ))
        # created_at 由 server_default 生成，同一事务内多条会撞在同一时刻；
        # 排序里带上 id 做 tie-breaker，这里也逐条 flush 保证插入顺序确定。
        await db.flush()
    return conv.id, texts


class TestFetchLimit:
    @pytest.mark.asyncio
    async def test_caps_rows_loaded_from_database(self, db, seeded_user):
        """长会话不该每轮都把全部消息搬进内存——搬运和逐条 token 估算的
        成本随会话长度线性增长，而用得上的永远只是末尾那一小段。"""
        conv_id, _ = await _make_conversation(db, seeded_user, 40)
        history = await load_history(db, conv_id, limit=10)
        assert len(history) == 10

    @pytest.mark.asyncio
    async def test_keeps_newest_not_oldest(self, db, seeded_user):
        """正序 + LIMIT 会取到最旧的几条，那是完全相反的结果：
        模型看到开头几句、看不到刚刚说了什么。"""
        conv_id, texts = await _make_conversation(db, seeded_user, 20)
        history = await load_history(db, conv_id, limit=5)
        assert [m["content"] for m in history] == texts[-5:]

    @pytest.mark.asyncio
    async def test_returned_in_chronological_order(self, db, seeded_user):
        """倒序取完必须翻回正序，否则模型读到的对话是反的。"""
        conv_id, texts = await _make_conversation(db, seeded_user, 8)
        history = await load_history(db, conv_id, limit=8)
        assert [m["content"] for m in history] == texts

    @pytest.mark.asyncio
    async def test_short_conversation_unaffected(self, db, seeded_user):
        conv_id, texts = await _make_conversation(db, seeded_user, 3)
        history = await load_history(db, conv_id)
        assert [m["content"] for m in history] == texts

    @pytest.mark.asyncio
    async def test_exclude_id_still_applies_under_limit(self, db, seeded_user):
        """两个机制要能叠加：limit 取最新 N 条，其中仍要剔除本轮那条。"""
        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()
        for text in ("旧一", "旧二"):
            db.add(Message(
                conversation_id=conv.id, user_id=seeded_user, role="user",
                content={"text": text}, status="done",
            ))
            await db.flush()
        current = Message(
            conversation_id=conv.id, user_id=seeded_user, role="user",
            content={"text": "本轮"}, status="done",
        )
        db.add(current)
        await db.flush()

        history = await load_history(db, conv.id, exclude_id=current.id, limit=2)
        contents = [m["content"] for m in history]
        assert "本轮" not in contents
        assert contents == ["旧一", "旧二"]

    def test_limit_leaves_room_above_window(self):
        """上限要远高于压缩窗口，否则取舍就由这道闸决定、而不是 token 预算。
        窗口按"轮"算，一轮至少两条消息。"""
        from app.core.config import get_settings

        assert HISTORY_FETCH_LIMIT >= get_settings().agent_history_window * 2


class TestDeterministicOrdering:
    """同一回合的 user 与 assistant 在同一事务提交，created_at 完全相同
    （PostgreSQL 的 now() 返回事务开始时刻）。只按 created_at 排序时，
    同一时刻的行由数据库自行决定顺序——助理的回复可能排在用户的提问之前。

    后果：模型读到的对话是乱的，先看到回答再看到问题。不报错，只表现为
    多轮对话里偶尔答非所问；而且顺序取决于物理存储，重放数据未必能复现。
    """

    @pytest.mark.asyncio
    async def test_same_transaction_messages_keep_insertion_order(self, db, seeded_user):
        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()

        # 关键：不逐条 flush，全部在同一事务里一次写入，复现 created_at 相同
        for text in ("问题一", "回答一", "问题二", "回答二"):
            db.add(Message(
                conversation_id=conv.id, user_id=seeded_user,
                role="user" if "问题" in text else "assistant",
                content={"text": text}, status="done",
            ))
        await db.flush()

        history = await load_history(db, conv.id)
        assert [m["content"] for m in history] == ["问题一", "回答一", "问题二", "回答二"]

    @pytest.mark.asyncio
    async def test_created_at_is_identical_within_transaction(self, db, seeded_user):
        """证明前一条测试的前提成立——否则它可能只是碰巧通过。"""
        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()
        messages = [
            Message(
                conversation_id=conv.id, user_id=seeded_user, role="user",
                content={"text": f"第{i}条"}, status="done",
            )
            for i in range(3)
        ]
        for message in messages:
            db.add(message)
        await db.flush()
        for message in messages:
            await db.refresh(message)

        stamps = {m.created_at for m in messages}
        assert len(stamps) == 1, (
            "同一事务内的 created_at 竟然不同，这条测试的前提失效了——"
            "请重新确认 seq 排序键是否仍有必要"
        )
        assert len({m.seq for m in messages}) == 3, "seq 必须逐条递增"

    @pytest.mark.asyncio
    async def test_ordering_is_stable_across_repeated_reads(self, db, seeded_user):
        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()
        for index in range(10):
            db.add(Message(
                conversation_id=conv.id, user_id=seeded_user, role="user",
                content={"text": f"第{index}条"}, status="done",
            ))
        await db.flush()

        first = [m["content"] for m in await load_history(db, conv.id)]
        for _ in range(3):
            assert [m["content"] for m in await load_history(db, conv.id)] == first

    @pytest.mark.asyncio
    async def test_messages_api_uses_same_ordering(self, db, seeded_user):
        """断线重连拉历史走 API，顺序乱了用户会直接看到回答排在提问前面。"""
        import inspect

        import app.api.v1.chat as chat_api

        source = inspect.getsource(chat_api.list_messages)
        assert "Message.seq" in source, (
            "list_messages 仍按 created_at 排序，重连后消息顺序可能是乱的"
        )


class TestHistoryWindowConfigIsWired:
    """agent_history_window 一直存在、有文档、能从环境变量读进来，
    却没有被接到 build_context——改它没有任何效果。

    这种"看起来能调、实际调不动"的配置比没有更糟：出问题时会按它的值
    去推断行为，然后得出完全错误的结论。
    """

    HISTORY = [{"role": "user", "content": f"第{i}条"} for i in range(12)]

    def _history_in_prompt(self, **kwargs) -> list[str]:
        msgs, _ = build_context(
            profile={}, facts=[], history=self.HISTORY,
            user_text="现在", today="2026-08-25", **kwargs,
        )
        # 前两条是系统提示，最后一条是当前消息
        return [m["content"] for m in msgs[2:-1]]

    def test_keep_recent_is_honoured(self):
        assert len(self._history_in_prompt(keep_recent=3, max_history_tokens=0)) == 3
        assert len(self._history_in_prompt(keep_recent=8, max_history_tokens=0)) == 8

    def test_default_matches_module_constant(self):
        from app.core.agent.context import KEEP_RECENT_TURNS

        kept = self._history_in_prompt(max_history_tokens=0)
        assert len(kept) == KEEP_RECENT_TURNS

    def test_chat_service_passes_configured_window(self, monkeypatch):
        """光有参数不够——服务层得真的把配置传进来。"""
        import inspect

        import app.services.chat_service as service

        source = inspect.getsource(service._run_turn_inner)
        assert "keep_recent=settings.agent_history_window" in source, (
            "chat_service 没有把 agent_history_window 传给 build_context，"
            "这个配置项改了不会有任何效果"
        )
