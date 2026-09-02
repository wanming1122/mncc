"""rich 渲染器：流式 Markdown、用量行、错误面板、工具进度、确认交互。

流式渲染的取舍（面试点）：rich.Live 没有"追加"API，每次刷新都要把累计
文本整体重排成 Markdown，长文本会明显卡顿，所以增量按 ~100ms 节流；
Live 用 vertical_overflow="visible"，内容高出屏幕时上半部分流入滚动区、
底部保持刷新——既实时又不刷屏。

工具进度的"单行覆盖式"（§4.1 不刷屏）：开始时打印不换行的摘要行，结束后
在同一行续写 ✓/✗，一行即一次工具调用的完整痕迹；异常中断时由 tool_aborted
补上状态并换行，避免留下悬空的半行。
"""

from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from ..agent.context import CompactReport, ContextManager, estimate_tokens
from ..llm.client import Message, Usage

_REFRESH_INTERVAL = 0.1  # 秒；增量渲染节流


class Renderer:
    """所有终端输出都经过这里，便于统一风格与在测试里重定向到 StringIO。"""

    def __init__(
        self,
        console: Console | None = None,
        *,
        usage_console: Console | None = None,
        error_console: Console | None = None,
    ) -> None:
        self.console = console or Console()
        # -p 模式把用量与错误打到 stderr，保证 stdout 只有模型回复，方便脚本消费
        self.usage_console = usage_console
        self.error_console = error_console
        self._live: Live | None = None
        self._parts: list[str] = []
        self._last_refresh = 0.0
        self._tool_pending = False  # tool_progress 已打印、尚未换行的悬空状态

    # ---- 生命周期与流式输出 ----

    def banner(self, version: str, model: str, base_url: str) -> None:
        self.console.print(
            Panel.fit(
                f"[bold cyan]mncc[/bold cyan] v{version} — 工具模式"
                "（read / write / edit / list / grep / run）\n"
                f"[dim]模型 {model} @ {base_url}[/dim]",
                border_style="cyan",
            )
        )
        self.console.print(
            "[dim]多行输入：Esc+Enter 或 Ctrl+J 换行 · Ctrl+C 中断 · "
            "/help 查看命令 · /exit 或 Ctrl+D 退出[/dim]\n"
        )

    def stream_start(self) -> None:
        self._parts = []
        self._last_refresh = 0.0
        self._live = Live(console=self.console, vertical_overflow="visible", refresh_per_second=8)
        self._live.start()

    def stream_delta(self, text: str) -> None:
        self._parts.append(text)
        if self._live is None:
            # 没有 Live（异常路径兜底）：直接打印增量，不丢内容
            self.console.print(text, end="")
            return
        now = time.monotonic()
        if now - self._last_refresh >= _REFRESH_INTERVAL:
            self._last_refresh = now
            self._live.update(Markdown("".join(self._parts)))

    def stream_end(self, final_text: str) -> None:
        self._close_live(Markdown(final_text) if final_text else None)
        if final_text:
            self.console.print()

    def stream_abort(self, partial: str, *, note: str | None = "⚠ 已中断") -> None:
        """流被提前终止（用户打断或出错）。note=None 用于错误路径，避免误报"已中断"。"""
        self._close_live(Markdown(partial) if partial else None)
        if note:
            self.console.print(f"[yellow]{note}[/yellow]\n")

    def _close_live(self, renderable: object) -> None:
        if self._live is not None:
            if renderable is not None:
                self._live.update(renderable)
            self._live.stop()
            self._live = None

    # ---- 工具执行进度（-p 模式走 usage_console，保证 stdout 只有模型回复）----

    def _progress_console(self) -> Console:
        return self.usage_console or self.console

    def tool_progress(self, name: str, brief: str) -> None:
        self._tool_pending = True
        self._progress_console().print(f"[dim]▸ {name}：{brief}[/dim]", end="")

    def tool_done(self, *, is_error: bool, elapsed: float) -> None:
        mark = "[red]✗[/red]" if is_error else "[green]✓[/green]"
        self._progress_console().print(f" {mark} [dim]{elapsed:.1f}s[/dim]")
        self._tool_pending = False

    def tool_aborted(self) -> None:
        """中断/异常路径收尾：补上悬空进度行的状态并换行。"""
        if self._tool_pending:
            self._progress_console().print(" [yellow]✗ 中断[/yellow]")
            self._tool_pending = False

    def confirm(self, title: str, body: str, *, lexer: str | None = None) -> bool:
        """通用操作确认：正文面板 +（可选）语法高亮（edit 用 diff）。默认 No。"""
        renderable: object = Syntax(body, lexer) if lexer else body
        self.console.print()
        self.console.print(
            Panel(renderable, title=f"[yellow]{title}[/yellow]", border_style="yellow")
        )
        return Confirm.ask("允许执行？", default=False, console=self.console)

    # ---- 非流式输出 ----

    def usage(self, usage: Usage, cumulative: Usage, *, estimated: bool = False) -> None:
        mark = "≈" if estimated else ""
        line = (
            f"[dim]{mark}tokens 输入 {usage.prompt_tokens:,} · 输出 {usage.completion_tokens:,}"
            f" · 本轮 {usage.total_tokens:,} · 累计 {cumulative.total_tokens:,}[/dim]"
        )
        (self.usage_console or self.console).print(line)

    def error(self, exc: Exception, *, title: str = "错误") -> None:
        (self.error_console or self.console).print(
            Panel(str(exc), title=f"[red]{title}[/red]", border_style="red")
        )

    def hint(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/dim]")

    def print_text(self, text: str) -> None:
        """原样输出纯文本（/help 这类本身就是排好版的内容）。"""
        self.console.print(text)

    def compact(self, report: CompactReport) -> None:
        """L2 压缩结果面板（auto 与手动 /compact 共用）。"""
        if report.degraded:
            note = "[yellow]摘要生成失败，已降级为截断最老消息[/yellow]"
        elif report.summary_chars == 0:
            note = "[dim]无可压缩内容[/dim]"
        else:
            note = f"[dim]摘要 {report.summary_chars:,} 字符[/dim]"
        body = f"{report.before_tokens:,} → {report.after_tokens:,} tokens　{note}"
        (self.usage_console or self.console).print(
            Panel(body, title="[cyan]上下文压缩[/cyan]", border_style="cyan")
        )

    def context_view(
        self,
        messages: list[Message],
        est_tokens: int,
        *,
        context: ContextManager | None = None,
    ) -> None:
        table = Table(title=f"上下文概览（估算 ≈{est_tokens:,} tokens）", border_style="dim")
        table.add_column("#", justify="right", style="dim")
        table.add_column("角色")
        table.add_column("字符数", justify="right")
        table.add_column("≈tokens", justify="right")
        for i, message in enumerate(messages):
            content = str(message.get("content") or "")
            table.add_row(
                str(i),
                str(message.get("role", "?")),
                str(len(content)),
                f"{estimate_tokens(content):,}",
            )
        self.console.print(table)
        if context is None:
            self.console.print("[dim]可用 /compact 手动压缩，或用 /clear 重置上下文[/dim]\n")
            return
        calibrated = context.estimate_messages(messages)
        ratio = calibrated / context.model_context_limit if context.model_context_limit else 0.0
        self.console.print(
            f"[dim]模型窗口 {context.model_context_limit:,} tokens · 校准后估算"
            f" ≈{calibrated:,}（{ratio:.1%} 已用）· 压缩阈值 {context.compact_threshold:.0%}"
            f" · 英文密度系数 ≈{context.estimator.divisor:.2f} 字符/token[/dim]\n"
        )
