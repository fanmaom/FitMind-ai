"""MCP（Model Context Protocol）客户端。

让这个项目能挂载外部 MCP server 提供的工具——查天气、读日历、搜文献，都不必
在本仓库写代码。

## 只做 stdio，不做 HTTP/SSE

MCP 规范定义了两种传输：stdio（子进程）和 HTTP+SSE。这里只实现 stdio，理由是
绝大多数现成的 MCP server 都以 stdio 形式分发（`npx -y @xxx/server`），而 HTTP
形式需要额外部署一个服务、配鉴权、管生命周期——为一个还没有实际需求的传输方式
引入这些，不如等真的需要时再加。传输层在 `MCPClient` 里是独立的一段，将来加
HTTP 只需换掉 `_open` 和 `_rpc` 的收发。

## stdio 上的 JSON-RPC 有个容易踩的坑

消息以换行分隔（每条一行 JSON），但**子进程的 stdout 是流，不保证一次 read 恰好
读到一整行**。必须用 `readline()` 而不是 `read(n)`——后者会把两条消息的片段粘在
一起，json.loads 直接报错，而错误信息（"Extra data"）完全指不到真正的原因。

另一个坑：server 的 stderr 必须单独接管。MCP server 常把日志写 stderr，如果
不读它，管道缓冲区满了之后子进程会**阻塞在写日志上**，表现为工具调用随机超时。

## 失败一律降级，不阻塞启动

外部 server 是不可控的：npx 可能没装、网络可能不通、server 自己可能崩。任何
失败都只记日志然后跳过这个 server——用户宁可少几个工具，也不能因为一个可选的
外部依赖连不上就整个服务起不来。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from app.core.logger import logger

# MCP 协议版本。写死一个已知可用的值而不是取最新：协议还在演进，
# 让 server 按我们声明的版本来协商，比盲目跟最新更稳。
PROTOCOL_VERSION = "2024-11-05"

# 单次 RPC 的超时。tools/list 在握手阶段，慢一点可以接受；tools/call 在用户等
# 回复的路径上，由调用方传更短的值。
DEFAULT_RPC_TIMEOUT_S = 20.0

# 子进程启动后等 initialize 响应的时间。npx 首次运行要下载包，给足余量。
INIT_TIMEOUT_S = 60.0


class MCPError(RuntimeError):
    """与 MCP server 通信失败。"""


@dataclass
class MCPServerConfig:
    """一个 MCP server 的启动配置。

    对齐 Claude Desktop / Cursor 的 mcpServers 格式，用户可以直接把现成的配置
    片段贴过来，不必学一套新写法。
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # 这个 server 的工具是否只读。MCP 协议本身**不表达副作用**——
    # readOnlyHint 是 2025 年才加的可选注解，绝大多数 server 不提供。
    # 所以默认 False（当写操作对待）：降级层据此决定能否安全重试，
    # 误判成只读会导致重复写入。要标只读必须由配置显式声明。
    readonly: bool = False

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> MCPServerConfig:
        command = str(raw.get("command", "")).strip()
        if not command:
            raise ValueError(f"MCP server「{name}」缺少 command")
        return cls(
            name=name,
            command=command,
            args=[str(a) for a in raw.get("args", [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            readonly=bool(raw.get("readonly", False)),
        )


@dataclass(frozen=True)
class MCPTool:
    """server 声明的一个工具。"""

    name: str
    description: str
    input_schema: dict


class MCPClient:
    """一个 server 一个实例。持有子进程，进程内长驻。

    不做连接池：MCP server 是有状态的子进程，握手一次之后要一直用同一个连接。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        # 串行化 RPC。stdio 上的响应靠 id 匹配，但并发写入会让两条请求的字节
        # 交错——JSON-RPC 没有帧长度头，交错之后整条流就废了。
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """启动子进程并完成 initialize 握手。"""
        if self.alive:
            return

        # 先查命令存在。不查的话报错是 FileNotFoundError，消息里只有命令名，
        # 看不出是"没装"还是"路径不对"。
        if shutil.which(self.config.command) is None:
            raise MCPError(
                f"找不到命令 `{self.config.command}`。"
                f"如果是 npx 形式的 server，需要先装 Node.js。",
            )

        # 继承当前环境再叠加配置里的 env：MCP server 常需要 PATH、HOME，
        # 只给配置里那几个变量会让它起不来。
        env = {**os.environ, **self.config.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.config.command, *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise MCPError(f"启动 MCP server 失败：{exc}") from exc

        # 单独接管 stderr。不读的话管道缓冲区满了之后子进程会阻塞在写日志上，
        # 表现为工具调用随机超时——一个极难定位的故障。
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            await asyncio.wait_for(self._handshake(), timeout=INIT_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise MCPError(
                f"MCP server「{self.config.name}」握手超时（{INIT_TIMEOUT_S}s）。"
                f"npx 首次运行需要下载包，可以先手动跑一次预热。",
            ) from exc
        except Exception:
            await self.close()
            raise

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            try:
                line = await self._proc.stderr.readline()
            except Exception:  # noqa: BLE001 - 进程关闭时读取会抛，正常路径
                return
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                # server 的日志降级成 debug：它们往往很啰嗦，而且不是我们的故障。
                logger.debug(f"[mcp:{self.config.name}] {text}")

    async def _handshake(self) -> None:
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fitmind", "version": "1.0"},
        })
        server = (result or {}).get("serverInfo", {})
        logger.info(
            f"MCP server「{self.config.name}」已连接："
            f"{server.get('name', '?')} {server.get('version', '')}",
        )
        # initialized 是通知（无 id、无响应）。漏发的话部分 server 会拒绝
        # 后续所有请求，报的还是很含糊的 "not initialized"。
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPTool]:
        result = await self._rpc("tools/list", {})
        tools = []
        for raw in (result or {}).get("tools", []):
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            tools.append(MCPTool(
                name=name,
                description=str(raw.get("description", "")).strip(),
                # 键名兼容：规范是 inputSchema，个别实现写成 input_schema。
                input_schema=raw.get("inputSchema") or raw.get("input_schema") or {},
            ))
        return tools

    async def call_tool(
        self, name: str, arguments: dict, timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
    ) -> str:
        """调用工具，返回给模型看的文本。

        MCP 的返回是 content 数组（可能含 text / image / resource）。这里只取
        text 并拼起来——模型上下文里放不了图片，而 resource 需要二次拉取，
        那属于将来的事。
        """
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s,
        )
        result = result or {}

        # isError 是 MCP 表达"工具自己失败了"的方式，和 JSON-RPC 层的错误不同。
        # 不识别它会把失败当成成功结果回灌给模型。
        parts = [
            str(item.get("text", ""))
            for item in result.get("content", [])
            if item.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p).strip()
        if result.get("isError"):
            raise MCPError(text or "工具执行失败")
        return text or "（工具没有返回内容）"

    async def _rpc(
        self, method: str, params: dict, timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
    ) -> dict | None:
        async with self._lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                raise MCPError("MCP server 未启动")

            self._next_id += 1
            request_id = self._next_id
            payload = {
                "jsonrpc": "2.0", "id": request_id,
                "method": method, "params": params,
            }
            await self._write(payload)

            # 循环读到 id 匹配为止：server 可能在响应之前先推送通知
            # （日志、进度），那些没有 id，不能当成响应。
            deadline = asyncio.get_running_loop().time() + timeout_s
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise MCPError(f"{method} 超时（{timeout_s}s）")
                message = await asyncio.wait_for(self._read(), timeout=remaining)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    err = message["error"] or {}
                    raise MCPError(
                        f"{method} 失败：{err.get('message', err)}",
                    )
                return message.get("result")

    async def _notify(self, method: str, params: dict) -> None:
        async with self._lock:
            await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MCPError("MCP server 已退出（管道断开）") from exc

    async def _read(self) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        # 必须用 readline：stdout 是流，read(n) 会把两条消息的片段粘在一起，
        # 而 json.loads 报的 "Extra data" 完全指不到真正的原因。
        line = await self._proc.stdout.readline()
        if not line:
            code = self._proc.returncode
            raise MCPError(f"MCP server 已退出（returncode={code}）")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            # 有些 server 会往 stdout 打非协议内容（启动横幅之类）。跳过而不是
            # 报错——报错会让整个 server 不可用，而它可能只是多打了一行。
            logger.warning(
                f"[mcp:{self.config.name}] stdout 出现非 JSON 内容，已跳过："
                f"{line[:120]!r}（{exc}）",
            )
            return {}

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"MCP server「{self.config.name}」未响应 terminate，强制杀掉")
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass


def parse_servers(raw: Any) -> list[MCPServerConfig]:
    """解析配置。

    接受两种形状：
      {"mcpServers": {"weather": {...}}}   —— Claude Desktop / Cursor 的格式
      {"weather": {...}}                   —— 省掉外层
    这样用户能直接把现成配置贴过来。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MCP 配置不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("MCP 配置必须是对象")

    servers = raw.get("mcpServers") if "mcpServers" in raw else raw
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 必须是对象")

    result = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise ValueError(f"MCP server「{name}」的配置必须是对象")
        result.append(MCPServerConfig.from_dict(str(name), spec))
    return result
