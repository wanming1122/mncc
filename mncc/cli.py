"""入口：参数解析、REPL 主循环、-p 非交互模式（§4.1）。

REPL 与 -p 的分叉点：两者共用 load_config / Session / run_agent_loop / 同一套
工具注册表，只差三件事——输入来源（prompt vs argv）、确认策略（交互 y/n vs
ConfirmRefused 硬拒）、输出目的地（stdout+stderr 混排 vs stdout 只留回复）。
这是 §10 INTERVIEW.md 里"-p 模式与 REPL 的架构分叉"的答案骨架。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from . import __version__
from .agent.context import ContextManager
from .agent.loop import (
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    LoopResult,
    Session,
    compact_session,
    run_agent_loop,
)
from .config import GLOBAL_CONFIG_PATH, Config, ConfigError, load_config, resolve_api_key
from .llm.client import LLMClient
from .llm.openai_compat import OpenAICompatClient
from .mcp import McpClient, McpServerConfig, attach_mcp_tools
from .prompts.system import SYSTEM_PROMPT
from .safety import CommandGuard, PathGuard
from .tools import (
    ConfirmFn,
    ConfirmRefused,
    EditFileTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    RunCommandTool,
    Tool,
    ToolRegistry,
    WriteFileTool,
)
from .ui.render import Renderer

HISTORY_PATH = Path.home() / ".mncc" / "history"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INTERRUPT = 130  # 惯例：被 SIGINT 终止的进程退出码

SLASH_USAGE = """\
/help        显示本帮助
/clear       清空对话历史（保留 system prompt）
/model       查看当前模型；/model <名称> 切换（仅本次会话生效）
/context     查看上下文消息与 token 估算
/compact     手动压缩上下文（生成摘要、保留最近两轮）
/exit        退出（等价 Ctrl+D）"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mncc",
        description="mini Claude Code —— 终端 AI 编程助手（Agent Loop + 流式工具调用 + "
        "上下文压缩 + MCP 客户端 + 评测）",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="task",
        metavar="任务",
        help="非交互模式：执行单个任务后退出，退出码反映成败（评测管线入口）",
    )
    parser.add_argument("--model", metavar="名称", help="覆盖配置文件中的模型名")
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="跳过所有确认（写入文件等操作将直接执行；评测与脚本化调用必备）",
    )
    parser.add_argument(
        "--stats-json",
        metavar="路径",
        help="评测用（仅 -p 有效）：任务成功结束时把 轮数/token/耗时 写入 JSON 文件",
    )
    parser.add_argument("--version", action="version", version=f"mncc {__version__}")
    return parser


def build_registry(root: Path | None = None) -> ToolRegistry:
    """M3 工具集：六个工具共用同一个安全锚点（工作区 + 命令守卫）。

    所有文件/搜索工具注入同一个 PathGuard 实例——"每一处文件操作都过
    同一道闸门"；RunCommandTool 单独注入 CommandGuard（命令的黑名单与
    授权记忆是会话态，路径守卫只负责文件）。
    M6 的 MCP 远端工具（§5.A）将以 mcp__ 前缀挂到同一个 registry。
    """
    workspace = (root or Path.cwd()).resolve()
    path_guard = PathGuard(workspace)
    registry = ToolRegistry()
    registry.register(ReadFileTool(path_guard))
    registry.register(WriteFileTool(path_guard))
    registry.register(EditFileTool(path_guard))
    registry.register(ListDirTool(path_guard))
    registry.register(GrepTool(path_guard))
    registry.register(RunCommandTool(CommandGuard()))
    return registry


def _connect_mcp(registry: ToolRegistry, servers: tuple[dict[str, object], ...]) -> list[McpClient]:
    """把 Config.mcp_servers（tuple[dict] 原始结构）转成 McpServerConfig 并 attach。

    返回成功连接的 client 列表；失败的 server 已在 attach 内打警告跳过（D4），
    主流程照常。
    """
    cfgs = [
        McpServerConfig(
            name=entry["name"],
            command=entry["command"],
            args=tuple(entry.get("args") or ()),
        )
        for entry in servers
    ]
    return attach_mcp_tools(registry, cfgs)


