"""Agent 循环（§4.2 核心）：消息历史 + 工具分发 + 轮次/预算控制 + 事件回调。

M2 起为完整闭环：用户任务 → 模型流式回复 → tool_calls → 本地执行 →
role=tool 回填 → 模型继续，直到模型不再调用工具（completed）、达到最大轮数
（max_turns）、耗尽 token 预算（budget_exceeded）、用户中断（interrupted）
或出错（error）。纯对话（registry=None 或为空）与带工具执行共用这一个函数，
REPL 与 -p 的差异全部由参数注入（confirm 回调、yolo），不设第二条代码路径。

中断语义（M2_DESIGN）：KeyboardInterrupt 在流式阶段到来时保留半截回复；
在工具执行阶段到来时，已完成的工具结果照常回填、未执行的补占位 tool 消息
——OpenAI 协议要求每个 tool_call 必须有对应 role=tool 消息，否则历史结构
非法，下一轮请求会被服务端拒绝。保持结构合法优先于一切。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..llm.client import (
    LLMClient,
    LLMError,
    Message,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    Usage,
)
from ..tools.base import ConfirmFn, ConfirmRefused, ToolRegistry
from .context import ContextManager, estimate_tokens

STATUS_COMPLETED = "completed"
STATUS_MAX_TURNS = "max_turns"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"

DEFAULT_MAX_TURNS = 25
DEFAULT_TOKEN_BUDGET = 200_000

_INTERRUPTED_TOOL_NOTE = "（用户中断，工具未执行完成）"
_REFUSED_TOOL_NOTE = "（操作被拒绝，任务中止）"


class Session:
    """会话状态：消息历史 + 累计用量。REPL 与 -p 模式共用。"""

    def __init__(self, system_prompt: str = "") -> None:
        self.messages: list[Message] = []
        self.total_usage = Usage()
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        if text:
            self.messages.append({"role": "assistant", "content": text})

    def add_assistant_tool_calls(self, content: str, tool_calls: list[ToolCall]) -> None:
        """回填发起工具调用的 assistant 消息（OpenAI 标准结构）。

        content 为空时必须用 None 而不是 ""：部分兼容端点对
        content="" + tool_calls 的组合会拒绝请求。
        """
        self.messages.append(
            {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )

    def add_tool_message(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def reset(self) -> None:
        """/clear：清掉对话，但保留 system prompt——身份与规则不该被清空。"""
        system = (
            self.messages[:1]
            if self.messages and self.messages[0].get("role") == "system"
            else []
        )
        self.messages = system
        self.total_usage = Usage()

    def add_usage(self, usage: Usage) -> None:
        self.total_usage = self.total_usage + usage

    def tokens_estimate(self) -> int:
        total = 0
        for m in self.messages:
            total += estimate_tokens(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                total += estimate_tokens(str(tc.get("function", {}).get("arguments") or ""))
        return total


@dataclass
class LoopResult:
    """一次任务的终态。turns/usage/elapsed 从 M2 就定型：M5 bench 要记录
    "成败/轮数/token/耗时"，接口先定避免返工。"""

    status: str  # completed | max_turns | budget_exceeded | interrupted | error
    turns: int = 0
    total_usage: Usage = field(default_factory=Usage)
    elapsed: float = 0.0
    content: str = ""  # 最后一条非空 assistant 文本（任务汇报）


def _report_usage(
    renderer: Any, session: Session, usage: Usage | None, content: str, prompt_est: int
) -> None:
    """每轮打印 token 用量（§4.2）；服务端没回 usage 时用启发式估算并标注 ≈。"""
    if usage is not None:
        renderer.usage(usage, session.total_usage)
        return
    completion = estimate_tokens(content)
    renderer.usage(
        Usage(prompt_est, completion, prompt_est + completion),
        session.total_usage,
        estimated=True,
    )


def compact_session(
    session: Session,
    context: ContextManager,
    client: LLMClient,
    renderer: Any,
) -> bool:
    """压缩上下文（auto 与手动 /compact 共用）。返回是否发生了压缩。

    为什么 auto 与手动共用一份实现：两者只差"谁发起"，动作完全相同，
    避免两处漂移。auto 路径调用时未达阈值直接返回 False、无任何输出。
    """
    if not context.should_compact(session.messages):
        return False
    session.messages, report = context.compact(session.messages, client)
    renderer.compact(report)
    return True


def run_agent_loop(
    client: LLMClient,
    renderer: Any,
    session: Session,
    registry: ToolRegistry | None = None,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    confirm: ConfirmFn | None = None,
    yolo: bool = False,
    context: ContextManager | None = None,
) -> LoopResult:
    """执行一个完整任务（可能含多轮模型调用与工具执行），返回终态。

    预期内的失败（LLMError、确认拒绝）都不向上抛——抛了 REPL 就崩（§4.2）；
    一律转成 LoopResult.status，由调用方决定展示与退出码。renderer 用 Any
    是为了避免 agent -> ui -> agent 的循环导入（渲染器在本模块只被鸭子类型使用）。
    """
    if context is None:
        # 自动档（与配置默认值一致）：现有测试不带 context 参数也能跑且不触发压缩
        context = ContextManager(
            model_context_limit=128_000, compact_threshold=0.8, summary_max_tokens=500
        )
    start = time.monotonic()
    turns = 0
    last_text = ""
    schemas = registry.openai_schemas() if registry and registry.names() else None
    confirm_fn: ConfirmFn = confirm or (lambda _tool, _args: False)

    def finish(status: str) -> LoopResult:
        # last_text/turns 都是闭包读当前值，注意不要写成默认参数（定义时求值会固化）
        return LoopResult(
            status=status,
            turns=turns,
            total_usage=session.total_usage,
            elapsed=time.monotonic() - start,
            content=last_text,
        )

    while True:
        # 预算/轮数守卫放在每轮模型调用之前：超限就不再发起请求
        if session.total_usage.total_tokens > token_budget:
            renderer.hint(
                f"已达任务 token 预算上限（{token_budget:,}），任务中止"
                f"（实际用量 {session.total_usage.total_tokens:,}）"
            )
            return finish(STATUS_BUDGET_EXCEEDED)
        if turns >= max_turns:
            renderer.hint(
                f"已达最大轮数（{max_turns}），任务中止；可继续对话引导，或 /clear 后重试"
            )
            return finish(STATUS_MAX_TURNS)
        turns += 1

        # auto-compact（D2）：与轮数/预算守卫同在"本轮模型调用之前"——
        # 估算逼近窗口上限时先压缩再发请求，请求发出去超限（服务端 400）就晚了
        compact_session(session, context, client, renderer)

        prompt_est = session.tokens_estimate()
        renderer.stream_start()
        parts: list[str] = []
        completed: ResponseCompleted | None = None
        try:
            for event in client.stream(session.messages, tools=schemas):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    renderer.stream_delta(event.text)
                elif isinstance(event, ResponseCompleted):
                    completed = event
        except KeyboardInterrupt:
            partial = "".join(parts)
            renderer.tool_aborted()
            renderer.stream_abort(partial)  # 半截回复保留进历史，用户可以接着追问
            if partial:
                session.add_assistant(partial)
            return finish(STATUS_INTERRUPTED)
        except LLMError as exc:
            renderer.tool_aborted()
            renderer.stream_abort("".join(parts), note=None)  # 错误面板随后给出
            renderer.error(exc)
            return finish(STATUS_ERROR)

        if completed is None:
            # stream 契约保证恰好一个 ResponseCompleted；这里只兜异常流
            renderer.tool_aborted()
            renderer.stream_abort("".join(parts), note=None)
            renderer.error(LLMError("模型流异常结束，未收到完整回复"))
            return finish(STATUS_ERROR)

        content = completed.content
        if completed.usage is not None:
            session.add_usage(completed.usage)
            # D1 在线校准：ratio = 真实 prompt tokens / 本地估算（本轮 stream 前快照）
            context.estimator.observe(prompt_est, completed.usage.prompt_tokens)
        renderer.stream_end(content)
        _report_usage(renderer, session, completed.usage, content, prompt_est)
        if content.strip():
            last_text = content

        if not completed.tool_calls:
            session.add_assistant(content)
            if not content:
                renderer.hint(f"（模型返回空回复，finish_reason={completed.finish_reason}）")
            return finish(STATUS_COMPLETED)

        if registry is None:
            # 模型在无工具会话里硬要调工具：回填错误让模型改用纯文字回答
            session.add_assistant_tool_calls(content, completed.tool_calls)
            for tc in completed.tool_calls:
                session.add_tool_message(
                    tc.id,
                    context.truncate_tool_output(
                        "当前会话未启用任何工具，请直接用文字回答"
                    ),
                )
            continue

        # ---- 工具轮：逐个 执行 → 回填 role=tool（结构合法性高于一切）----
        session.add_assistant_tool_calls(content, completed.tool_calls)
        done = 0
        try:
            for tc in completed.tool_calls:
                assert registry is not None  # 上一分支已 continue，仅为类型收窄
                renderer.tool_progress(tc.name, registry.brief(tc.name, tc.arguments))
                elapsed = time.monotonic()
                result = registry.execute(tc.name, tc.arguments, confirm=confirm_fn, yolo=yolo)
                renderer.tool_done(is_error=result.is_error, elapsed=time.monotonic() - elapsed)
                # L1（D6）：回填前的统一体积兜底（16000 字符），记录进历史前压一手
                session.add_tool_message(
                    tc.id, context.truncate_tool_output(result.output)
                )
                done += 1
        except KeyboardInterrupt:
            for tc in completed.tool_calls[done:]:
                session.add_tool_message(
                    tc.id, context.truncate_tool_output(_INTERRUPTED_TOOL_NOTE)
                )
            renderer.tool_aborted()
            renderer.hint("已中断（已执行的工具结果已保留，可继续对话）")
            return finish(STATUS_INTERRUPTED)
        except ConfirmRefused as exc:
            for tc in completed.tool_calls[done:]:
                session.add_tool_message(
                    tc.id, context.truncate_tool_output(_REFUSED_TOOL_NOTE)
                )
            renderer.error(exc, title="操作被拒")
            return finish(STATUS_ERROR)
