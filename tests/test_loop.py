"""Agent 循环（M2 闭环版）：工具分发与回填、轮次/预算中止、中断结构合法性、
错误自纠。LLM 一律用脚本化假客户端回放多轮事件序列（§6）。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from mncc.agent.context import ContextManager
from mncc.agent.loop import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_MAX_TURNS,
    Session,
    compact_session,
    run_agent_loop,
)
from mncc.llm.client import (
    CompletionResult,
    Event,
    LLMClient,
    LLMConnectionError,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    Usage,
)
from mncc.prompts.system import SUMMARY_PROMPT
from mncc.tools import (
    ConfirmRefused,
    Tool,
    ToolError,
    ToolRegistry,
)
from mncc.ui.render import Renderer


class ScriptedClient(LLMClient):
    """按轮次回放假事件序列：第 i 次 stream() 返回 scripts[i]（M2_DESIGN §4）。"""

    def __init__(
        self,
        scripts: list[list[Event]],
        *,
        completion: str | Exception = "对话摘要",
    ) -> None:
        self.model = "scripted"
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.completion = completion

    def stream(self, messages, tools=None) -> Iterator[Event]:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        index = len(self.calls) - 1
        if index >= len(self.scripts):
            raise AssertionError("脚本轮数不够：循环发起了多余的模型调用")
        yield from self.scripts[index]

    def complete(self, messages, max_tokens=None) -> CompletionResult:
        self.complete_calls.append({"messages": messages, "max_tokens": max_tokens})
        if isinstance(self.completion, Exception):
            raise self.completion
        return CompletionResult(self.completion)


class EchoTool(Tool):
    name = "echo"
    description = "回显"
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(self, text: str) -> str:
        self.ran.append(text)
        return f"echo: {text}"


class BoomTool(Tool):
    name = "boom"
    description = "预期内失败"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        raise ToolError("内部坏了")


class InterruptTool(Tool):
    name = "intr"
    description = "执行中被用户打断"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        raise KeyboardInterrupt


class RiskyTool(Tool):
    name = "risky"
    description = "需要确认"
    schema = {"type": "object", "properties": {}}

    def run(self) -> str:
        return "risky done"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return True


def make_renderer(string_console) -> tuple[Renderer, Any]:
    console, buf = string_console
    return Renderer(console=console), buf


def _tool_call(cid: str, name: str, args_json: str) -> ResponseCompleted:
    return ResponseCompleted(content="", tool_calls=[ToolCall(cid, name, args_json)])


# ---- 完整闭环 ----


def test_full_tool_loop(string_console) -> None:
    renderer, buf = make_renderer(string_console)
    registry = ToolRegistry()
    echo = EchoTool()
    registry.register(echo)
    session = Session("sys")
    session.add_user("跑一下")
    client = ScriptedClient(
        [
            [_tool_call("c1", "echo", '{"text": "hi"}')],
            [ResponseCompleted("任务完成", usage=Usage(10, 5, 15))],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry)

    assert result.status == STATUS_COMPLETED
    assert result.content == "任务完成"
    assert result.turns == 2
    assert result.total_usage == Usage(10, 5, 15)
    assert echo.ran == ["hi"]
    # 消息结构：system, user, assistant(tool_calls), tool, assistant
    roles = [m["role"] for m in session.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assistant_msg = session.messages[2]
    assert assistant_msg["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text": "hi"}'},
        }
    ]
    assert assistant_msg["content"] is None  # 空文本 + tool_calls 用 None
    assert session.messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "echo: hi"}
    # 第二轮模型调用收到了工具结果
    second_call_messages = client.calls[1]["messages"]
    assert second_call_messages[-1]["content"] == "echo: hi"
    # schemas 传给了客户端；每轮用量打到输出
    assert client.calls[0]["tools"] == registry.openai_schemas()
    assert "tokens" in buf.getvalue()


def test_tool_error_lets_model_continue(string_console) -> None:
    """工具 is_error 回填原文后模型继续（脚本化第二轮自纠）。"""
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(BoomTool())
    echo = EchoTool()
    registry.register(echo)
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [_tool_call("c1", "boom", "{}")],
            [_tool_call("c2", "echo", '{"text": "fixed"}')],
            [ResponseCompleted("自纠后完成")],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry)

    assert result.status == STATUS_COMPLETED
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "内部坏了"  # 错误原文回填
    assert echo.ran == ["fixed"]


def test_unknown_tool_lists_available(string_console) -> None:
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [_tool_call("c1", "nope", "{}")],
            [ResponseCompleted("改对了")],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry)

    assert result.status == STATUS_COMPLETED
    tool_msg = session.messages[3]
    assert tool_msg["content"].startswith("未知工具")
    assert "echo" in tool_msg["content"]


def test_malformed_json_backfilled(string_console) -> None:
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [_tool_call("c1", "echo", '{"text": ')],
            [ResponseCompleted("改对了")],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry)

    assert result.status == STATUS_COMPLETED
    assert "JSON" in session.messages[3]["content"]


def test_pure_chat_without_registry(string_console) -> None:
    """registry=None：纯对话路径（M1 行为合并不回退）。"""
    renderer, _buf = make_renderer(string_console)
    session = Session("sys")
    session.add_user("你好")
    client = ScriptedClient([[TextDelta("你"), TextDelta("好"), ResponseCompleted("你好")]])

    result = run_agent_loop(client, renderer, session)

    assert result.status == STATUS_COMPLETED
    assert result.content == "你好"
    assert client.calls[0]["tools"] is None
    assert session.messages[-1] == {"role": "assistant", "content": "你好"}


# ---- 中止条件 ----


def test_max_turns_stops(string_console) -> None:
    renderer, buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = Session("sys")
    session.add_user("loop forever")
    client = ScriptedClient([[_tool_call(f"c{i}", "echo", '{"text": "x"}')] for i in range(10)])

    result = run_agent_loop(client, renderer, session, registry, max_turns=3)

    assert result.status == STATUS_MAX_TURNS
    assert result.turns == 3
    assert len(client.calls) == 3
    assert "最大轮数" in buf.getvalue()


def test_budget_exceeded_stops_before_next_model_call(string_console) -> None:
    renderer, buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [
                ResponseCompleted(
                    content="",
                    usage=Usage(150, 100, 250),
                    tool_calls=[ToolCall("c1", "echo", "{}")],
                )
            ],
            [ResponseCompleted("不该到达")],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry, token_budget=200)

    assert result.status == STATUS_BUDGET_EXCEEDED
    assert len(client.calls) == 1  # 预算超限后没有再发起模型调用
    assert "预算" in buf.getvalue()


# ---- 中断：消息结构必须保持合法 ----


def test_interrupt_during_stream_keeps_partial(string_console) -> None:
    renderer, buf = make_renderer(string_console)

    class InterruptingClient(LLMClient):
        model = "scripted"

        def stream(self, messages, tools=None) -> Iterator[Event]:
            yield TextDelta("半截")
            raise KeyboardInterrupt

        def complete(self, messages, max_tokens=None) -> CompletionResult:
            return CompletionResult("")  # 本测试不触发压缩

    session = Session("sys")
    result = run_agent_loop(InterruptingClient(), renderer, session)

    assert result.status == STATUS_INTERRUPTED
    assert session.messages[-1] == {"role": "assistant", "content": "半截"}
    assert "已中断" in buf.getvalue()


def test_interrupt_during_tool_execution_keeps_structure_valid(string_console) -> None:
    """已完成的工具结果照常回填，被打断的补占位 tool 消息（M2_DESIGN §4）。"""
    renderer, buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(InterruptTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [
                ResponseCompleted(
                    content="",
                    tool_calls=[
                        ToolCall("c1", "echo", '{"text": "a"}'),
                        ToolCall("c2", "intr", "{}"),
                    ],
                )
            ]
        ]
    )

    result = run_agent_loop(client, renderer, session, registry)

    assert result.status == STATUS_INTERRUPTED
    # 每个 tool_call 都有对应的 role=tool 消息 → 结构合法
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert tool_msgs[0]["content"] == "echo: a"
    assert "未执行" in tool_msgs[1]["content"]
    assert "已中断" in buf.getvalue()


# ---- 确认策略 ----


def test_confirm_refused_backfills_and_continues(string_console) -> None:
    """交互模式拒绝（返回 False）→ is_error 回填，模型可换方案。"""
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(RiskyTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient([[_tool_call("c1", "risky", "{}")], [ResponseCompleted("好的，停了")]])

    def deny(tool: Tool, args: dict[str, Any]) -> bool:
        return False

    result = run_agent_loop(client, renderer, session, registry, confirm=deny)

    assert result.status == STATUS_COMPLETED
    assert "用户拒绝" in session.messages[3]["content"]


def test_confirm_refused_exception_aborts_task(string_console) -> None:
    """-p 模式硬拒（ConfirmRefused）→ 任务中止、状态 error、占位消息补齐。"""
    renderer, buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(RiskyTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient(
        [
            [
                ResponseCompleted(
                    content="",
                    tool_calls=[ToolCall("c1", "risky", "{}"), ToolCall("c2", "risky", "{}")],
                )
            ]
        ]
    )

    def hard_deny(tool: Tool, args: dict[str, Any]) -> bool:
        raise ConfirmRefused("请加 --yolo")

    result = run_agent_loop(client, renderer, session, registry, confirm=hard_deny)

    assert result.status == STATUS_ERROR
    assert "--yolo" in buf.getvalue()
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]  # 结构仍合法
    assert all("拒绝" in m["content"] for m in tool_msgs)


def test_yolo_skips_confirm(string_console) -> None:
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(RiskyTool())
    session = Session("sys")
    session.add_user("go")
    client = ScriptedClient([[_tool_call("c1", "risky", "{}")], [ResponseCompleted("完成")]])

    def must_not_ask(tool: Tool, args: dict[str, Any]) -> bool:
        raise AssertionError("yolo 模式不应触发确认回调")

    result = run_agent_loop(client, renderer, session, registry, confirm=must_not_ask, yolo=True)

    assert result.status == STATUS_COMPLETED


# ---- 其他 ----


def test_llm_error_returns_error_status(string_console) -> None:
    renderer, buf = make_renderer(string_console)
    session = Session("sys")
    count_before = len(session.messages)

    class ErrorClient(LLMClient):
        model = "scripted"

        def stream(self, messages, tools=None) -> Iterator[Event]:
            yield TextDelta("开头")
            raise LLMConnectionError("无法连接服务端")

        def complete(self, messages, max_tokens=None) -> CompletionResult:
            return CompletionResult("")  # 本测试不触发压缩

    result = run_agent_loop(ErrorClient(), renderer, session)

    assert result.status == STATUS_ERROR
    assert len(session.messages) == count_before  # 失败轮不写入 assistant 消息
    assert "无法连接" in buf.getvalue()


def test_loop_result_elapsed_and_turns_populated(string_console) -> None:
    renderer, _buf = make_renderer(string_console)
    session = Session("sys")
    session.add_user("hi")
    client = ScriptedClient([[ResponseCompleted("ok", usage=Usage(1, 1, 2))]])

    result = run_agent_loop(client, renderer, session)

    assert result.elapsed >= 0.0
    assert result.turns == 1
    assert result.total_usage == Usage(1, 1, 2)


def test_session_tokens_estimate_counts_tool_arguments() -> None:
    session = Session("sys")
    session.add_assistant_tool_calls(
        "", [ToolCall("c1", "write_file", '{"path": "a.py", "content": "x = 1"}')]
    )
    session.add_tool_message("c1", "done")
    # tool_calls 的 arguments 计入估算（内容是大头，漏计会低估上下文）
    assert session.tokens_estimate() > estimate_of_tool_args(session)


def estimate_of_tool_args(session: Session) -> int:
    from mncc.agent.context import estimate_tokens

    return estimate_tokens('{"path": "a.py", "content": "x = 1"}')


# ---- M4：auto-compact / 校准 / L1 截断 ----


class BigEchoTool(Tool):
    name = "big_echo"
    description = "返回超大输出"
    schema = {"type": "object", "properties": {"size": {"type": "integer"}}, "required": ["size"]}

    def run(self, size: int) -> str:
        return "x" * size


def _big_history_session() -> Session:
    """构造超过压缩阈值的会话：多个大输出旧轮 + 最近两轮。"""
    session = Session("sys")
    session.add_user("任务")
    session.add_assistant_tool_calls("", [ToolCall("g1", "big_echo", '{"size": 2000}')])
    session.add_tool_message("g1", "x" * 2000)
    session.add_assistant_tool_calls("", [ToolCall("g2", "big_echo", '{"size": 2000}')])
    session.add_tool_message("g2", "x" * 2000)
    session.add_assistant_tool_calls("", [ToolCall("g3", "big_echo", '{"size": 2000}')])
    session.add_tool_message("g3", "x" * 2000)
    session.add_assistant_tool_calls("", [ToolCall("g4", "big_echo", '{"size": 50}')])
    session.add_tool_message("g4", "x" * 50)
    return session


def test_default_context_small_session_no_compact(string_console) -> None:
    """不传 context 参数：自动档（128k 窗口）对小消息不触发压缩、不发起 complete()。"""
    renderer, buf = make_renderer(string_console)
    session = Session("sys")
    session.add_user("你好")
    client = ScriptedClient([[ResponseCompleted("ok", usage=Usage(1, 1, 2))]])

    result = run_agent_loop(client, renderer, session)

    assert result.status == STATUS_COMPLETED
    assert client.complete_calls == []
    assert "上下文压缩" not in buf.getvalue()


def test_auto_compact_compacts_before_model_call(string_console) -> None:
    """守卫区后、stream 前触发：旧轮进摘要，模型收到的消息变短（D2/D3）。"""
    cm = ContextManager(model_context_limit=1000, compact_threshold=0.8, summary_max_tokens=50)
    renderer, buf = make_renderer(string_console)
    session = _big_history_session()
    client = ScriptedClient([[ResponseCompleted("完成", usage=Usage(10, 2, 12))]])

    result = run_agent_loop(client, renderer, session, context=cm)

    assert result.status == STATUS_COMPLETED
    # 摘要请求发起过：SUMMARY_PROMPT + max_tokens 透传
    assert len(client.complete_calls) == 1
    assert client.complete_calls[0]["messages"][0] == {
        "role": "system",
        "content": SUMMARY_PROMPT,
    }
    assert client.complete_calls[0]["max_tokens"] == 50
    # 压缩面板已打印
    assert "上下文压缩" in buf.getvalue()
    # 压缩后结构：system + user 摘要 + 最近 2 轮；旧的大输出进摘要而非仍留在历史
    roles = [m["role"] for m in session.messages]
    assert roles[0] == "system"
    assert roles[1] == "user"
    assert session.messages[1]["content"] == "对话摘要"
    # 模型收到的请求里不再包含最早的 g1 大输出
    sent = [str(m.get("content") or "") for m in client.calls[0]["messages"]]
    assert not any("g1" in s for s in sent)
    assert "对话摘要" in sent[1]


def test_auto_compact_records_observations(string_console) -> None:
    """每轮 stream 带 usage 时调用 observe 校准（D1）。"""
    cm = ContextManager(model_context_limit=128_000, compact_threshold=0.8, summary_max_tokens=50)
    renderer, _buf = make_renderer(string_console)
    session = Session()  # 无 system 消息：prompt_est 恰好 1000
    session.add_user("x" * 4000)
    client = ScriptedClient([[ResponseCompleted("ok", usage=Usage(1333, 5, 1338))]])

    run_agent_loop(client, renderer, session, context=cm)

    # ratio = 1333/1000 → divisor = 0.8*4 + 0.2*(4*1.333) = 4.2664
    assert cm.estimator.divisor == pytest.approx(4.2664, abs=1e-3)


def test_tool_output_l1_truncated_before_backfill(string_console) -> None:
    """超大工具输出回填前被 L1 统一截断（D6）。"""
    cm = ContextManager(model_context_limit=128_000, compact_threshold=0.8, summary_max_tokens=500)
    renderer, _buf = make_renderer(string_console)
    registry = ToolRegistry()
    registry.register(BigEchoTool())
    session = Session("sys")
    session.add_user("跑一下")
    client = ScriptedClient(
        [
            [_tool_call("c1", "big_echo", '{"size": 20000}')],
            [ResponseCompleted("完成")],
        ]
    )

    result = run_agent_loop(client, renderer, session, registry, context=cm)

    assert result.status == STATUS_COMPLETED
    tool_msg = session.messages[3]
    assert len(tool_msg["content"]) < 16_100
    assert tool_msg["content"].startswith("x" * 12_000)
    assert "中间省略" in tool_msg["content"]
    assert tool_msg["content"].endswith("x" * 4_000)
    # 第二轮模型调用收到的是截断后的内容，不是原始 20000 字符
    second_call_last = client.calls[1]["messages"][-1]
    assert len(second_call_last["content"]) < 16_100


def test_compact_session_manual_path(string_console) -> None:
    """手动压缩与 auto 共用同一实现：达阈值压缩、未达阈值不动。"""
    cm = ContextManager(model_context_limit=1000, compact_threshold=0.8, summary_max_tokens=50)
    renderer, buf = make_renderer(string_console)
    session = _big_history_session()
    client = ScriptedClient([])  # 不会被 stream：只触发 complete()

    assert compact_session(session, cm, client, renderer) is True
    assert session.messages[1]["content"] == "对话摘要"
    assert "上下文压缩" in buf.getvalue()

    small = Session("sys")
    small.add_user("短")
    small.add_assistant("短")
    assert compact_session(small, cm, client, renderer) is False
    assert len(small.messages) == 3  # 未发生任何修改