def build_prompt_session() -> PromptSession[str] | _FallbackPrompt:
    """输入行：跨会话历史（上箭头）+ 多行输入（§4.1）。

    多行方案：保持 Enter=发送的单行习惯，另绑定 Esc+Enter / Ctrl+J 插入换行。
    不用 multiline=True（Enter 变换行、Meta+Enter 才发送），因为那会破坏
    大多数单行输入的肌肉记忆。

    Git Bash(mintty)/管道下 prompt_toolkit 无法创建 Win32 控制台缓冲区，
    构造即抛 NoConsoleScreenBufferError——返回 input() 兜底实现，
    保证 REPL 在任何终端可用（§3 的开发环境正是 Git Bash）。
    """
    try:
        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def _insert_newline(event: object) -> None:
            buffer = getattr(event, "current_buffer", None)
            if buffer is not None:
                buffer.insert_text("\n")

        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        return PromptSession[str](history=FileHistory(str(HISTORY_PATH)), key_bindings=bindings)
    except Exception:
        return _FallbackPrompt()


class _FallbackPrompt:
    """prompt_toolkit 不可用时的简易输入行（无历史/多行，但永远不会崩）。"""

    def prompt(self, text: str, **_kwargs: object) -> str:
        # **_kwargs 吞掉 bottom_toolbar 等增强参数，保持与 PromptSession.prompt 同签名
        return input(text)


def handle_slash(
    line: str,
    *,
    session: Session,
    client: LLMClient,
    renderer: Renderer,
    context: ContextManager,
) -> bool:
    """处理斜杠命令。返回 False 表示要求退出 REPL。"""
    cmd, _, arg = line.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd == "/help":
        renderer.print_text(SLASH_USAGE)
    elif cmd == "/clear":
        session.reset()
        renderer.hint("已清空对话历史")
    elif cmd == "/exit":
        return False
    elif cmd == "/model":
        if arg:
            client.model = arg
            renderer.hint(f"模型已切换为 {arg}（仅本次会话生效，未写入配置文件）")
        else:
            renderer.hint(
                f"当前模型：{client.model}\n"
                f"切换：/model <模型名>；持久化请写入 {GLOBAL_CONFIG_PATH}"
            )
    elif cmd == "/context":
        renderer.context_view(session.messages, session.tokens_estimate(), context=context)
    elif cmd == "/compact":
        if not compact_session(session, context, client, renderer):
            renderer.hint("当前上下文未达到压缩阈值，无需压缩")
    else:
        renderer.hint(f"未知命令：{cmd}（/help 查看可用命令）")
    return True


def _repl_confirm(renderer: Renderer) -> ConfirmFn:
    """REPL 的确认回调：按工具的 confirm_title/preview 展示（edit 走 diff 高亮）。

    yolo 短路在 registry.execute 里；黑名单短路在 CommandGuard 里，都到不了这里。
    """

    def confirm(tool: Tool, args: dict) -> bool:
        return renderer.confirm(
            tool.confirm_title(args), tool.preview(args), lexer=tool.preview_lexer or None
        )

    return confirm


def _make_context(config: Config) -> ContextManager:
    """压缩参数全部配置驱动（D7），不新增命令行旗标。"""
    return ContextManager(
        model_context_limit=config.model_context_limit,
        compact_threshold=config.compact_threshold,
        summary_max_tokens=config.summary_max_tokens,
    )


def _print_mode_confirm(tool: Tool, args: dict) -> bool:
    """-p 模式的确认回调（M2_DESIGN 决策 1）：无人值守时不允许"静默写文件"。

    以异常硬拒而不是返回 False：返回 False 模型还能换方案继续跑，而 -p 场景
    下正确行为是立即失败并提示 --yolo（验收 2：退出码非 0）。
    """
    raise ConfirmRefused(
        f"非交互模式（-p）默认拒绝{tool.name}操作；"
        "如需自动执行请在命令行加 --yolo，或改用交互模式手动确认"
    )


def run_repl(config: Config, client: LLMClient, registry: ToolRegistry, *, yolo: bool) -> int:
    renderer = Renderer()
    renderer.banner(__version__, client.model, config.base_url)
    session = Session(SYSTEM_PROMPT)
    context = _make_context(config)
    prompt_session = build_prompt_session()
    if isinstance(prompt_session, _FallbackPrompt):
        renderer.hint(
            "当前终端不支持高级输入（历史/多行），已用简易模式；"
            "Windows Terminal / cmd 下体验完整功能"
        )

    def toolbar() -> HTML:
        # 状态栏常驻：让"上下文在长大"这件事可见，为 M4 的压缩做心理铺垫
        return HTML(
            f"<style fg='#7f7f7f'> {client.model} · 消息 {len(session.messages)}"
            f" · ≈{session.tokens_estimate():,} tokens · Esc+Enter 换行 · /help</style>"
        )

    while True:
        try:
            line = prompt_session.prompt("mncc> ", bottom_toolbar=toolbar)
        except KeyboardInterrupt:
            # §4.1：Ctrl+C 中断当前任务但不退出 REPL
            renderer.hint("已取消本次输入（Ctrl+C 不退出；/exit 或 Ctrl+D 退出）")
            continue
        except EOFError:
            break

        text = line.strip()
        if not text:
            continue
        if text.startswith("/"):
            if not handle_slash(
                text, session=session, client=client, renderer=renderer, context=context
            ):
                break
            continue

        session.add_user(text)
        # 任务级失败（max_turns/预算/错误）只体现在 status 与已渲染的错误面板，
        # REPL 不退出——下一句对话仍然可用（§4.2）
        run_agent_loop(
            client,
            renderer,
            session,
            registry,
            max_turns=config.max_turns,
            token_budget=config.token_budget,
            confirm=_repl_confirm(renderer),
            yolo=yolo,
            context=context,
        )

    renderer.hint("再见！")
    return EXIT_OK


