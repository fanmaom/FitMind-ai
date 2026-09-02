"""中断登记表：热路径纯内存，跨进程靠 PG LISTEN/NOTIFY 广播。

按 conversation_id 记「这个会话请求中断了」。前端在拿到 message_start 之前
就可能点停止，那时它手上只有 conversation_id——所以键选它而不是 message id。
"""

import asyncio
import inspect
import uuid

import pytest

from app.services import interrupt


@pytest.fixture(autouse=True)
def _clean_flags():
    interrupt._requested.clear()
    yield
    interrupt._requested.clear()


class TestRegistry:
    def test_not_requested_by_default(self):
        assert interrupt.is_requested(uuid.uuid4()) is False

    def test_request_then_is_requested(self):
        conv = uuid.uuid4()
        interrupt.request(conv)
        try:
            assert interrupt.is_requested(conv) is True
        finally:
            interrupt.clear(conv)

    def test_clear_removes_the_flag(self):
        conv = uuid.uuid4()
        interrupt.request(conv)
        interrupt.clear(conv)
        assert interrupt.is_requested(conv) is False

    def test_conversations_do_not_leak_into_each_other(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        interrupt.request(a)
        try:
            assert interrupt.is_requested(b) is False
        finally:
            interrupt.clear(a)

    def test_clear_is_idempotent(self):
        """run_turn 的 finally 会无条件 clear，没有旗子时不能炸。"""
        conv = uuid.uuid4()
        interrupt.clear(conv)
        interrupt.clear(conv)

    def test_request_is_idempotent(self):
        """用户连点两下停止，不应留下两面旗子导致 clear 一次清不掉。"""
        conv = uuid.uuid4()
        interrupt.request(conv)
        interrupt.request(conv)
        interrupt.clear(conv)
        assert interrupt.is_requested(conv) is False


class TestHotPathStaysInMemory:
    """is_requested 的调用频率是每个 token 一次。把它做成数据库查询意味着
    每个 token 一条 SELECT——这是热路径上最不该出现的东西。"""

    def test_is_requested_is_synchronous(self):
        assert not inspect.iscoroutinefunction(interrupt.is_requested), (
            "is_requested 变成了协程，说明它开始做 I/O 了。每个 token 一次的"
            "调用频率下这是不可接受的"
        )

    def test_is_requested_does_not_touch_database(self, monkeypatch):
        def exploding_connect(*_a, **_kw):
            raise AssertionError("is_requested 连了数据库")

        monkeypatch.setattr(interrupt.asyncpg, "connect", exploding_connect)
        conv = uuid.uuid4()
        interrupt._requested.add(conv)
        assert interrupt.is_requested(conv) is True

    def test_clear_does_not_block_on_broadcast(self):
        """同步 clear 只写本地 set，留给非异步语境的调用方。"""
        assert not inspect.iscoroutinefunction(interrupt.clear)

    def test_sync_request_does_not_broadcast(self, monkeypatch):
        """同步版本只写本地 set。它留给不在异步上下文里的调用方，
        没有事件循环可以派发广播任务。"""
        def exploding_connect(*_a, **_kw):
            raise AssertionError("同步 request 尝试连数据库")

        monkeypatch.setattr(interrupt.asyncpg, "connect", exploding_connect)
        conv = uuid.uuid4()
        interrupt.request(conv)
        assert interrupt.is_requested(conv) is True


class TestBroadcastIsAwaitedOnRequest:
    """HTTP 端点必须等广播发出去再返回 204。

    fire-and-forget 的话，进程恰好在这之后关闭（部署、重启）会把通知丢掉，
    而用户已经看到"已停止"了——最坏的一种失败：界面说停了，实际还在烧 token。
    """

    def test_request_and_broadcast_is_awaitable(self):
        assert inspect.iscoroutinefunction(interrupt.request_and_broadcast)

    @pytest.mark.asyncio
    async def test_flag_set_even_if_broadcast_fails(self, monkeypatch):
        """广播失败不能让本进程的中断也失效。"""
        async def failing_connect(*_a, **_kw):
            raise OSError("数据库不可达")

        monkeypatch.setattr(interrupt.asyncpg, "connect", failing_connect)
        conv = uuid.uuid4()
        await interrupt.request_and_broadcast(conv)
        assert interrupt.is_requested(conv) is True

    def test_endpoint_awaits_the_broadcast(self):
        import app.api.v1.chat as chat_api

        source = inspect.getsource(chat_api.interrupt_turn)
        assert "request_and_broadcast" in source, (
            "端点仍用 fire-and-forget 的 request()，进程在返回后立刻关闭会丢通知"
        )


class TestBroadcastPayload:
    """广播必须区分「请求」与「清理」两种消息。

    只广播请求会留下一个隐蔽的 bug：另一个进程的旗子永远清不掉，把该会话的
    **下一个**回合一启动就杀掉——正是那种「上次点了停止，之后第一条消息永远
    没反应」的现象，只是换到了跨进程版本。
    """

    def test_notify_applies_remote_request(self):
        conv = uuid.uuid4()
        interrupt._on_notify(None, 0, interrupt.CHANNEL, f"req:{conv}")
        assert interrupt.is_requested(conv) is True

    def test_notify_applies_remote_clear(self):
        conv = uuid.uuid4()
        interrupt._requested.add(conv)
        interrupt._on_notify(None, 0, interrupt.CHANNEL, f"clr:{conv}")
        assert interrupt.is_requested(conv) is False

    def test_malformed_payload_is_ignored(self):
        """脏消息不能把监听器打死——那会让整个进程失去跨进程中断能力。"""
        interrupt._on_notify(None, 0, interrupt.CHANNEL, "req:not-a-uuid")
        interrupt._on_notify(None, 0, interrupt.CHANNEL, "")
        interrupt._on_notify(None, 0, interrupt.CHANNEL, "unknown:xxx")

    def test_clear_without_flag_skips_broadcast(self, monkeypatch):
        """run_turn 的 finally 每轮都会 clear。没有旗子时还广播，等于给每次
        对话白加一条通知。"""
        sent = []

        async def spy(payload):
            sent.append(payload)

        monkeypatch.setattr(interrupt, "_publish", spy)
        asyncio.run(interrupt.clear_and_broadcast(uuid.uuid4()))
        assert sent == []

    def test_clear_with_flag_broadcasts(self, monkeypatch):
        """必须广播清理，否则另一个进程的旗子会一直留着，把该会话的下一个
        回合一启动就杀掉。"""
        sent = []

        async def spy(payload):
            sent.append(payload)

        monkeypatch.setattr(interrupt, "_publish", spy)
        conv = uuid.uuid4()
        interrupt._requested.add(conv)
        asyncio.run(interrupt.clear_and_broadcast(conv))
        assert sent == [f"clr:{conv}"]

    def test_run_turn_broadcasts_its_cleanup(self):
        """光有函数不够——run_turn 得真的用可等待版本，否则跨进程的旗子
        清不掉。"""
        import app.services.chat_service as service

        source = inspect.getsource(service.run_turn)
        assert "clear_and_broadcast" in source


class TestDsnConversion:
    def test_strips_sqlalchemy_driver_suffix(self):
        """SQLAlchemy 的 URL 带 +asyncpg，asyncpg 直连不认这个后缀。"""
        dsn = interrupt._dsn()
        assert "+asyncpg" not in dsn
        assert dsn.startswith("postgresql://")


class TestListenerLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        assert await interrupt.start_listener() is True
        try:
            assert interrupt._listener_conn is not None
        finally:
            await interrupt.stop_listener()
        assert interrupt._listener_conn is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        await interrupt.start_listener()
        try:
            first = interrupt._listener_conn
            await interrupt.start_listener()
            assert interrupt._listener_conn is first, "重复启动建了第二条连接"
        finally:
            await interrupt.stop_listener()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self):
        await interrupt.stop_listener()

    @pytest.mark.asyncio
    async def test_failure_degrades_instead_of_raising(self, monkeypatch):
        """监听起不来只告警：退化成单进程行为，比整个服务起不来好。"""
        async def failing_connect(*_a, **_kw):
            raise OSError("数据库不可达")

        monkeypatch.setattr(interrupt.asyncpg, "connect", failing_connect)
        assert await interrupt.start_listener() is False
        assert interrupt._listener_conn is None

        # 降级之后本进程内的中断仍然照常工作。
        conv = uuid.uuid4()
        interrupt._requested.add(conv)
        assert interrupt.is_requested(conv) is True


