"""M6 验收/测试用的最小 MCP server（纯标准库，D8）。

`python -m mncc.mcp.echo_server` 启动：stdio 循环读帧，逐个响应：
- initialize          → protocolVersion=2024-11-05, capabilities={}
- notifications/initialized → 通知，不响应
- tools/list          → 一个工具 echo(say: string)
- tools/call          → 回显 say 参数（isError 或 content）
- shutdown            → 空结果后退出

作用：单测端到端 + 用户本机验收（无需 npx/Node）；同时是"手写 MCP server"
的参考实现，与 client 共用同一套 framing。
"""

from __future__ import annotations

import sys
from typing import Any

from .protocol import ProtocolError, decode_message, encode_message

TOOL_SPEC: dict[str, Any] = {
    "name": "echo",
    "description": "把参数 say 的内容原样回显给你（用于验证 MCP 通路）。",
    "inputSchema": {
        "type": "object",
        "properties": {"say": {"type": "string", "description": "要回显的文本"}},
        "required": ["say"],
    },
}


def _handle(msg: dict[str, Any]) -> dict[str, Any]:
    method = msg.get("method")
    _id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "mncc-echo", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL_SPEC]}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != TOOL_SPEC["name"]:
            result = {
                "content": [{"type": "text", "text": f"未知工具：{name!r}"}],
                "isError": True,
            }
        else:
            text = arguments.get("say", "")
            if not isinstance(text, str):
                result = {
                    "content": [{"type": "text", "text": "参数 say 必须是字符串"}],
                    "isError": True,
                }
            else:
                result = {"content": [{"type": "text", "text": text}]}
    elif method == "shutdown":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": _id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": _id, "result": result}


def main() -> int:
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while True:
        try:
            msg = decode_message(reader)
        except ProtocolError as exc:
            print(f"[echo_server] 帧错误，退出：{exc}", file=sys.stderr)
            return 1
        if msg is None:
            return 0
        if "id" not in msg:
            continue  # 通知（notifications/initialized）无需响应
        response = _handle(msg)
        writer.write(encode_message(response))
        writer.flush()
        if msg.get("method") == "shutdown":
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
