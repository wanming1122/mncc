"""工具抽象与注册分发（§8 tools/base.py）。

设计要点（M2_DESIGN 决策 3/4）：
- 工具本身无 IO：确认交互通过 ConfirmFn 回调注入，进度显示由 loop 负责，
  工具只负责"参数 → 输出文本"（外加文件系统/子进程这类预期副作用）。
  好处：单测不需要模拟终端，换 UI（REPL/-p/未来的 GUI）不动工具。
- 错误不以异常穿出 execute：模型参数写错、文件不存在等失败以
  ToolResult(is_error=True) 回填 role=tool，模型看到错误原文才能自纠重试。
  抛异常终止循环就杀死了"自主恢复"——那是 agent 可靠性的核心。
- 例外：ConfirmRefused 是策略性失败（用户/模式拒绝授权），不是模型可自纠
  的错误，因此以异常穿出、由 loop 中止整个任务。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..safety import SafetyViolation

# 参数 JSON 解析失败时回显给模型的原始片段上限，防止畸形超长参数刷屏
_ERROR_ARGS_SNIPPET = 200


class ToolError(Exception):
    """工具的预期内失败。message 面向模型，必须能指导下一步动作。"""


class ConfirmRefused(Exception):
    """确认回调以异常方式拒绝（-p 非交互模式）：中止任务并提示 --yolo。

    与 confirm 返回 False 区分：False 是交互模式下用户说"不"，模型可以
    换方案继续；本异常发生在无人值守场景，继续跑没有意义，必须硬停。
    """


@dataclass
class ToolResult:
    """一次工具执行的结果：output 原样回填 role=tool 消息。"""

    output: str
    is_error: bool = False


class Tool(ABC):
    """一个可被模型调用的工具。

    子类三件事：name/description/schema 声明接口，run() 实现，
    按需覆盖 needs_confirm/brief/preview。
    """

    name: str = ""
    description: str = ""
    # JSON Schema 的 parameters 部分（{"type":"object","properties":...,"required":[...]}）。
    # 文案是评测驱动迭代（§6）的主要打磨对象：模型按描述决定何时、怎么调用。
    schema: dict[str, Any] = {}
    # 确认面板正文的语法高亮词法名（M3：edit_file 用 "diff"）；空串表示纯文本
    preview_lexer: str = ""

    @abstractmethod
    def run(self, **kwargs: Any) -> str:
        """执行工具。成功返回结果文本；预期内失败 raise ToolError。"""
        raise NotImplementedError

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        """该次调用是否需要用户确认（M2 仅 write_file；M3 加入危险命令）。"""
        return False

    def brief(self, args: dict[str, Any]) -> str:
        """进度行摘要（"正在读取 xxx"）。必须单行且短，长参数自行截断。"""
        text = json.dumps(args, ensure_ascii=False)
        return text[:60] + "…" if len(text) > 60 else text

    def preview(self, args: dict[str, Any]) -> str:
        """确认交互时展示给用户的内容预览。"""
        return json.dumps(args, ensure_ascii=False, indent=2)

    def confirm_title(self, args: dict[str, Any]) -> str:
        """确认面板标题（"写入 xxx" / "编辑 xxx" / "执行命令"）。"""
        return self.name


# 确认回调：返回 True 放行；False 表示交互模式下用户拒绝（模型可换方案）；
# 非交互模式的硬拒绝用 ConfirmRefused 异常。由 cli 注入，工具与 registry 不碰终端。
ConfirmFn = Callable[[Tool, dict[str, Any]], bool]


class ToolRegistry:
    """按名注册/分发工具；参数解析、确认门禁等横切逻辑集中在这里。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict[str, Any]]:
        """OpenAI function calling 的 tools 请求参数。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
            for tool in self._tools.values()
        ]

    def brief(self, name: str, arguments_json: str) -> str:
        """生成进度行摘要。解析失败不影响主流程（返回原始片段）。"""
        tool = self.get(name)
        if tool is None:
            return (arguments_json or name)[:60]
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except json.JSONDecodeError:
            return (arguments_json or "（无参数）")[:60]
        if not isinstance(args, dict):
            return (arguments_json or "（无参数）")[:60]
        return tool.brief(args)

    def execute(
        self,
        name: str,
        arguments_json: str,
        *,
        confirm: ConfirmFn,
        yolo: bool,
    ) -> ToolResult:
        """解析参数 → 确认门禁 → 执行，任何失败都转成 is_error 结果回填。

        顺序即模型的纠错路径：先查名字（列可用工具）、再查 JSON（提示重发）、
        再过确认（拒绝要说明后果）、最后执行（异常含原文）。
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                is_error=True,
                output=(
                    f"未知工具 {name!r}。可用工具：{', '.join(self._tools) or '（无）'}；"
                    "请从以上工具中选择后重试"
                ),
            )

        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except json.JSONDecodeError as exc:
            snippet = arguments_json[:_ERROR_ARGS_SNIPPET]
            return ToolResult(
                is_error=True,
                output=(
                    f"工具参数不是合法 JSON（{exc.msg}，第 {exc.pos} 个字符附近）。"
                    f"arguments 必须是 JSON 对象字符串，请修正后重新调用。"
                    f"原始内容：{snippet!r}"
                ),
            )
        if not isinstance(args, dict):
            return ToolResult(
                is_error=True,
                output=f"工具参数必须是 JSON 对象，收到 {type(args).__name__}，请重新调用",
            )

        if tool.needs_confirm(args) and not yolo:
            try:
                approved = confirm(tool, args)
            except (ToolError, SafetyViolation) as exc:
                # M3 D7：确认回调内部要算预览（edit 读文件/算 diff），可能失败；
                # 失败必须作为 is_error 回填而非炸穿循环——模型看到原因才能自纠
                return ToolResult(is_error=True, output=f"确认预览失败：{exc}")
            if not approved:
                return ToolResult(
                    is_error=True,
                    output=(
                        "用户拒绝了本次操作。请停止执行，向用户说明你想做什么并等待指示；"
                        "未经同意不要重复该操作"
                    ),
                )

        try:
            output = tool.run(**args)
        except ToolError as exc:
            return ToolResult(is_error=True, output=str(exc))
        except SafetyViolation as exc:
            # 独立的守卫异常：原文回填（"路径越界/命令被拦截"+ 正确做法），
            # 不走通用兜底的"工具执行异常"包装
            return ToolResult(is_error=True, output=str(exc))
        except ConfirmRefused:
            raise  # 策略性失败：穿出 execute，由 loop 中止任务（M2_DESIGN 决策 1/-p 行为）
        except TypeError as exc:
            # 模型漏传/错传参数名：给出 schema 让它对照修正
            return ToolResult(
                is_error=True,
                output=f"参数与工具定义不匹配：{exc}。schema："
                f"{json.dumps(tool.schema, ensure_ascii=False)}",
            )
        except Exception as exc:  # 工具内部未预期异常也不能炸掉 agent 循环
            return ToolResult(is_error=True, output=f"工具执行异常：{type(exc).__name__}: {exc}")
        return ToolResult(output=output)
