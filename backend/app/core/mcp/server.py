"""MCP 服务端：把这个助理的工具暴露给外部 MCP 客户端。

配好之后，用户可以在 Claude Desktop / Cursor 里直接问"我上次卧推多少"、
"我这周该吃多少"——数据还在这个系统里，只是多了一个入口。

## 与 client.py 是相反方向的两件事

client.py 是**主动方**：起子进程、发 tools/list、发 tools/call。
这里是**被动方**：读 stdin 上别人发来的请求，回响应。协议消息名一样，
但代码几乎没有可复用的，所以是独立一个文件。

## 三个必须处理好的问题

**1. stdout 是协议通道，不能写日志。**
MCP stdio 用 stdout 逐行传 JSON-RPC。混进一行 INFO 日志，客户端就报
"unexpected token"——而问题出在一条毫不相关的日志上，极难定位。
入口在导入业务代码之前就调用 route_to_stderr()。

**2. 身份从哪来。**
MCP stdio 是单用户本地进程模型，没有登录概念。而这个系统里每个工具都要
user_id——数据隔离靠 PG RLS 强制，没有它一行都读不到。

解法是从环境变量读一个已签发的 access token，启动时换成 user_id 并绑定 RLS
上下文。token 放配置文件里是这个方案的代价，README 里写明了。

**3. 只暴露只读工具。**
写操作一律不给。理由不是"怕出 bug"，而是**风险与收益不对称**：
- 收益侧：外部客户端要的是"查我的数据"，写操作在对话式界面里本来就该由
  本系统自己的 UI 承担（那里有确认流程、有卡片反馈、有撤销的余地）。
- 风险侧：token 明文躺在配置文件里，泄漏后果是"数据被改"还是"数据被读"，
  差别很大。

白名单按 ToolSpec.readonly 自动筛，不手写名单——手写的那种迟早和新增工具脱节。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any, TextIO

from app.core.logger import logger

# 与 client.py 保持同一个协议版本。
PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "fitmind"
SERVER_VERSION = "1.0"

# JSON-RPC 标准错误码。用标准码而不是自定义：客户端据此决定要不要重试、
# 要不要提示用户，自定义码它只能当成未知错误。
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class MCPServer:
    """一个进程一个实例，服务一个用户。

    不做多用户：stdio 传输本身就是一个客户端一个子进程，进程内混多个用户
    只会让 RLS 上下文变得难以推理。
    """

    def __init__(self, user_id: uuid.UUID, readonly_only: bool = True) -> None:
        self.user_id = user_id
        self.readonly_only = readonly_only
        self._initialized = False

    # ── 工具清单 ────────────────────────────────────────────────────────

    def _exposed_specs(self) -> list:
        from app.core.mcp.bridge import split_name
        from app.core.tools.registry import registry

        specs = []
        for spec in registry.all():
            # 不转发外部 MCP 工具。它们是这个进程从别的 server 借来的，
            # 再转出去会形成一条谁也说不清的调用链，而且那些 server 的
            # 副作用我们无从判断。
            if split_name(spec.name) is not None:
                continue
            if self.readonly_only and not spec.readonly:
                continue
            specs.append(spec)
        return specs

    def _tools_payload(self) -> list[dict]:
        return [
            {
                "name": spec.name,
                # 把用户可见说法放进描述开头。外部客户端的模型只看 description，
                # 它需要知道这些工具属于哪个系统，否则会拿 calc_macros 去算
                # 与健身无关的东西。
                "description": f"[{SERVER_NAME}] {spec.label}。{spec.description}",
                "inputSchema": spec.input_model.model_json_schema(),
            }
            for spec in self._exposed_specs()
        ]

    # ── 请求分发 ────────────────────────────────────────────────────────

    async def handle(self, message: dict) -> dict | None:
        """处理一条消息。返回 None 表示这是通知，不需要回响应。"""
        method = message.get("method")
        request_id = message.get("id")

        # 通知没有 id，不能回响应——回了客户端会当成协议错误。
        if request_id is None:
            if method == "notifications/initialized":
                self._initialized = True
            return None

        if method == "initialize":
            self._initialized = True
            return self._ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })

        if method == "tools/list":
            return self._ok(request_id, {"tools": self._tools_payload()})

        if method == "tools/call":
            return await self._call(request_id, message.get("params") or {})

        # ping 是规范里的保活方法，回空对象即可。不认它的话有些客户端会
        # 判定连接已死。
        if method == "ping":
            return self._ok(request_id, {})

        return self._err(request_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")

    async def _call(self, request_id: Any, params: dict) -> dict:
        from app.core.tools.registry import (
            ToolNotFoundError,
            ToolValidationError,
            registry,
        )

        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}

        # 白名单检查必须在 invoke 之前，而且要基于同一份 _exposed_specs：
        # 只靠"注册表里有"是不够的，那会把写操作也放进来。
        allowed = {spec.name for spec in self._exposed_specs()}
        if name not in allowed:
            # 不区分"不存在"和"不允许"——区分了就等于告诉调用方
            # "这个工具存在但你不能用"，是一条不必要的信息泄漏。
            return self._tool_error(request_id, f"没有可用的工具：{name}")

        session = None
        try:
            session, ctx = await self._make_context()
            result = await registry.invoke(name, arguments, ctx)
            await session.commit()
        except ToolValidationError as exc:
            # 参数错误回 isError 而不是 JSON-RPC error：这是"工具执行失败"，
            # 客户端的模型应该看到它并自行改参数重试。回 JSON-RPC error 会被
            # 当成传输层故障。
            return self._tool_error(request_id, str(exc))
        except ToolNotFoundError:
            return self._tool_error(request_id, f"没有可用的工具：{name}")
        except Exception as exc:  # noqa: BLE001
            # 日志里留全貌，回给客户端的只有一句话——异常里可能带 SQL 片段、
            # 表名、连接串。
            logger.exception(f"MCP 工具执行失败：{name}")
            return self._tool_error(request_id, f"执行失败：{type(exc).__name__}")
        finally:
            if session is not None:
                await session.close()

        # 卡片是本系统前端的东西，对外部客户端没有意义，去掉。
        if isinstance(result, dict):
            result.pop("__card__", None)
        text = json.dumps(result, ensure_ascii=False, default=str, indent=2)
        return self._ok(request_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        })

    async def _make_context(self):
        """开一个绑好 RLS 的 session。

        每次调用一个新 session 而不是复用：MCP server 是长驻进程，一个 session
        用几小时会累积未回滚的事务状态，而这里没有 Web 框架的请求边界来兜底。
        """
        from sqlalchemy import text

        from app.core.database import async_session_maker, bind_rls_user
        from app.core.tools.registry import ToolContext

        session = async_session_maker()
        bind_rls_user(session, self.user_id)
        # 触发 autobegin，让 RLS 上下文立即生效——与 api/deps.py 同一套做法。
        await session.execute(text("SELECT 1"))
        return session, ToolContext(user_id=self.user_id, session=session)

    # ── JSON-RPC 帧 ─────────────────────────────────────────────────────

    @staticmethod
    def _ok(request_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _err(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_error(request_id: Any, message: str) -> dict:
        """工具层失败：走 result.isError，不走 JSON-RPC error。"""
        return MCPServer._ok(request_id, {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        })

    # ── 主循环 ──────────────────────────────────────────────────────────

    async def serve(self, reader: asyncio.StreamReader, writer: TextIO) -> None:
        """读一行、处理、回一行，直到 stdin 关闭。

        writer 是同步的文本流（实际就是 sys.stdout），不是 asyncio 的
        StreamWriter。stdout 的写入量很小（一条 JSON），同步写不会阻塞事件循环，
        而包成 StreamWriter 要多接一个 pipe，没有收益。
        """
        while True:
            line = await reader.readline()
            if not line:
                logger.info("stdin 已关闭，MCP 服务端退出")
                return

            stripped = line.strip()
            if not stripped:
                continue

            try:
                message = json.loads(stripped)
            except json.JSONDecodeError as exc:
                # 解析失败也要回，否则客户端会一直等。id 用 None——
                # 我们压根不知道它是几。
                self._write(writer, self._err(None, PARSE_ERROR, f"JSON 解析失败：{exc}"))
                continue

            if not isinstance(message, dict):
                self._write(writer, self._err(None, INVALID_REQUEST, "请求必须是对象"))
                continue

            try:
                response = await self.handle(message)
            except Exception as exc:  # noqa: BLE001
                # 兜到最外层：单条消息处理失败不能让整个 server 退出，
                # 那会让客户端失去所有工具。
                logger.exception("MCP 消息处理异常")
                response = self._err(
                    message.get("id"), INTERNAL_ERROR, f"内部错误：{type(exc).__name__}",
                )
            if response is not None:
                self._write(writer, response)

    @staticmethod
    def _write(writer: TextIO, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        writer.write(line)
        # 必须显式 flush。stdout 接管道时是块缓冲的，不 flush 响应会攒在缓冲区里
        # 直到进程退出——客户端那边表现为"发了请求没有任何回应"，然后超时。
        writer.flush()


async def _stdin_reader() -> asyncio.StreamReader:
    """把 sys.stdin 包成 asyncio 的 StreamReader。

    不直接用 input()：那是阻塞调用，会把事件循环卡住，导致工具里的
    await（数据库查询）永远不返回。
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin,
    )
    return reader


async def run_stdio(token: str, *, readonly_only: bool = True) -> int:
    """入口：校验 token，然后在 stdio 上服务。返回进程退出码。"""
    from app.core.security import decode_token
    from app.core.tools.registry import load_tools

    try:
        user_id = uuid.UUID(decode_token(token))
    except ValueError as exc:
        # 这条必须写 stderr（logger 已改道），并且不能把 token 打出来。
        logger.error(f"FITMIND_TOKEN 无效：{exc}")
        return 2

    load_tools()
    server = MCPServer(user_id, readonly_only=readonly_only)
    exposed = [s.name for s in server._exposed_specs()]  # noqa: SLF001
    logger.info(
        f"MCP 服务端启动：user={user_id} "
        f"暴露 {len(exposed)} 个{'只读' if readonly_only else ''}工具 {exposed}",
    )

    reader = await _stdin_reader()
    await server.serve(reader, sys.stdout)
    return 0
