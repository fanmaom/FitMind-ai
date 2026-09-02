"""判定结果缓存。

判定调用实测单次 2.8–4.3 秒（真实网关），judge_json 内部还会重试最多 3 次。
一轮抽 3 条待办、每条 3 个候选就是 9 次判定，接近 30 秒。这些调用有大量重复：
job 退避重试会把整批重跑，同一批内候选高度重叠，跨轮聊同一话题会反复比对
同一条已有项。
"""

import asyncio

import pytest

from app.core.llm.judge_cache import JudgeCache, action_dedupe_cache, fact_reconcile_cache

# 全局缓存的清理由 conftest 的 _reset_judge_caches 负责——它对所有测试生效，
# 因为不同用例经常用相同输入却期望不同判定结果。


class TestBasicCaching:
    @pytest.mark.asyncio
    async def test_second_call_hits_cache(self):
        cache = JudgeCache()
        calls = []

        async def compute():
            calls.append(1)
            return "same"

        assert await cache.get_or_compute("k", compute) == "same"
        assert await cache.get_or_compute("k", compute) == "same"
        assert len(calls) == 1, "第二次仍然调了模型，缓存没生效"
        assert cache.stats()["hits"] == 1

    @pytest.mark.asyncio
    async def test_different_keys_computed_separately(self):
        cache = JudgeCache()
        results = []

        async def make(value):
            async def compute():
                results.append(value)
                return value
            return compute

        assert await cache.get_or_compute("a", await make("A")) == "A"
        assert await cache.get_or_compute("b", await make("B")) == "B"
        assert results == ["A", "B"]

    @pytest.mark.asyncio
    async def test_false_result_is_cached(self):
        """False 是有效结果，不能因为"假值"就重算。"""
        cache = JudgeCache()
        calls = []

        async def compute():
            calls.append(1)
            return False

        assert await cache.get_or_compute("k", compute) is False
        assert await cache.get_or_compute("k", compute) is False
        assert len(calls) == 1


class TestFailureNotCached:
    @pytest.mark.asyncio
    async def test_exception_propagates_and_is_not_cached(self):
        """判定失败往往是暂时的（空输出、限流）。缓存失败会把一次偶发故障
        固化成永久故障——之后每次都直接抛，永远不再尝试。"""
        cache = JudgeCache()
        calls = []

        async def failing():
            calls.append(1)
            raise RuntimeError("空输出")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cache.get_or_compute("k", failing)
        assert len(calls) == 2, "失败被缓存了，第二次没有重新尝试"

    @pytest.mark.asyncio
    async def test_success_after_failure_is_cached(self):
        cache = JudgeCache()
        state = {"fail": True}

        async def flaky():
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("暂时失败")
            return "ok"

        with pytest.raises(RuntimeError):
            await cache.get_or_compute("k", flaky)
        assert await cache.get_or_compute("k", flaky) == "ok"
        assert await cache.get_or_compute("k", flaky) == "ok"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_same_key_computes_once(self):
        """同一对输入的并发请求不该各发一次调用——判定要几秒，
        并发打三次就是三倍成本换同一个答案。"""
        cache = JudgeCache()
        calls = []

        async def slow():
            calls.append(1)
            await asyncio.sleep(0.05)
            return "same"

        results = await asyncio.gather(
            *[cache.get_or_compute("k", slow) for _ in range(5)],
        )
        assert results == ["same"] * 5
        assert len(calls) == 1, f"并发发起了 {len(calls)} 次调用"

    @pytest.mark.asyncio
    async def test_concurrent_failure_propagates_to_all_waiters(self):
        cache = JudgeCache()

        async def slow_fail():
            await asyncio.sleep(0.02)
            raise RuntimeError("boom")

        results = await asyncio.gather(
            *[cache.get_or_compute("k", slow_fail) for _ in range(3)],
            return_exceptions=True,
        )
        assert all(isinstance(r, RuntimeError) for r in results)

    @pytest.mark.asyncio
    async def test_pending_cleared_after_completion(self):
        """飞行中的记录必须清掉，否则第二批请求会等一个已完成的 Future。"""
        cache = JudgeCache()

        async def compute():
            return "x"

        await cache.get_or_compute("k", compute)
        assert cache._pending == {}


class TestEviction:
    @pytest.mark.asyncio
    async def test_lru_evicts_oldest(self):
        cache = JudgeCache(max_entries=2)

        async def make(value):
            async def compute():
                return value
            return compute

        await cache.get_or_compute("a", await make("A"))
        await cache.get_or_compute("b", await make("B"))
        await cache.get_or_compute("c", await make("C"))

        assert cache.stats()["size"] == 2
        assert "a" not in cache._done, "最旧的条目没有被淘汰"

    @pytest.mark.asyncio
    async def test_access_refreshes_recency(self):
        cache = JudgeCache(max_entries=2)

        async def make(value):
            async def compute():
                return value
            return compute

        await cache.get_or_compute("a", await make("A"))
        await cache.get_or_compute("b", await make("B"))
        await cache.get_or_compute("a", await make("A"))  # a 变成最近使用
        await cache.get_or_compute("c", await make("C"))

        assert "a" in cache._done
        assert "b" not in cache._done


