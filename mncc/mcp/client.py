"""MCP 客户端（M6 D1–D5）：手写 JSON-RPC over stdio 最小子集，不引入 mcp SDK。

架构要点：
- 每台配置的 server 一个子进程（Popen stdin/stdout 管道）；一个守护 reader 线程
  持续读帧放入队列，请求/响应按 id 关联——跨平台（Windows 管道没有 select）且
  天然支持"单请求超时"（D5：queue.get 带超时）。
- McpTool 复用现有 Tool 抽象（tools/base.py）：注册进同一 registry 即自动具备
  function calling + 确认门禁 + 错误回填，MCP 不是另一套体系（§7 面试点）。
- D3：远端工具 needs_confirm 默认 True；D4：连接失败的 server 跳过不拖垮主流程；
  D5：close 先发 shutdown(5s) 再 terminate+wait 防僵尸。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from ..tools.base import Tool, ToolRegistry
from .protocol import (
    ProtocolError,
    decode_message,
    encode_message,
    make_notification,
    make_request,
)

__all__ = [
    "McpClient",
    "McpError",
    "McpServerConfig",
    "McpTool",
    "attach_mcp_tools",
]

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_NAME = "mncc"
_CLIENT_VERSION = "0.1.0"
_SHUTDOWN_TIMEOUT = 5.0  # D5：close 的优雅退出宽限


@dataclass(frozen=True)
class McpServerConfig:
    """cli 层把 Config.mcp_servers（原始 tuple[dict]）转换后的客户端可消费结构。"""

    name: str
    command: str
    args: tuple[str, ...] = ()


class McpError(Exception):
    """连接/协议/超时统一出口，message 面向模型可读（经 registry 转 is_error 回填）。"""


@dataclass
class _InboxItem:
    """reader 线程投递的消息或终止原因。msg=None 表示对端已关/协议错误。"""

    msg: dict | None
    error: str | None = None


class McpClient:
    """一个 MCP server 子进程的生命周期管理 + 同步请求（D5：一问一答）。"""

    def __init__(self, cfg: McpServerConfig, *, timeout: float = 30.0) -> None:
        self._cfg = cfg
        self._timeout = timeout
        self.server = cfg.name
        self._proc: subprocess.Popen | None = None
        self._stdin: object = None
        self._stdout: object = None
        self._messages: queue.Queue[_InboxItem] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._counter = 0

    # ---- 生命周期（D4/D5）----

    def connect(self) -> None:
        """Popen → initialize（版本协商）→ notifications/initialized。失败抛 McpError。"""
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                [self._cfg.command, *self._cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise McpError(
                f"server {self._cfg.name!r} 进程启动失败（{self._cfg.command!r}）：{exc}"
            ) from exc
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"mcp-{self._cfg.name}", daemon=True
        )
        self._reader.start()

        _id = self._next_id()
        self._send(
            make_request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                },
                _id,
            )
        )
        resp = self._recv(_id)
        if "error" in resp:
            raise McpError(f"server {self._cfg.name!r} 拒绝 initialize：{resp['error']}")
        result = resp.get("result") or {}
        if result.get("protocolVersion") != _PROTOCOL_VERSION:
            raise McpError(
                f"server {self._cfg.name!r} 协议版本不匹配："
                f"对端 {result.get('protocolVersion')!r}，需要 {_PROTOCOL_VERSION}"
            )
        self._send(make_notification("notifications/initialized", {}))

    def close(self) -> None:
        """shutdown（带 5s 超时）→ terminate → wait，防僵尸（D5）。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            shutdown_id = self._next_id()
            self._send(make_request("shutdown", {}, shutdown_id))
            self._recv(shutdown_id, timeout=_SHUTDOWN_TIMEOUT)
        except McpError:
            pass  # 对端不响应 shutdown：交给 terminate 强收
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---- 协议方法（MCP 2024-11-05 最小子集）----

    def list_tools(self) -> list[dict]:
        """-> [{name, description, inputSchema}]。"""
        _id = self._next_id()
        self._send(make_request("tools/list", {}, _id))
        resp = self._recv(_id)
        if "error" in resp:
            raise McpError(f"server {self._cfg.name!r} tools/list 失败：{resp['error']}")
        return list((resp.get("result") or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """tools/call，结果序列化成可读文本（§3.2 规则）。"""
        _id = self._next_id()
        self._send(make_request("tools/call", {"name": name, "arguments": arguments}, _id))
        resp = self._recv(_id)
        if "error" in resp:
            raise McpError(f"工具 {name} 调用失败：{resp['error']}")
        result = resp.get("result") or {}
        is_error = bool(result.get("isError"))
        texts = [
            block.get("text", "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        ]
        if texts:
            text = "\n".join(texts)
        elif result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"], ensure_ascii=False)
        else:
            text = json.dumps(result, ensure_ascii=False)
        return ("[error] " if is_error else "") + text

    # ---- 内部：帧收发（reader 线程 + 按 id 关联）----

    def _send(self, payload: dict) -> None:
        if not self.alive:
            raise McpError(f"server {self._cfg.name!r} 已退出，无法发送请求")
        try:
            self._stdin.write(encode_message(payload))  # type: ignore[union-attr]
            self._stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"写管道失败（server {self._cfg.name!r} 可能已退出）：{exc}") from exc

    def _recv(self, _id: int, timeout: float | None = None) -> dict:
        """按 id 从队列取响应，超时抛 McpError（D5：单请求超时）。"""
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"等待响应超时（{timeout:g}s）：server {self._cfg.name!r}")
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise McpError(f"等待响应超时（{timeout:g}s）：server {self._cfg.name!r}") from None
            if item.msg is None:
                raise McpError(item.error or f"server {self._cfg.name!r} 连接已关闭")
            if item.msg.get("id") == _id:
                return item.msg

    def _reader_loop(self) -> None:
        """持续读帧；EOF/协议错误 → 投递终止哨兵，让 _recv 立刻报错。"""
        error: str | None = None
        try:
            while True:
                msg = decode_message(self._stdout)  # type: ignore[arg-type]
                if msg is None:
                    break
                self._messages.put(_InboxItem(msg=msg))
        except ProtocolError as exc:
            error = f"帧解码失败（server {self._cfg.name!r}）：{exc}"
        except Exception as exc:  # 兜底：reader 线程绝不能静默死亡
            error = f"读帧异常（server {self._cfg.name!r}）：{type(exc).__name__}: {exc}"
        finally:
            self._messages.put(_InboxItem(msg=None, error=error))

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter


class McpTool(Tool):
    """MCP 远端工具的本地适配：动态命名 + 透传 description/schema（D3）。

    run() 代理转发 tools/call；传输层失败（超时/管道断）抛 McpError，
    由 registry.execute 转 is_error 回填，模型可见并自纠。
    """

    def __init__(self, client: McpClient, server: str, tool_name: str, spec: dict) -> None:
        self._client = client
        self._server = server
        self._tool = tool_name
        self.name = f"mcp__{server}__{tool_name}"
        self.description = spec.get("description") or ""
        schema = spec.get("inputSchema")
        if isinstance(schema, dict) and schema.get("type") == "object":
            self.schema = schema
        else:
            # 兜底：部分 server 不返回 inputSchema，给一个空对象避免模型误传
            self.schema = {"type": "object", "properties": {}}

    def run(self, **kwargs: object) -> str:
        return self._client.call_tool(self._tool, dict(kwargs))

    def needs_confirm(self, args: dict) -> bool:
        return True  # D3：远端副作用对用户是黑盒，REPL 下先预览确认


def attach_mcp_tools(registry: ToolRegistry, servers: list[McpServerConfig]) -> list[McpClient]:
    """逐台 connect + list_tools + 注册（D4）。

    任何一台失败（启动失败/握手超时/协议错误）只在 stderr 打一条警告并跳过，
    其余照常可用；返回成功连接的 client 列表供调用方统一 close。
    """
    connected: list[McpClient] = []
    for cfg in servers:
        client = McpClient(cfg)
        try:
            client.connect()
            tools = client.list_tools()
        except McpError as exc:
            print(f"[mcp] 跳过 server {cfg.name!r}：{exc}", file=sys.stderr)
            client.close()
            continue
        for spec in tools:
            tool_name = spec.get("name")
            if not tool_name or not isinstance(tool_name, str):
                continue
            registry.register(McpTool(client, cfg.name, tool_name, spec))
        connected.append(client)
    return connected
