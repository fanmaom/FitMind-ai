"""进程内中断登记表。

用户点「停止」时前端打一个 interrupt 请求，这里记下旗子；Agent 循环在推每个
事件之前查一次，见旗即收尾。

**键为什么是 conversation_id 而不是 message id**：前端在收到 message_start
之前就可能点停止（手快，或首个 token 迟迟不来），那时它手上只有
conversation_id。用 message id 会漏掉最该中断的那一段等待。

**为什么放进程内存而不入库**：这是一个活着不到几秒的控制信号，查询频率是
每个 token 一次。写库意味着每个 token 一条 SELECT。代价是多 uvicorn worker
部署时，interrupt 请求可能落到另一个进程而失效——当前 docker-compose 是单
进程，够用。真要横向扩容，把这里换成 PG LISTEN/NOTIFY，调用方无需改动。
"""

import uuid

# set 而非 dict：request 天然幂等，用户连点两下停止只留一面旗子。
_requested: set[uuid.UUID] = set()


def request(conversation_id: uuid.UUID) -> None:
    """标记该会话待中断。重复调用等价于调用一次。"""
    _requested.add(conversation_id)


def is_requested(conversation_id: uuid.UUID) -> bool:
    return conversation_id in _requested


def clear(conversation_id: uuid.UUID) -> None:
    """清掉旗子。没有旗子时静默返回——run_turn 的 finally 会无条件调用。"""
    _requested.discard(conversation_id)
