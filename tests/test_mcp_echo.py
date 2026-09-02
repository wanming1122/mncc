"""M6 验收：自带 echo server 端到端（D8 测试策略 c，无需外部工具）。

`python -m mncc.mcp.echo_server` 起一个真实子进程，走完整握手 →
list → call → close 链路。
"""

from __future__ import annotations

import sys

from mncc.mcp.client import McpClient, McpServerConfig
from mncc.mcp.echo_server import TOOL_SPEC


def _echo_client() -> McpClient:
    cfg = McpServerConfig(
        name="echo",
        command=sys.executable,
        args=("-m", "mncc.mcp.echo_server"),
    )
    return McpClient(cfg, timeout=5.0)


def test_echo_end_to_end() -> None:
    client = _echo_client()
    try:
        client.connect()
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
        assert tools[0]["inputSchema"] == TOOL_SPEC["inputSchema"]
        assert client.call_tool("echo", {"say": "你好"}) == "你好"
        # 非字符串参数 → echo server 返回 isError → [error] 前缀
        assert client.call_tool("echo", {"say": 123}) == "[error] 参数 say 必须是字符串"
    finally:
        client.close()
    assert client.alive is False


def test_echo_handles_notification_and_shutdown() -> None:
    # initialize 之后 client 会发 notifications/initialized（通知不期待响应）；
    # shutdown 应正常关闭进程。整条链路不抛错即通过。
    client = _echo_client()
    client.connect()
    assert client.alive is True
    client.close()
    assert client.alive is False
