"""ToolRegistry：参数解析失败回填、未知名回填、确认门禁、--yolo 跳过、异常兜底。

错误一律转 ToolResult(is_error=True) 而不抛异常（M2_DESIGN 决策 4）——
这里是该契约的回归测试。
"""

from __future__ import annotations

from typing import Any

import pytest

from mncc.safety import SafetyViolation
from mncc.tools import Tool, ToolError, ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "回显输入"
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self) -> None:
        self.ran_with: dict[str, Any] | None = None

    def run(self, text: str) -> str:
        self.ran_with = {"text": text}
        return f"echo: {text}"


class BoomTool(Tool):
    name = "boom"
    description = "预期内失败"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        raise ToolError("内部坏了")


class CrashTool(Tool):
    name = "crash"
    description = "未预期异常"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        raise RuntimeError("意外崩溃")


class RiskyTool(Tool):
    name = "risky"
    description = "需要确认"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        return "done"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return True


class BadPreviewTool(Tool):
    """确认预览自身失败（M3 D7：edit 预览读文件失败的真实形态）。"""

    name = "badpreview"
    description = "预览会失败的确认工具"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        return "done"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return True

    def preview(self, args: dict[str, Any]) -> str:
        raise ToolError("预览失败：目标文件不存在")


class HuntTool(Tool):
    """守卫拒绝路径：SafetyViolation 抛出的工具。"""

    name = "hunt"
    description = "触发路径守卫"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        raise SafetyViolation("路径越界：../secret 不在工作区内")

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return False


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(BoomTool())
    reg.register(CrashTool())
    reg.register(RiskyTool())
    reg.register(BadPreviewTool())
    reg.register(HuntTool())
    return reg


def _execute(reg: ToolRegistry, name: str, args: str, **kwargs: Any):
    confirm = kwargs.pop("confirm", lambda _t, _a: True)
    return reg.execute(name, args, confirm=confirm, yolo=kwargs.pop("yolo", False))


def test_execute_success_returns_output(registry: ToolRegistry) -> None:
    result = _execute(registry, "echo", '{"text": "hi"}')
    assert result.is_error is False
    assert result.output == "echo: hi"
    assert registry.get("echo").ran_with == {"text": "hi"}  # type: ignore[union-attr]


def test_unknown_name_lists_available_tools(registry: ToolRegistry) -> None:
    result = _execute(registry, "nope", "{}")
    assert result.is_error is True
    for name in ("echo", "boom", "crash", "risky"):
        assert name in result.output


def test_malformed_json_backfills_parse_error(registry: ToolRegistry) -> None:
    result = _execute(registry, "echo", '{"text": ')
    assert result.is_error is True
    assert "JSON" in result.output
    assert '{"text": ' in result.output  # 原始内容回显帮助模型定位


def test_non_object_json_rejected(registry: ToolRegistry) -> None:
    result = _execute(registry, "echo", '["text"]')
    assert result.is_error is True
    assert "JSON 对象" in result.output


def test_missing_required_arg_backfills_type_error(registry: ToolRegistry) -> None:
    result = _execute(registry, "echo", "{}")
    assert result.is_error is True
    assert "参数" in result.output
    assert "schema" in result.output  # 附上 schema 让模型对照修正


def test_tool_error_backfills_original_message(registry: ToolRegistry) -> None:
    result = _execute(registry, "boom", "{}")
    assert result.is_error is True
    assert result.output == "内部坏了"


def test_unexpected_exception_backfills_not_raises(registry: ToolRegistry) -> None:
    result = _execute(registry, "crash", "{}")
    assert result.is_error is True
    assert "意外崩溃" in result.output


def test_confirm_denied_backfills_refusal(registry: ToolRegistry) -> None:
    result = _execute(registry, "risky", "{}", confirm=lambda _t, _a: False)
    assert result.is_error is True
    assert "用户拒绝" in result.output


def test_confirm_accepted_runs_tool(registry: ToolRegistry) -> None:
    asked_with: list[tuple[str, dict[str, Any]]] = []

    def confirm(tool: Tool, args: dict[str, Any]) -> bool:
        asked_with.append((tool.name, args))
        return True

    result = _execute(registry, "risky", "{}", confirm=confirm)
    assert result.is_error is False
    assert asked_with == [("risky", {})]


def test_yolo_skips_confirm_callback(registry: ToolRegistry) -> None:
    def must_not_ask(_tool: Tool, _args: dict[str, Any]) -> bool:
        raise AssertionError("yolo 不应触发确认")

    result = registry.execute("risky", "{}", confirm=must_not_ask, yolo=True)
    assert result.is_error is False
    assert result.output == "done"


def test_no_confirm_needed_when_tool_declares_false(registry: ToolRegistry) -> None:
    def must_not_ask(_tool: Tool, _args: dict[str, Any]) -> bool:
        raise AssertionError("无需确认的工具不应触发回调")

    result = registry.execute("echo", '{"text": "x"}', confirm=must_not_ask, yolo=False)
    assert result.output == "echo: x"


def test_duplicate_register_rejected() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    with pytest.raises(ValueError, match="重复"):
        reg.register(EchoTool())


def test_openai_schemas_structure() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    schemas = reg.openai_schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显输入",
                "parameters": EchoTool.schema,
            },
        }
    ]


def test_empty_arguments_json_means_empty_args(registry: ToolRegistry) -> None:
    # 少数模型会发 arguments=""；按空参数处理，让漏参错误走统一的 TypeError 回填
    result = _execute(registry, "echo", "")
    assert result.is_error is True
    assert "参数" in result.output


def test_brief_uses_tool_brief_and_tolerates_bad_json(registry: ToolRegistry) -> None:
    # EchoTool 未覆盖 brief，走 Tool.brief 默认实现（json.dumps）
    assert "hi" in registry.brief("echo", '{"text": "hi"}')
    assert registry.brief("echo", "{bad json")  # 不抛异常
    assert registry.brief("nope", "{}") == "{}"


# ---- M3 新增：确认预览失败与守卫异常的回填契约 ----


def test_preview_failure_backfills_error_not_raises(registry: ToolRegistry) -> None:
    """D7 回归：确认回调算预览时抛错 → is_error 回填原因，不炸穿循环。"""

    # 模拟真实确认回调（cli._repl_confirm 形态）：先调 preview 再返回 True
    def confirm(tool: Tool, args: dict[str, Any]) -> bool:
        tool.preview(args)
        return True

    result = _execute(registry, "badpreview", "{}", confirm=confirm)
    assert result.is_error is True
    assert "预览失败" in result.output


def test_safety_violation_backfilled_not_wrapped(registry: ToolRegistry) -> None:
    """SafetyViolation 显式转 is_error：输出原文"路径越界"，不带通用包装。"""
    result = _execute(registry, "hunt", "{}")
    assert result.is_error is True
    assert "路径越界" in result.output
    assert "工具执行异常" not in result.output


def test_confirm_title_default_is_tool_name(registry: ToolRegistry) -> None:
    assert registry.get("echo").confirm_title({"text": "x"}) == "echo"  # type: ignore[union-attr]
