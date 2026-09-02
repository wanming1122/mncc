"""LLM 客户端抽象层：事件流 + 统一异常。

为什么要有这层抽象而不让上层直接用 openai SDK：
1. §7 计划增加 Anthropic 原生协议的第二个实现，Agent 循环与 UI 只依赖
   这里的 Event/异常类型，换协议不动业务层——这是抽象的试金石；
2. 单元测试（§10：LLM 一律 mock）只需构造假事件序列，不需要网络。

为什么 stream() 是同步迭代器而不是 §8 草案里的 AsyncIterator：
- mncc 的执行模型严格串行（一问一答、工具依次执行），asyncio 带不来并发收益；
- REPL 要用 KeyboardInterrupt 精准打断"正在生成的回复"，同步阻塞调用天然支持，
  而 asyncio 的信号取消与 prompt_toolkit 事件循环集成在 Windows 上坑很多；
- 若 M6 的 MCP 客户端需要异步，可以让它在独立线程里跑自己的事件循环。
抽象层保证未来切换成本集中在 openai_compat.py 与 loop.py 两个文件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# OpenAI 消息格式：{"role": "system"|"user"|"assistant"|"tool", "content": str, ...}
# M2 引入 tool_calls 后结构会扩展，先用宽松的 dict 而不是 TypedDict，
# 避免为每种消息形态各建一个类型。
Message = dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """一次（或累计）API 调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class Event:
    """stream() 产出的事件基类。"""


@dataclass
class TextDelta(Event):
    """一段增量文本。"""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求（协议层聚合完成的产物）。

    arguments 保留原始 JSON 串而不是解析后的 dict：解析放在 ToolRegistry
    且失败时作为错误回填给模型自纠——协议层不掺业务语义。
    """

    id: str
    name: str
    arguments: str


@dataclass
class ResponseCompleted(Event):
    """一轮回复结束：完整文本 + 结束原因 + 用量 + 聚合后的 tool_calls。

    tool_calls 非空表示模型要求执行工具；调用方按 tool_call_id 以 role=tool
    消息逐个回填结果。流式协议里的分片在此刻之前必须已经聚合完（M2_DESIGN 决策 2）。
    """

    content: str
    finish_reason: str = "stop"
    usage: Usage | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class CompletionResult:
    """非流式补全结果（summarize 等内部任务用）。"""

    content: str
    usage: Usage | None = None


class LLMError(Exception):
    """LLM 调用失败的统一出口。message 必须是面向用户可读的中文。"""


class LLMAuthError(LLMError):
    """API key 无效 / 无权限。"""


class LLMRateLimitError(LLMError):
    """限流或账户额度问题。"""


class LLMConnectionError(LLMError):
    """网络不通 / 超时。"""


class LLMResponseError(LLMError):
    """服务端拒绝请求或返回了无法解析的响应。"""


class LLMClient(ABC):
    """LLM 客户端抽象。model 是可变属性：/model 命令运行期切换。"""

    model: str

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[Event]:
        """流式请求一轮补全。

        产出 TextDelta（增量文本），最后产出恰好一个 ResponseCompleted。
        失败时抛出 LLMError 子类；调用方收到 KeyboardInterrupt 视为用户打断。
        """
        raise NotImplementedError

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """非流式补全（L2 摘要等内部任务用）。失败抛 LLMError 子类。

        为什么单独一个能力面而不是复用 stream()：内部任务不需要 UI 流式
        渲染，同步一次拿结果代码路径最短；未来接 Anthropic 时两种实现都做，
        抽象价值得以验证。
        """
        raise NotImplementedError