class TestCrossProcess:
    """真实的跨进程验证：一条连接发 NOTIFY，本进程的监听器收到并落到 set。

    这条是整个改动的命题——多 uvicorn worker 部署时，interrupt 请求落到哪个
    进程都要生效。
    """

    @pytest.mark.asyncio
    async def test_remote_request_reaches_this_process(self):
        import asyncpg

        conv = uuid.uuid4()
        assert await interrupt.start_listener() is True
        try:
            publisher = await asyncpg.connect(interrupt._dsn())
            try:
                # 模拟"另一个进程"发起的中断请求
                await publisher.execute(
                    "SELECT pg_notify($1, $2)", interrupt.CHANNEL, f"req:{conv}",
                )
            finally:
                await publisher.close()

            for _ in range(50):
                if interrupt.is_requested(conv):
                    break
                await asyncio.sleep(0.02)
            assert interrupt.is_requested(conv) is True, (
                "另一个进程发起的中断没有传到这里，多副本部署时点停止会失效"
            )
        finally:
            await interrupt.stop_listener()

    @pytest.mark.asyncio
    async def test_remote_clear_reaches_this_process(self):
        import asyncpg

        conv = uuid.uuid4()
        interrupt._requested.add(conv)
        assert await interrupt.start_listener() is True
        try:
            publisher = await asyncpg.connect(interrupt._dsn())
            try:
                await publisher.execute(
                    "SELECT pg_notify($1, $2)", interrupt.CHANNEL, f"clr:{conv}",
                )
            finally:
                await publisher.close()

            for _ in range(50):
                if not interrupt.is_requested(conv):
                    break
                await asyncio.sleep(0.02)
            assert interrupt.is_requested(conv) is False, (
                "远端的清理没有传到这里，该会话的下一个回合会被误杀"
            )
        finally:
            await interrupt.stop_listener()

    @pytest.mark.asyncio
    async def test_local_request_is_broadcast(self):
        """本进程点停止也要广播出去，否则跑着回合的那个进程收不到。"""
        import asyncpg

        conv = uuid.uuid4()
        received: list[str] = []
        subscriber = await asyncpg.connect(interrupt._dsn())
        try:
            await subscriber.add_listener(
                interrupt.CHANNEL,
                lambda _c, _p, _ch, payload: received.append(payload),
            )
            await interrupt.request_and_broadcast(conv)

            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.02)
            assert f"req:{conv}" in received, "本进程的中断请求没有广播出去"
        finally:
            await subscriber.close()


class TestLifespanWiring:
    def test_listener_started_in_lifespan(self):
        """光有函数不够——不在 lifespan 里启动，跨进程通道压根不工作。"""
        import app.main as main_module

        source = inspect.getsource(main_module.lifespan)
        assert "start_listener" in source
        assert "stop_listener" in source