def run_print_mode(
    config: Config,
    client: LLMClient,
    task: str,
    registry: ToolRegistry,
    *,
    yolo: bool,
    stats_json: str | None = None,
) -> int:
    """mncc -p：一次性执行。stdout 只有模型回复，进度与错误走 stderr（§4.1）。"""
    stderr_console = Console(stderr=True)
    renderer = Renderer(usage_console=stderr_console, error_console=stderr_console)
    session = Session(SYSTEM_PROMPT)
    session.add_user(task)
    context = _make_context(config)

    result = run_agent_loop(
        client,
        renderer,
        session,
        registry,
        max_turns=config.max_turns,
        token_budget=config.token_budget,
        confirm=_print_mode_confirm,
        yolo=yolo,
        context=context,
    )
    if result.status == STATUS_INTERRUPTED:
        stderr_console.print("[yellow]已中断[/yellow]")
        if stats_json:
            _write_stats(stats_json, result)
        return EXIT_INTERRUPT
    if result.status != STATUS_COMPLETED:
        # max_turns / budget_exceeded / error 都按失败计：bench 靠退出码判分
        if stats_json:
            _write_stats(stats_json, result)
        return EXIT_FAIL
    if stats_json:
        _write_stats(stats_json, result)
    return EXIT_OK


def _write_stats(path: str, result: LoopResult) -> None:
    """D4：评测记账的机器可读契约。进程能正常退出的所有终态都落盘（status 字段
    区分 completed/max_turns/budget_exceeded/interrupted/error）——失败任务的
    轮数/token 同样是归因数据；唯一例外是外部强杀（timeout）：进程没机会写，
    由 bench runner 自行记录。"""
    payload = {
        "status": result.status,
        "turns": result.turns,
        "prompt_tokens": result.total_usage.prompt_tokens,
        "completion_tokens": result.total_usage.completion_tokens,
        "total_tokens": result.total_usage.total_tokens,
        "elapsed": round(result.elapsed, 2),
        "chars": len(result.content),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _force_utf8_stdio() -> None:
    # Windows 控制台默认 GBK，统一 UTF-8 避免乱码（§3）；不可重配时静默跳过
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _print_startup_error(exc: Exception) -> None:
    console = Console(stderr=True)
    console.print(f"[red]配置错误：[/red]{exc}")
    console.print(
        f"[dim]示例（{GLOBAL_CONFIG_PATH} 或项目目录 .mncc.toml）：\n"
        'base_url = "https://open.bigmodel.cn/api/paas/v4/"\n'
        'model = "glm-4.6"\n'
        "export MNCC_API_KEY=sk-xxx[/dim]"
    )


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_arg_parser().parse_args(argv)

    try:
        config = load_config({"model": args.model} if args.model else None)
        api_key = resolve_api_key(config)
    except ConfigError as exc:
        _print_startup_error(exc)
        return EXIT_FAIL

    client = OpenAICompatClient(base_url=config.base_url, api_key=api_key, model=config.model)
    registry = build_registry()
    mcp_clients = _connect_mcp(registry, config.mcp_servers) if config.mcp_servers else []
    try:
        if args.task is not None:
            return run_print_mode(
                config, client, args.task, registry, yolo=args.yolo, stats_json=args.stats_json
            )
        try:
            return run_repl(config, client, registry, yolo=args.yolo)
        except KeyboardInterrupt:  # 流式渲染过程中的 Ctrl+C 兜底
            return EXIT_INTERRUPT
    finally:
        # D4/D5：-p 与 REPL 两种退出都统一关闭全部 MCP client（shutdown+terminate 防僵尸）
        for mcp_client in mcp_clients:
            mcp_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
