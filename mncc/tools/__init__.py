"""工具集（§4.3）：base 抽象 + 各工具实现。

M3 起所有工具构造都需注入对应守卫（PathGuard / CommandGuard），
由 cli.build_registry 统一装配；测试用各自的 tmp_path 构造。
"""

from .base import ConfirmFn, ConfirmRefused, Tool, ToolError, ToolRegistry, ToolResult
from .command import RunCommandTool
from .files import EditFileTool, ReadFileTool, WriteFileTool
from .search import GrepTool, ListDirTool

__all__ = [
    "ConfirmFn",
    "ConfirmRefused",
    "EditFileTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "RunCommandTool",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
]
