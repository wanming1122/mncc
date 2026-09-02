"""MCP 客户端（M6 D1–D5）：手写 JSON-RPC over stdio 最小子集。

模块边界：protocol.py 管帧编解码（纯函数）；client.py 管子进程生命周期与
请求/响应（McpClient/McpTool/attach_mcp_tools）；echo_server.py 是自写的
验收/测试 server（无需外部工具）。
"""

from .client import McpClient, McpError, McpServerConfig, McpTool, attach_mcp_tools

__all__ = [
    "McpClient",
    "McpError",
    "McpServerConfig",
    "McpTool",
    "attach_mcp_tools",
]
