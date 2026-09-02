"""会话滚动摘要：填补「压缩只丢不摘」的缺口。"""

import uuid

import pytest

from app.core.agent.context import build_context, compress_history
from app.core.llm.client import ChatChunk, LLMError
from app.core.memory.summary import (
    MAX_SUMMARY_CHARS,
    handle_summarize_conversation,
    load_summary,
    summarize,
)
from app.models.conversation import Conversation
from app.models.message import Message


class _StubProvider:
    """可控的假 provider，避免打真实网关。"""

    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    async def stream(self, req):
        self.prompts.append(req.messages[0]["content"])
        if self.text:
            yield ChatChunk(text_delta=self.text)


@pytest.fixture
def stub_llm(monkeypatch):
    def install(text: str) -> _StubProvider:
        provider = _StubProvider(text)
        monkeypatch.setattr(
            "app.core.memory.summary.build_provider", lambda *_a, **_kw: provider,
        )
        return provider

    return install


async def _conversation_with_messages(db, user_id, texts: list[str]) -> uuid.UUID:
    conv = Conversation(user_id=user_id, title="t")
    db.add(conv)
    await db.flush()
    for index, text in enumerate(texts):
        db.add(Message(
            conversation_id=conv.id, user_id=user_id,
            role="user" if index % 2 == 0 else "assistant",
            content={"text": text}, status="done",
        ))
    await db.flush()
    return conv.id


class TestSummarize:
    @pytest.mark.asyncio
    async def test_returns_model_output(self, stub_llm):
        stub_llm("用户在执行 8 周减脂，前提 82kg、TDEE 2770。")
        assert "8 周减脂" in await summarize("用户：...\n助理：...", None)

    @pytest.mark.asyncio
    async def test_empty_output_raises_for_retry(self, stub_llm):
        """推理模型思考预算耗尽时返回 HTTP 200 + 空正文，不是报错。
        当成"这段没什么可摘要的"会让摘要静默退化成"从不摘要"，且日志无痕。"""
        stub_llm("")
        with pytest.raises(LLMError, match="空输出"):
            await summarize("用户：...\n助理：...", None)

    @pytest.mark.asyncio
    async def test_whitespace_only_also_raises(self, stub_llm):
        stub_llm("   \n  ")
        with pytest.raises(LLMError):
            await summarize("对话", None)

    @pytest.mark.asyncio
    async def test_output_is_hard_truncated(self, stub_llm):
        """模型经常无视长度要求。不硬截断，摘要会一轮轮膨胀，
        最后比它省下的历史还长。"""
        stub_llm("很长" * 2000)
        assert len(await summarize("对话", None)) == MAX_SUMMARY_CHARS

    @pytest.mark.asyncio
    async def test_existing_summary_is_passed_for_merging(self, stub_llm):
        provider = stub_llm("合并后的摘要")
        await summarize("新增对话", "之前的摘要内容")
        assert "之前的摘要内容" in provider.prompts[0]

    @pytest.mark.asyncio
    async def test_first_run_has_no_existing_block(self, stub_llm):
        provider = stub_llm("首次摘要")
        await summarize("对话", None)
        assert "已有摘要" not in provider.prompts[0]


