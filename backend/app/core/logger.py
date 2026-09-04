"""日志配置。

默认写 stdout。但有一个例外必须处理：MCP stdio 服务端把 stdout 当作协议通道
（JSON-RPC 消息逐行传输），任何一行日志混进去都会让客户端解析失败。

那种失败很难查——客户端只会报 "unexpected token"，而问题出在一条毫不相关的
INFO 日志上。所以提供 route_to_stderr()，由 stdio 入口在**任何业务代码执行之前**
调用一次。
"""

import sys

from loguru import logger

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

logger.remove()
logger.add(sys.stdout, level="INFO", format=_FORMAT)


def route_to_stderr(level: str = "INFO") -> None:
    """把日志改道 stderr。

    给 MCP stdio 服务端用：那里 stdout 是协议通道，写日志进去等于往数据流里
    插脏字节。MCP 规范也明确要求 server 把日志写 stderr。

    必须在任何业务代码执行之前调用——模块导入阶段就可能打日志（工具注册表在
    load_tools 里就会写一行），晚了就已经污染了。
    """
    logger.remove()
    logger.add(sys.stderr, level=level, format=_FORMAT)


__all__ = ["logger", "route_to_stderr"]
