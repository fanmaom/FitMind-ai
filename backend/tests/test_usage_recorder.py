"""LLM 用量记录与统计。"""

import pytest

from app.core.llm.usage_recorder import extract_tokens, record_usage, summarize_usage


class TestExtractTokens:
    def test_openai_field_names(self):
        assert extract_tokens({
            "prompt_tokens": 5000, "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 4000},
        }) == (5000, 300, 4000)

    def test_anthropic_field_names(self):
        """字段名各家不同，都要认。"""
        assert extract_tokens({
            "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80,
        }) == (100, 20, 80)

    def test_missing_usage_is_tolerated(self):
        """有些兼容接口干脆不返回 usage，不能因此炸掉整个请求。"""
        assert extract_tokens(None) == (0, 0, 0)
        assert extract_tokens({}) == (0, 0, 0)

    def test_partial_usage_is_tolerated(self):
        assert extract_tokens({"prompt_tokens": 100}) == (100, 0, 0)


class TestRecordAndSummarize:
    @pytest.mark.asyncio
    async def test_records_and_summarizes(self, db, seeded_user):
        await record_usage(
            session=db, user_id=seeded_user, conversation_id=None, model="m",
            usage={"prompt_tokens": 5000, "completion_tokens": 300,
                   "prompt_tokens_details": {"cached_tokens": 4000}},
            latency_ms=1200, tool_calls=2, degradation_level=0,
        )
        await db.flush()

        s = await summarize_usage(db, seeded_user)
        assert s["request_count"] == 1
        assert s["total_input"] == 5000
        assert s["total_cached"] == 4000
        assert s["cache_hit_rate"] == pytest.approx(0.8)
        assert s["degradation_rate"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_tracks_degradation_rate(self, db, seeded_user):
        """运行数据可以直观反映降级机制的实际效果。"""
        for level in (0, 0, 0, 2):
            await record_usage(
                session=db, user_id=seeded_user, conversation_id=None, model="m",
                usage=None, latency_ms=100, tool_calls=0, degradation_level=level,
            )
        await db.flush()

        s = await summarize_usage(db, seeded_user)
        assert s["request_count"] == 4
        assert s["degraded_count"] == 1
        assert s["degradation_rate"] == pytest.approx(0.25)

    @pytest.mark.asyncio
    async def test_no_usage_does_not_crash(self, db, seeded_user):
        await record_usage(
            session=db, user_id=seeded_user, conversation_id=None, model="m",
            usage=None, latency_ms=900, tool_calls=0, degradation_level=1,
        )
        await db.flush()
        assert (await summarize_usage(db, seeded_user))["request_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_history_returns_zeros(self, db, seeded_user):
        s = await summarize_usage(db, seeded_user)
        assert s["request_count"] == 0
        assert s["cache_hit_rate"] == 0.0
