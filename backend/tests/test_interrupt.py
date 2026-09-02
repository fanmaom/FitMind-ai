"""进程内中断登记表。

按 conversation_id 记「这个会话请求中断了」。前端在拿到 message_start 之前
就可能点停止，那时它手上只有 conversation_id——所以键选它而不是 message id。
"""

import uuid

from app.services import interrupt


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
