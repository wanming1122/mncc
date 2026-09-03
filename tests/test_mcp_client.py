"""MCP client 层：stub server 子进程回放（D8 测试策略 b，全部无真实 API）。

stub 是 `python -c` 起的一个临时脚本进程：标准库手写 framing，行为由
init_mode/call_mode 控制（'ok' 正常回应；'silent' 对该方法永不回应）。
"""

from __future__ import annotations

import sys

import pytest

from mncc.mcp.client import McpClient, McpError, McpServerConfig

_STUB_TEMPLATE = """\
import json
import sys

INIT_MODE = "__INIT_MODE__"
CALL_MODE = "__CALL_MODE__"

def read_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None if not headers else sys.exit(2)
        line = line.rstrip(b"\\r\\n")
        if not line:
            break
        key, sep, value = line.partition(b":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return json.loads(stream.read(int(headers.get(b"content-length", b"0"))))

def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\\r\\n\\r\\n" + body)
    sys.stdout.buffer.flush()

TOOL = {"name": "stub_tool", "description": "stub 工具",
        "inputSchema": {"type": "object",
                        "properties": {"say": {"type": "string"}},
                        "required": ["say"]}}

def respond(msg):
    method = msg.get("method")
    if method == "initialize":
        if INIT_MODE == "silent":
            return None
        return {"jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [TOOL]}}
    if method == "tools/call":
        if CALL_MODE == "silent":
            return None
        args = (msg.get("params") or {}).get("arguments") or {}
        if args.get("say") == "boom":
            return {"jsonrpc": "2.0", "id": msg["id"],
                    "result": {"content": [{"type": "text", "text": "boom 失败"}],
                               "isError": True}}
        text = "hello " + str(args.get("say", ""))
        return {"jsonrpc": "2.0", "id": msg["id"],
                "result": {"content": [{"type": "text", "text": text}]}}
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not found"}}

while True:
    msg = read_frame(sys.stdin.buffer)
    if msg is None:
        break
    if "id" not in msg:
        continue
    response = respond(msg)
    if response is None:
        continue
    send(response)
    if msg.get("method") == "shutdown":
        break
"""


def _stub_script(*, init_mode: str = "ok", call_mode: str = "ok") -> str:
    return _STUB_TEMPLATE.replace("__INIT_MODE__", init_mode).replace("__CALL_MODE__", call_mode)


def _client(
    *,
    init_mode: str = "ok",
    call_mode: str = "ok",
    timeout: float = 5.0,
) -> McpClient:
    cfg = McpServerConfig(
        name="stub",
        command=sys.executable,
        args=("-c", _stub_script(init_mode=init_mode, call_mode=call_mode)),
    )
    return McpClient(cfg, timeout=timeout)


def test_handshake_ok_and_list_tools() -> None:
    client = _client()
    try:
        client.connect()
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["stub_tool"]
    finally:
        client.close()


def test_call_tool_textualize() -> None:
    client = _client()
    try:
        client.connect()
        assert client.call_tool("stub_tool", {"say": "世界"}) == "hello 世界"
    finally:
        client.close()


def test_call_tool_is_error_prefix() -> None:
    client = _client()
    try:
        client.connect()
        text = client.call_tool("stub_tool", {"say": "boom"})
        assert text == "[error] boom 失败"
    finally:
        client.close()


def test_request_timeout_raises_mcp_error() -> None:
    # call_mode=silent：tools/call 永不回应 → 单请求超时（D5）。
    # timeout=2.0：握手要有充足余量（慢 CI 上 0.5s 可能不够），只让 call 超时。
    client = _client(call_mode="silent", timeout=2.0)
    try:
        client.connect()
        with pytest.raises(McpError, match="超时"):
            client.call_tool("stub_tool", {"say": "x"})
    finally:
        client.close()


def test_handshake_timeout_raises_mcp_error() -> None:
    # init_mode=silent：initialize 永不回应 → 握手超时
    client = _client(init_mode="silent", timeout=0.5)
    with pytest.raises(McpError, match="超时"):
        client.connect()
    # close 仍要发 shutdown（stub 会响应）→ 快速且无僵尸
    client.close()
    assert not client.alive


def test_startup_failure_raises_mcp_error() -> None:
    cfg = McpServerConfig(name="ghost", command="definitely-not-a-real-cmd-xyz")
    client = McpClient(cfg)
    with pytest.raises(McpError, match="启动失败"):
        client.connect()
    client.close()  # 启动即失败：close 应是无害的


def test_close_terminates_subprocess() -> None:
    client = _client()
    client.connect()
    assert client.alive is True
    client.close()
    assert client.alive is False
    # 子进程真正退出，不留僵尸
    assert client._proc.poll() is not None  # type: ignore[union-attr]


def test_connect_idempotent() -> None:
    client = _client()
    client.connect()
    client.connect()  # 已连接：直接返回，不重复握手
    assert client.alive
    client.close()


def test_close_before_connect_is_noop() -> None:
    client = _client()
    client.close()
    assert client.alive is False
