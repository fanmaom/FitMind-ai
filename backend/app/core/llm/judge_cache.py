"""判定结果缓存。

判定调用（记忆冲突消解、待办判重）实测单次 2.8–4.3 秒，而且 judge_json 内部
最多重试 3 次。一轮抽 3 条待办、每条 3 个候选就是 9 次判定，接近 30 秒。

这些调用有大量是重复的：

1. **job 重试**。抽取任务失败后按 30/120/600 秒退避重跑，整批待办会被重新
   判一遍。而判定的输入（两条文本）和输出（same / relation）都没变。
2. **同一批内的共享候选**。一轮抽出的几条待办往往是同一话题的不同侧面，
   向量粗筛给出的候选高度重叠，同一条已有待办会被反复拿去比对。
3. **跨轮的稳定判定**。用户连续几轮聊同一个动作时，新抽出的建议会一次次和
   同一条已有待办比较。

判定对同一对输入是稳定的，所以缓存是安全的——这是纯函数式的映射，不像召回
那样依赖库里的当前状态。

## 为什么是进程内而不是 Redis / 数据库表

判定结果没有持久化价值：worker 重启后重新判一次只是多花几秒，不会出错。
而引入 Redis 会给部署多一个依赖，落库则要多一张表加迁移——为了省几次调用
不值得。进程内 LRU 是这里成本收益比最好的一档。

## 为什么不用 functools.lru_cache

它不支持协程：包住 async 函数缓存的是尚未 await 的 coroutine 对象，第二次
命中时拿到的是**已经被消费过的** coroutine，await 它会抛
"cannot reuse already awaited coroutine"。必须自己写。

同时也要处理并发：同一对输入的两个并发请求不应各发一次调用。用 Future 让
后到的等前一个的结果。
"""

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable

from app.core.logger import logger

# 缓存容量。判定对是短文本，一千条占用可以忽略；取这个量级是为了覆盖
# "一个用户连续聊同一话题" 与 "job 退避重试" 两个场景，不追求全量命中。
MAX_ENTRIES = 1024


class JudgeCache:
    """按 key 缓存判定结果的异步 LRU。

    不缓存异常：判定失败往往是暂时的（空输出、限流），缓存失败会把一次偶发
    故障固化成永久故障。让它下次重新尝试。
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max_entries
        self._done: OrderedDict[str, object] = OrderedDict()
        self._pending: dict[str, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0

    def clear(self) -> None:
        self._done.clear()
        self._pending.clear()
        self.hits = 0
        self.misses = 0

    async def get_or_compute(self, key: str, compute: Callable[[], Awaitable]) -> object:
        if key in self._done:
            self._done.move_to_end(key)
            self.hits += 1
            return self._done[key]

        # 已有同 key 的调用在飞，等它——不要再发一次。
        inflight = self._pending.get(key)
        if inflight is not None:
            self.hits += 1
            return await asyncio.shield(inflight)

        self.misses += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[key] = future
        try:
            result = await compute()
        except BaseException as exc:
            # 让等待者也拿到同一个异常，但不写进 _done——失败不缓存。
            if not future.done():
                future.set_exception(exc)
            # 没有等待者时主动消费掉异常，避免 "Future exception was never
            # retrieved" 噪声日志。
            future.exception()
            raise
        else:
            if not future.done():
                future.set_result(result)
            self._done[key] = result
            while len(self._done) > self._max:
                self._done.popitem(last=False)
            return result
        finally:
            self._pending.pop(key, None)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": len(self._done),
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


# 两个判定点各用一个实例：key 空间不同，混在一起会让容量互相挤占，
# 也让命中率统计没法分开看。
action_dedupe_cache = JudgeCache()
fact_reconcile_cache = JudgeCache()


def log_stats() -> None:
    logger.info(
        f"判定缓存 待办判重={action_dedupe_cache.stats()} "
        f"事实消解={fact_reconcile_cache.stats()}",
    )
