"""McpTool 与 ToolRegistry 集成（D3）：命名、schema 透传、确认门禁、错误回填。

远端工具复用本地 Tool 抽象——注册进同一 registry 即自动具备 function calling、
确认门禁与错误回填，MCP 不是另一套体系（§7 面试点）。
"""

from __future__ import annotations

import sys

from mncc.mcp.client import (
    McpError,
    McpServerConfig,
    McpTool,
    attach_mcp_tools,
)
from mncc.tools.base import ToolRegistry

SPEC = {
    "name": "stub_tool",
    "description": "一个测试工具",
    "inputSchema": {
        "type": "object",
        "properties": {"say": {"type": "string"}},
        "required": ["say"],
    },
}


def test_mcp_tool_name_and_schema_passthrough() -> None:
    tool = McpTool(client=None, server="echo", tool_name="stub_tool", spec=SPEC)  # type: ignore[arg-type]
    assert tool.name == "mcp__echo__stub_tool"
    assert tool.description == "一个测试工具"
    assert tool.schema == SPEC["inputSchema"]


def test_mcp_tool_needs_confirm_default_true() -> None:
    tool = McpTool(None, "echo", "stub_tool", SPEC)  # type: ignore[arg-type]
    assert tool.needs_confirm({}) is True  # D3：远端副作用默认确认


def test_mcp_tool_missing_schema_defaults_empty_object() -> None:
    tool = McpTool(None, "s", "t", {"name": "t", "description": ""})  # type: ignore[arg-type]
    assert tool.schema == {"type": "object", "properties": {}}


def test_mcp_tool_error_backfills_is_error() -> None:
    """传输层失败（超时/管道断）经 registry.execute 转 is_error 回填，模型可见自纠。"""

    class BoomClient:
        def call_tool(self, name: str, arguments: dict) -> str:
            raise McpError("远端超时")

    registry = ToolRegistry()
    registry.register(McpTool(BoomClient(), "echo", "stub_tool", SPEC))
    result = registry.execute(
        "mcp__echo__stub_tool", '{"say": "x"}', confirm=lambda _t, _a: True, yolo=True
    )
    assert result.is_error is True
    assert "远端超时" in result.output


def test_mcp_tool_namespace_unique_in_registry() -> None:
    registry = ToolRegistry()
    registry.register(McpTool(None, "a", "tool", SPEC))  # type: ignore[arg-type]
    registry.register(McpTool(None, "b", "tool", SPEC))  # type: ignore[arg-type]
    assert registry.names() == ["mcp__a__tool", "mcp__b__tool"]


def test_attach_registers_echo_tool_from_subprocess() -> None:
    """真实 echo server attach 进 registry：mcp__ 前缀命名 + needs_confirm=True。"""
    cfg = McpServerConfig(name="echo", command=sys.executable, args=("-m", "mncc.mcp.echo_server"))
    registry = ToolRegistry()
    clients = attach_mcp_tools(registry, [cfg])
    try:
        assert "mcp__echo__echo" in registry.names()
        tool = registry.get("mcp__echo__echo")
        assert tool is not None and tool.needs_confirm({}) is True
        # 走 registry.execute（确认放行）能真正调到远端工具
        result = registry.execute(
            "mcp__echo__echo", '{"say": "你好"}', confirm=lambda _t, _a: True, yolo=True
        )
        assert result.is_error is False
        assert result.output == "你好"
    finally:
        for client in clients:
            client.close()


def test_attach_skips_failing_server() -> None:
    """D4：启动失败的 server 打警告跳过，其余照常注册。"""
    registry = ToolRegistry()
    good = McpServerConfig(name="echo", command=sys.executable, args=("-m", "mncc.mcp.echo_server"))
    bad = McpServerConfig(name="ghost", command="definitely-not-a-real-cmd-xyz")
    clients = attach_mcp_tools(registry, [bad, good])
    try:
        assert "mcp__echo__echo" in registry.names()
        assert "mcp__ghost__" not in " ".join(registry.names())
    finally:
        for client in clients:
            client.close()