class TestHandler:
    @pytest.mark.asyncio
    async def test_creates_summary_and_records_coverage(self, db, seeded_user, stub_llm):
        stub_llm("摘要内容")
        conv_id = await _conversation_with_messages(
            db, seeded_user, ["问题一", "回答一", "问题二", "回答二"],
        )
        await handle_summarize_conversation(db, {
            "conversation_id": str(conv_id), "user_id": str(seeded_user),
        })

        row = await load_summary(db, conv_id)
        assert row.content == "摘要内容"
        assert row.covered_seq > 0, "covered_seq 没有推进，下次会重复摘要同一段"

    @pytest.mark.asyncio
    async def test_second_run_only_reads_new_messages(self, db, seeded_user, stub_llm):
        """增量摘要：不能每次都把全部历史重新摘要一遍——既浪费，
        又会让摘要随调用次数漂移。"""
        stub_llm("第一版摘要")
        conv_id = await _conversation_with_messages(db, seeded_user, ["旧问题", "旧回答"])
        payload = {"conversation_id": str(conv_id), "user_id": str(seeded_user)}
        await handle_summarize_conversation(db, payload)

        db.add(Message(
            conversation_id=conv_id, user_id=seeded_user, role="user",
            content={"text": "全新的问题"}, status="done",
        ))
        await db.flush()

        provider = stub_llm("第二版摘要")
        await handle_summarize_conversation(db, payload)

        prompt = provider.prompts[0]
        assert "全新的问题" in prompt
        assert "旧问题" not in prompt, "已覆盖的消息被重复送去摘要"
        assert "第一版摘要" in prompt, "没有把已有摘要传进去合并"

    @pytest.mark.asyncio
    async def test_no_new_messages_skips_llm(self, db, seeded_user, stub_llm):
        stub_llm("摘要")
        conv_id = await _conversation_with_messages(db, seeded_user, ["问", "答"])
        payload = {"conversation_id": str(conv_id), "user_id": str(seeded_user)}
        await handle_summarize_conversation(db, payload)

        provider = stub_llm("不该被调用")
        await handle_summarize_conversation(db, payload)
        assert provider.prompts == [], "没有新消息却仍然调了模型"

    @pytest.mark.asyncio
    async def test_repeated_runs_keep_single_row(self, db, seeded_user, stub_llm):
        """一个会话一份摘要。靠唯一约束 + upsert 收敛，
        而不是"先查再插"——后者并发下会插出两行。"""
        from sqlalchemy import func, select

        from app.models.conversation_summary import ConversationSummary

        conv_id = await _conversation_with_messages(db, seeded_user, ["问", "答"])
        payload = {"conversation_id": str(conv_id), "user_id": str(seeded_user)}
        for index in range(3):
            stub_llm(f"第{index}版")
            db.add(Message(
                conversation_id=conv_id, user_id=seeded_user, role="user",
                content={"text": f"追加{index}"}, status="done",
            ))
            await db.flush()
            await handle_summarize_conversation(db, payload)

        count = await db.scalar(
            select(func.count()).select_from(ConversationSummary)
            .where(ConversationSummary.conversation_id == conv_id),
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_streaming_messages_excluded(self, db, seeded_user, stub_llm):
        """streaming 是正在写、还没定稿的，不该进摘要。"""
        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()
        db.add(Message(
            conversation_id=conv.id, user_id=seeded_user, role="user",
            content={"text": "已完成的问题"}, status="done",
        ))
        db.add(Message(
            conversation_id=conv.id, user_id=seeded_user, role="assistant",
            content={"text": "还在写的回答"}, status="streaming",
        ))
        await db.flush()

        provider = stub_llm("摘要")
        await handle_summarize_conversation(db, {
            "conversation_id": str(conv.id), "user_id": str(seeded_user),
        })
        assert "还在写的回答" not in provider.prompts[0]


class TestContextInjection:
    HISTORY = [{"role": "user", "content": f"第{i}条"} for i in range(12)]

    def test_summary_injected_when_history_was_dropped(self):
        msgs, budget = build_context(
            profile={}, facts=[], history=self.HISTORY, user_text="现在",
            today="2026-08-25", summary="更早聊过 8 周减脂计划",
            keep_recent=3, max_history_tokens=0,
        )
        joined = "".join(
            m["content"] for m in msgs if isinstance(m.get("content"), str)
        )
        assert "8 周减脂计划" in joined
        assert budget.summary > 0

    def test_summary_skipped_when_nothing_was_dropped(self):
        """没丢东西就不注入：白烧 token，而且摘要与还在上下文里的原文重复，
        会让模型在两份说法之间摇摆。"""
        msgs, budget = build_context(
            profile={}, facts=[], history=self.HISTORY[:2], user_text="现在",
            today="2026-08-25", summary="更早聊过 8 周减脂计划",
            keep_recent=6, max_history_tokens=10_000,
        )
        joined = "".join(
            m["content"] for m in msgs if isinstance(m.get("content"), str)
        )
        assert "8 周减脂计划" not in joined
        assert budget.summary == 0

    def test_summary_marked_as_older_than_verbatim_history(self):
        """摘要天生滞后。不标注的话，模型会把里面过时的中间结论当成现状复述。"""
        msgs, _ = build_context(
            profile={}, facts=[], history=self.HISTORY, user_text="现在",
            today="2026-08-25", summary="旧结论",
            keep_recent=3, max_history_tokens=0,
        )
        block = msgs[1]["content"]
        assert "更早" in block
        assert "以原文为准" in block

    def test_empty_summary_adds_nothing(self):
        _, budget = build_context(
            profile={}, facts=[], history=self.HISTORY, user_text="现在",
            today="2026-08-25", summary="",
            keep_recent=3, max_history_tokens=0,
        )
        assert budget.summary == 0

    def test_summary_stays_after_cache_prefix(self):
        """摘要每轮可能变，写进系统提示会打断缓存前缀。"""
        a, _ = build_context(
            profile={}, facts=[], history=self.HISTORY, user_text="x",
            today="2026-08-25", summary="摘要甲", keep_recent=3, max_history_tokens=0,
        )
        b, _ = build_context(
            profile={}, facts=[], history=self.HISTORY, user_text="x",
            today="2026-08-25", summary="摘要乙", keep_recent=3, max_history_tokens=0,
        )
        assert a[0] == b[0], "摘要变化影响了系统提示，缓存前缀会失效"


class TestCompressStillWorks:
    def test_public_helper_signature_unchanged(self):
        """compress_history 是既有公开函数，签名和返回值不能变。"""
        history = [{"role": "user", "content": f"第{i}条"} for i in range(20)]
        out = compress_history(history, keep_recent=6, max_tokens=10_000)
        assert isinstance(out, list)
        assert out[-6:] == history[-6:]


class TestJobWiring:
    def test_handler_registered(self):
        """handler 没注册的话，job 会以「未知任务类型」失败三次后进 failed，
        而摘要永远不会生成。"""
        from app.core.jobs.worker import HANDLERS

        assert "summarize_conversation" in HANDLERS

    def test_enqueued_only_for_long_conversations(self):
        """每轮都投等于给每次对话白加一次 LLM 调用，而短会话的历史全在窗口里、
        根本没有缺口要填。"""
        import inspect

        import app.services.chat_service as service

        source = inspect.getsource(service._run_turn_inner)
        assert "summarize_conversation" in source, "没有投递摘要任务"
        assert "agent_history_window * 2" in source, (
            "摘要任务没有长度门槛，短会话也会白投"
        )

    def test_interrupted_turn_does_not_enqueue(self):
        """中断的回合在 finish 后直接 return，摘要任务和抽取任务一样不投——
        半句话摘要会把没说完的结论写成既定事实。"""
        import inspect

        import app.services.chat_service as service

        source = inspect.getsource(service._run_turn_inner)
        interrupted_pos = source.index('{"interrupted": True}')
        summary_pos = source.index('"summarize_conversation"')
        assert interrupted_pos < summary_pos, (
            "摘要投递出现在中断分支之前，中断的回合也会投"
        )
