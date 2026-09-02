"""对话后事实抽取。"""

import json

import pytest

from app.core.memory.extractor import (
    MIN_CONFIDENCE,
    ExtractedFact,
    _parse,
    extract_facts,
    handle_extract_memory,
)


class TestParse:
    def test_parses_json_and_fenced_json(self):
        raw = json.dumps([{
            "content": "用户不吃香菜", "category": "preference", "confidence": 0.95,
        }], ensure_ascii=False)
        assert _parse(raw)[0].content == "用户不吃香菜"
        assert _parse(f"```json\n{raw}\n```")[0].category == "preference"

    def test_malformed_output_returns_empty(self):
        assert _parse("not-json") == []
        assert _parse('{"content": "不是数组"}') == []

    def test_invalid_category_falls_back_and_content_is_bounded(self):
        raw = json.dumps([{
            "content": "x" * 800, "category": "invented", "confidence": 0.8,
        }])
        fact = _parse(raw)[0]
        assert fact.category == "general"
        assert len(fact.content) == 500


class TestExtract:
    @pytest.mark.asyncio
    async def test_collects_streamed_json(self, monkeypatch):
        import app.core.memory.extractor as module
        from app.core.llm.client import ChatChunk

        class Provider:
            async def stream(self, _request):
                yield ChatChunk(text_delta='[{"content":"右肩有旧伤",')
                yield ChatChunk(text_delta='"category":"injury","confidence":0.9}]')

        monkeypatch.setattr(module, "build_provider", lambda: Provider())
        facts = await extract_facts("用户：我的右肩以前伤过")
        assert facts == [ExtractedFact("右肩有旧伤", "injury", 0.9)]


class TestHandle:
    @pytest.mark.asyncio
    async def test_filters_low_confidence_before_write(self, db, seeded_user, monkeypatch):
        import app.core.memory.extractor as module

        async def fake_extract(_text):
            return [
                ExtractedFact("高置信事实", "general", MIN_CONFIDENCE),
                ExtractedFact("低置信推测", "general", MIN_CONFIDENCE - 0.01),
            ]

        written = []

        async def fake_reconcile(session, user_id, content, category, confidence, source_id):
            written.append((user_id, content, category, confidence, source_id))

        monkeypatch.setattr(module, "extract_facts", fake_extract)
        monkeypatch.setattr(module, "reconcile_fact", fake_reconcile)
        await handle_extract_memory(db, {
            "user_id": str(seeded_user),
            "conversation_text": "对话",
            "source_message_id": None,
        })
        assert [item[1] for item in written] == ["高置信事实"]

    @pytest.mark.asyncio
    async def test_bad_payload_is_not_silently_accepted(self, db):
        with pytest.raises((KeyError, ValueError)):
            await handle_extract_memory(db, {"conversation_text": "缺 user_id"})


class TestEmptyOutputIsNotSilentlyAccepted:
    """推理模型思考预算耗尽时返回**空正文**（HTTP 200，finish_reason=length）。

    当成"这轮没有事实"处理，记忆功能就悄悄退化成"从不记忆"：任务标记 done、
    库里 0 条、没有任何报错。线上就是这样——3 个抽取任务全部 done，
    memories 表 0 行。

    实测同一段真实对话：max_tokens=1024 → 正文 0 字 / 0 条事实；
    8192 → 457 字 / 5 条事实。
    """

    @pytest.mark.asyncio
    async def test_empty_output_raises_so_the_job_retries(self, monkeypatch):
        import app.core.memory.extractor as module
        from app.core.llm.client import ChatChunk, LLMError

        class Silent:
            async def stream(self, _request):
                yield ChatChunk(finish_reason="length")

        monkeypatch.setattr(module, "build_provider", lambda: Silent())
        with pytest.raises(LLMError):
            await extract_facts("用户：我身高181，体重92")

    @pytest.mark.asyncio
    async def test_uses_the_configured_output_budget(self, monkeypatch):
        """1024 不够这个模型思考完再输出 JSON。预算必须跟着配置走。"""
        import app.core.memory.extractor as module
        from app.core.config import get_settings
        from app.core.llm.client import ChatChunk

        seen: list[int] = []

        class Recording:
            async def stream(self, request):
                seen.append(request.max_tokens)
                yield ChatChunk(text_delta="[]")

        monkeypatch.setattr(module, "build_provider", lambda: Recording())
        await extract_facts("用户：随便说说")
        assert seen == [get_settings().llm_max_tokens]

    @pytest.mark.asyncio
    async def test_empty_array_is_a_valid_answer(self):
        """"这轮确实没有可记的事实"是合法结论，不能跟空输出混为一谈。"""
        assert _parse("[]") == []
