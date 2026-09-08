"""MCP stdio 服务端入口。

在 Claude Desktop / Cursor 的配置里这样挂：

    {
      "mcpServers": {
        "fitmind": {
          "command": "python",
          "args": ["-m", "app.mcp_stdio"],
          "cwd": "/绝对路径/backend",
          "env": {
            "FITMIND_TOKEN": "<登录后拿到的 access token>",
            "DATABASE_URL": "postgresql+asyncpg://...",
            "JWT_SECRET": "<与服务端一致>"
          }
        }
      }
    }

## DATABASE_URL 必须指向真正存着数据的那个库

这是独立进程，不走 API 容器。填错库不会报错，只会让每个工具静静地返回空结果——
所以启动时会先自检「这个库里有没有这个用户」，不满足就退出（退出码 3），
而不是带着一个只会说「你还没有任何记录」的假象跑起来。

## 为什么日志改道是这个文件里的第一件事

MCP stdio 用 **stdout 传 JSON-RPC**。而 app.core.logger 默认写 stdout，且
`load_tools()` 在注册工具时就会打一行 INFO——那一行会直接混进协议流，客户端
只会报 "unexpected token"，而真正的原因是一条毫不相关的日志。

所以 route_to_stderr() 必须在**导入任何业务模块之前**执行。下面那几个 import
的位置是刻意的，不要为了"整洁"把它们提到文件顶部。
"""

import os
import sys

# ruff: noqa: E402 - 见上文：日志改道必须先于业务模块导入
from app.core.logger import route_to_stderr

route_to_stderr()

import asyncio

from app.core.logger import logger
from app.core.mcp.server import EXIT_BAD_TOKEN, run_stdio

# 环境变量名。用 FITMIND_ 前缀而不是复用 JWT_*：这是"以某个用户身份运行"的
# 凭据，与服务端的签名密钥是两件不同的东西，混在一起容易配错。
TOKEN_ENV = "FITMIND_TOKEN"

# 是否允许写操作。默认只读——token 明文躺在客户端配置文件里，泄漏后果是
# "数据被改"还是"数据被读"，差别很大。
# 要开写操作必须显式设 FITMIND_MCP_ALLOW_WRITE=1，并且清楚这意味着什么。
ALLOW_WRITE_ENV = "FITMIND_MCP_ALLOW_WRITE"


def main() -> int:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        logger.error(
            f"缺少 {TOKEN_ENV}。先在应用里登录，拿到 access token 后写进 "
            f"MCP 客户端配置的 env 里。",
        )
        return EXIT_BAD_TOKEN

    allow_write = os.environ.get(ALLOW_WRITE_ENV, "").strip() in ("1", "true", "yes")
    if allow_write:
        logger.warning(
            "已开启写操作。外部客户端将能修改档案、写训练记录——"
            "确认这个 token 只在你自己的机器上使用。",
        )

    try:
        return asyncio.run(run_stdio(token, readonly_only=not allow_write))
    except KeyboardInterrupt:
        # 客户端关闭子进程时的正常路径，不该打成错误。
        return 0


if __name__ == "__main__":
    sys.exit(main())
