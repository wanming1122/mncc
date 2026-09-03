"""CLI：斜杠命令分发 + main() 的 -p 模式契约（退出码、stdout 纯净性、工具写入拒绝）。

交互式 REPL 本身无法在单测里驱动，只测可注入依赖的纯函数路径。
M2 起 -p 模式的 write_file 默认拒绝（ConfirmRefused）；--yolo 绕过。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mncc.agent.context import ContextManager
from mncc.agent.loop import Session
from mncc.cli import EXIT_FAIL, EXIT_INTERRUPT, EXIT_OK, handle_slash, main
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
from mncc.ui.render import Renderer


class DummyClient(LLMClient):
    model = "dummy"

    def stream(self, messages, tools=None) -> Iterator[Event]:
        yield from ()

    def complete(self, messages, max_tokens=None) -> CompletionResult:
        return CompletionResult("固定摘要")


@pytest.fixture()
def ctx(string_console):
    console, buf = string_console
    return {
        "session": Session("sys"),
        "client": DummyClient(),
        "renderer": Renderer(console=console),
        # 小窗口：/compact 测试用小历史即可跨过阈值
        "context": ContextManager(
            model_context_limit=500, compact_threshold=0.8, summary_max_tokens=50
        ),
    }, buf


def call(ctx, line: str) -> bool:
    return handle_slash(
        line,
        session=ctx["session"],
        client=ctx["client"],
        renderer=ctx["renderer"],
        context=ctx["context"],
    )


def test_clear_resets_history(ctx) -> None:
    repl, _buf = ctx
    repl["session"].add_user("u")
    repl["session"].add_assistant("a")
    assert call(repl, "/clear") is True
    assert [m["role"] for m in repl["session"].messages] == ["system"]


def test_exit_returns_false(ctx) -> None:
    repl, _buf = ctx
    assert call(repl, "/exit") is False


def test_model_switch_and_show(ctx) -> None:
    repl, buf = ctx
    assert call(repl, "/model glm-4-flash") is True
    assert repl["client"].model == "glm-4-flash"
    call(repl, "/model")
    assert "glm-4-flash" in buf.getvalue()


def test_unknown_command_hinted(ctx) -> None:
    repl, buf = ctx
    assert call(repl, "/nope") is True
    assert "未知命令" in buf.getvalue()


def test_context_view_lists_messages(ctx) -> None:
    repl, buf = ctx
    repl["session"].add_user("你好")
    assert call(repl, "/context") is True
    assert "user" in buf.getvalue()
    # M4：追加了窗口/阈值信息
    assert "模型窗口" in buf.getvalue()


def test_compact_manual_trigger(ctx) -> None:
    """达阈值时 /compact 真正压缩：DummyClient.complete 的固定摘要进历史。"""
    repl, buf = ctx
    session = repl["session"]
    for i in range(6):
        session.add_user(f"任务 {i}")
        session.add_assistant("a" * 320)  # 每轮 80+ tokens；6 轮 ≈ 500 ≥ 400
    assert call(repl, "/compact") is True
    assert "上下文压缩" in buf.getvalue()
    assert session.messages[1] == {"role": "user", "content": "固定摘要"}
    assert len(session.messages) < 13  # 历史变短：system + 摘要 + 最近两轮


def test_compact_below_threshold_hints_and_keeps_history(ctx) -> None:
    repl, buf = ctx
    session = repl["session"]
    session.add_user("短")
    session.add_assistant("短")
    before = list(session.messages)
    assert call(repl, "/compact") is True
    assert "无需压缩" in buf.getvalue()
    assert session.messages == before  # 未达阈值：历史不动、不调 complete()


def test_help_lists_all_commands(ctx) -> None:
    repl, buf = ctx
    call(repl, "/help")
    for cmd in ("/help", "/clear", "/model", "/context", "/compact", "/exit"):
        assert cmd in buf.getvalue()


# ---- main() 的 -p 模式契约 ----


class ReplyClient(LLMClient):
    """回放一条固定回复（纯文本，无工具调用）。"""

    model = "dummy"

    def __init__(self, interrupt: bool = False) -> None:
        self._interrupt = interrupt

    def stream(self, messages, tools=None) -> Iterator[Event]:
        yield TextDelta("好的")
        if self._interrupt:
            raise KeyboardInterrupt
        yield ResponseCompleted("好的", usage=Usage(3, 2, 5))

    def complete(self, messages, max_tokens=None) -> CompletionResult:
        return CompletionResult("")  # -p 测例不触发压缩


class FailingClient(LLMClient):
    """立即抛连接错误的客户端。"""

    model = "dummy"

    def stream(self, messages, tools=None) -> Iterator[Event]:
        raise LLMConnectionError("无法连接服务端")
        yield  # pragma: no cover # 使其成为生成器函数

    def complete(self, messages, max_tokens=None) -> CompletionResult:
        return CompletionResult("")


class WriteToolClient(LLMClient):
    """模型发起 write_file 调用，用于验证 -p 模式的写入拒绝。

    每次 stream() 调用只产出一个 ResponseCompleted（OpenAI 协议约定）；
    第一次带 tool_calls，第二次是纯文本回复。
    """

    model = "dummy"

    def __init__(self) -> None:
        self._calls = 0

    def stream(self, messages, tools=None) -> Iterator[Event]:
        self._calls += 1
        if self._calls == 1:
            yield ResponseCompleted(
                content="",
                tool_calls=[ToolCall("tc1", "write_file", '{"path": "out.py", "content": "x=1"}')],
            )
        else:
            yield ResponseCompleted("写完了")

    def complete(self, messages, max_tokens=None) -> CompletionResult:
        return CompletionResult("")  # 本组测例不触发压缩


@pytest.fixture()
def fake_deps(monkeypatch: pytest.MonkeyPatch):
    """main() 依赖全部替换：配置固定、客户端脚本化，不发任何网络请求。"""
    from mncc import cli
    from mncc.config import Config

    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config())
    monkeypatch.setattr(cli, "resolve_api_key", lambda cfg: "sk-test")
    holder: dict[str, object] = {}

    def install(client: LLMClient) -> None:
        holder["client"] = client
        monkeypatch.setattr(cli, "OpenAICompatClient", lambda **kwargs: holder["client"])

    return install


def test_main_print_mode_ok(fake_deps, capsys) -> None:
    fake_deps(ReplyClient())
    assert main(["-p", "你好"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "好的" in captured.out
    # 用量打到 stderr，stdout 只有回复
    assert "tokens" in captured.err


def test_main_print_mode_interrupt(fake_deps, capsys) -> None:
    fake_deps(ReplyClient(interrupt=True))
    assert main(["-p", "你好"]) == EXIT_INTERRUPT


def test_main_print_mode_llm_error_once_on_stderr(fake_deps, capsys) -> None:
    """回归：错误只渲染一次（run_turn 已渲染），且走 stderr 不污染 stdout。"""
    fake_deps(FailingClient())
    assert main(["-p", "你好"]) == EXIT_FAIL
    captured = capsys.readouterr()
    assert captured.err.count("无法连接服务端") == 1
    assert "无法连接服务端" not in captured.out


# ---- D4：--stats-json 落盘（评测记账契约） ----


def test_main_print_mode_stats_json_written(fake_deps, capsys, tmp_path: Path) -> None:
    """-p 正常完成 → LoopResult 落盘 JSON，字段全（轮数/token/耗时/回复长度）。"""
    import json

    fake_deps(ReplyClient())
    stats = tmp_path / "stats.json"
    assert main(["-p", "你好", "--stats-json", str(stats)]) == EXIT_OK
    payload = json.loads(stats.read_text(encoding="utf-8"))
    assert set(payload) == {
        "status",
        "turns",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "elapsed",
        "chars",
    }
    assert payload["status"] == "completed"
    assert payload["turns"] == 1
    assert payload["prompt_tokens"] == 3
    assert payload["completion_tokens"] == 2
    assert payload["total_tokens"] == 5
    assert payload["chars"] == 2  # 回复 "好的" 两个字符
    assert payload["elapsed"] >= 0


def test_main_print_mode_llm_error_stats_recorded(fake_deps, capsys, tmp_path: Path) -> None:
    """graceful 失败也落盘（status=error）：失败任务的轮数/token 是归因数据；
    唯一不落盘的场景是外部强杀（timeout），由 runner 自行记录。"""
    import json

    fake_deps(FailingClient())
    stats = tmp_path / "stats.json"
    assert main(["-p", "你好", "--stats-json", str(stats)]) == EXIT_FAIL
    payload = json.loads(stats.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert payload["turns"] == 1  # 第一轮 stream 即失败
    assert payload["total_tokens"] == 0
    assert payload["elapsed"] >= 0


def test_main_print_mode_interrupt_stats_recorded(fake_deps, capsys, tmp_path: Path) -> None:
    import json

    fake_deps(ReplyClient(interrupt=True))
    stats = tmp_path / "stats.json"
    assert main(["-p", "你好", "--stats-json", str(stats)]) == EXIT_INTERRUPT
    payload = json.loads(stats.read_text(encoding="utf-8"))
    assert payload["status"] == "interrupted" and payload["turns"] == 1


def test_main_missing_key_fails_gracefully(monkeypatch, capsys) -> None:
    from mncc import cli
    from mncc.config import Config, ConfigError

    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config())
    monkeypatch.setattr(
        cli, "resolve_api_key", lambda cfg: (_ for _ in ()).throw(ConfigError("未找到 API key"))
    )
    assert main(["-p", "hi"]) == EXIT_FAIL
    assert "API key" in capsys.readouterr().err


def test_main_version_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0


# ---- -p 写入拒绝（M2_DESIGN 决策 1）----


def test_print_mode_write_refused_without_yolo(fake_deps, capsys) -> None:
    """-p 模式遇到 write_file 确认时 ConfirmRefused → 退出码非 0。"""
    fake_deps(WriteToolClient())
    assert main(["-p", "写点东西"]) == EXIT_FAIL
    captured = capsys.readouterr()
    assert "--yolo" in captured.err


def test_print_mode_write_allowed_with_yolo(fake_deps, capsys, tmp_path: Path) -> None:
    """--yolo 绕过确认，write_file 正常执行。"""
    fake_deps(WriteToolClient())
    # tmp_path 作为 cwd 保证文件写在安全位置
    import os

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert main(["-p", "写点东西", "--yolo"]) == EXIT_OK
    finally:
        os.chdir(old_cwd)


# ---- prompt_toolkit 不可用时的降级 ----


def test_build_prompt_session_falls_back(monkeypatch) -> None:
    """Git Bash(mintty)/管道下 prompt_toolkit 构造即崩；必须降级为 input() 而非崩溃。"""
    from mncc import cli

    def _boom(*args, **kwargs):
        raise RuntimeError("NoConsoleScreenBufferError: Found xterm-256color")

    monkeypatch.setattr(cli, "PromptSession", _boom)
    session = cli.build_prompt_session()
    assert isinstance(session, cli._FallbackPrompt)
    # FallbackPrompt 的 EOFError 语义与 prompt_toolkit 一致（REPL 循环据此退出）
    monkeypatch.setattr("builtins.input", lambda _msg: (_ for _ in ()).throw(EOFError))
    with pytest.raises(EOFError):
        session.prompt("mncc> ")


# ---- M3：build_registry 装配 ----


def test_build_registry_has_six_guarded_tools(tmp_path: Path) -> None:
    from mncc.cli import build_registry

    registry = build_registry(root=tmp_path)
    assert registry.names() == [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "grep",
        "run_command",
    ]


def test_build_registry_blocked_command_via_execute(tmp_path: Path) -> None:
    """§11 场景 4：危险命令经 registry 返回 is_error（绝不被执行）。"""
    from mncc.cli import build_registry

    registry = build_registry(root=tmp_path)
    result = registry.execute(
        "run_command", '{"cmd": "rm -rf /"}', confirm=lambda _t, _a: True, yolo=True
    )
    assert result.is_error is True
    assert "拦截" in result.output


def test_build_registry_path_guard_via_execute(tmp_path: Path) -> None:
    import json

    from mncc.cli import build_registry

    registry = build_registry(root=tmp_path)
    args = json.dumps({"path": str(tmp_path.parent / "outside.txt")})
    result = registry.execute("read_file", args, confirm=lambda _t, _a: True, yolo=False)
    assert result.is_error is True
    assert "越界" in result.output


# ---- M6：_connect_mcp 与 MCP 生命周期（D4/D5）----


def test_connect_mcp_empty_returns_empty_list() -> None:
    from mncc.cli import _connect_mcp
    from mncc.tools.base import ToolRegistry

    assert _connect_mcp(ToolRegistry(), ()) == []


def test_connect_mcp_converts_config_to_server_config(monkeypatch) -> None:
    """Config.mcp_servers（tuple[dict] 原始结构）→ McpServerConfig，再交给 attach。"""
    from mncc import cli
    from mncc.mcp import McpServerConfig
    from mncc.tools.base import ToolRegistry

    captured: dict[str, object] = {}

    def fake_attach(registry, cfgs):
        captured["cfgs"] = cfgs
        return []

    monkeypatch.setattr(cli, "attach_mcp_tools", fake_attach)
    servers = ({"name": "echo", "command": "python", "args": ["-m", "mncc.mcp.echo_server"]},)
    cli._connect_mcp(ToolRegistry(), servers)
    assert captured["cfgs"] == [
        McpServerConfig(name="echo", command="python", args=("-m", "mncc.mcp.echo_server"))
    ]


def test_main_closes_mcp_clients_on_exit(monkeypatch) -> None:
    """成功连接的 client 在 main 退出后统一 close（D4/D5 finally 语义）。"""
    from mncc import cli
    from mncc.config import Config

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    holder: dict[str, FakeClient] = {}

    def fake_connect(registry, servers):
        holder["client"] = FakeClient()
        return [holder["client"]]

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *a, **k: Config(mcp_servers=({"name": "echo", "command": "python"},)),
    )
    monkeypatch.setattr(cli, "resolve_api_key", lambda cfg: "sk-test")
    monkeypatch.setattr(cli, "_connect_mcp", fake_connect)
    monkeypatch.setattr(cli, "OpenAICompatClient", lambda **kwargs: ReplyClient())
    assert main(["-p", "你好"]) == EXIT_OK
    assert holder["client"].closed is True