class TestDedupeIntegration:
    @pytest.mark.asyncio
    async def test_repeated_pair_calls_judge_once(self, monkeypatch):
        import app.core.actions.dedupe as dedupe

        calls = []

        async def fake_judge(prompt, **kwargs):
            calls.append(prompt)
            return {"same": True}

        monkeypatch.setattr(dedupe, "judge_json", fake_judge)

        for _ in range(3):
            assert await dedupe._is_same_task("把卧推加到 82.5kg", "下周卧推 82.5 公斤")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_direction_is_part_of_key(self, monkeypatch):
        """「是否同一件事」在语义上对称，但 prompt 里两者位置不同，模型的
        回答不保证一致。把两个方向当成同一个 key 会让缓存返回另一个方向的
        结果。"""
        import app.core.actions.dedupe as dedupe

        calls = []

        async def fake_judge(prompt, **kwargs):
            calls.append(prompt)
            return {"same": True}

        monkeypatch.setattr(dedupe, "judge_json", fake_judge)

        await dedupe._is_same_task("甲", "乙")
        await dedupe._is_same_task("乙", "甲")
        assert len(calls) == 2, "两个方向被当成了同一个缓存 key"


class TestReconcileIntegration:
    @pytest.mark.asyncio
    async def test_repeated_pair_calls_judge_once(self, monkeypatch):
        import app.core.memory.reconcile as reconcile

        calls = []

        async def fake_judge(prompt, **kwargs):
            calls.append(prompt)
            return {"relation": "update"}

        monkeypatch.setattr(reconcile, "judge_json", fake_judge)

        for _ in range(3):
            assert await reconcile._judge("旧记忆", "新记忆") == "update"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_direction_matters_for_supersede(self, monkeypatch):
        """supersede 的语义是"旧的失效"，方向反了结论就反了——
        缓存把两个方向混在一起会让新记忆被旧记忆顶掉。"""
        import app.core.memory.reconcile as reconcile

        calls = []

        async def fake_judge(prompt, **kwargs):
            calls.append(prompt)
            return {"relation": "supersede"}

        monkeypatch.setattr(reconcile, "judge_json", fake_judge)

        await reconcile._judge("右肩有旧伤", "右肩已经好了")
        await reconcile._judge("右肩已经好了", "右肩有旧伤")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_two_caches_are_independent(self, monkeypatch):
        """两个判定点 key 空间不同，混用会互相挤占容量，
        也让命中率统计没法分开看。"""
        import app.core.actions.dedupe as dedupe
        import app.core.memory.reconcile as reconcile

        async def fake_same(prompt, **kwargs):
            return {"same": True}

        async def fake_relation(prompt, **kwargs):
            return {"relation": "duplicate"}

        monkeypatch.setattr(dedupe, "judge_json", fake_same)
        monkeypatch.setattr(reconcile, "judge_json", fake_relation)

        await dedupe._is_same_task("甲", "乙")
        await reconcile._judge("甲", "乙")

        assert action_dedupe_cache.stats()["size"] == 1
        assert fact_reconcile_cache.stats()["size"] == 1


class TestExactMatchShortCircuit:
    @pytest.mark.asyncio
    async def test_identical_text_skips_judge(self, db, seeded_user, monkeypatch):
        """逐字相同不必问模型。这在 job 退避重试里很常见——同一条建议被
        重新抽出来、和上次写进去的那条逐字比对。

        用会爆炸的假 judge 来断言：只要走到判定就立刻失败，比检查调用次数
        更直接。
        """
        import app.core.actions.dedupe as dedupe
        from app.core.actions.store import insert_action

        async def exploding_judge(prompt, **kwargs):
            raise AssertionError("逐字相同的两条待办不该调模型判定")

        async def fixed_embed(_content: str) -> list[float]:
            return _unit_vector()

        monkeypatch.setattr(dedupe, "judge_json", exploding_judge)
        monkeypatch.setattr(dedupe, "embed", fixed_embed)

        content = "把卧推工作重量提到 82.5kg"
        existing = await insert_action(
            db, seeded_user, content, "training", vector=_unit_vector(),
        )
        await db.flush()

        # 前提校验：候选查询必须真的能查到那条，否则这个测试什么都没验证。
        candidates = await dedupe.find_candidates(db, seeded_user, _unit_vector())
        assert existing.id in [c.id for c in candidates], "候选没查到，测试失去意义"

        assert await dedupe.insert_if_new(db, seeded_user, content, "training") is None

    @pytest.mark.asyncio
    async def test_different_text_still_judged(self, db, seeded_user, monkeypatch):
        """短路只对逐字相同生效，不能顺手把"措辞不同的同一件事"也跳过——
        那才是判定真正要解决的问题。"""
        import app.core.actions.dedupe as dedupe
        from app.core.actions.store import insert_action

        calls = []

        async def counting_judge(prompt, **kwargs):
            calls.append(prompt)
            return {"same": True}

        async def fixed_embed(_content: str) -> list[float]:
            return _unit_vector()

        monkeypatch.setattr(dedupe, "judge_json", counting_judge)
        monkeypatch.setattr(dedupe, "embed", fixed_embed)

        await insert_action(
            db, seeded_user, "把卧推工作重量提到 82.5kg", "training",
            vector=_unit_vector(),
        )
        await db.flush()

        result = await dedupe.insert_if_new(
            db, seeded_user, "下周把卧推加到 82.5 公斤", "training",
        )
        assert result is None
        assert len(calls) == 1, "措辞不同的两条没有走判定"


def _unit_vector() -> list[float]:
    from app.core.config import get_settings

    vector = [0.0] * get_settings().embedding_dim
    vector[0] = 1.0
    return vector
