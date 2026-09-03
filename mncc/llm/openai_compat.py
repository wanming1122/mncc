"""OpenAI 兼容协议实现（GLM / DeepSeek / Qwen / Ollama 等均可对接）。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import openai

from .client import (
    CompletionResult,
    Event,
    LLMAuthError,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    Message,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    Usage,
)


def _to_friendly_error(exc: Exception) -> LLMError:
    """把 openai SDK 异常翻译成"用户知道下一步该干什么"的 LLMError。"""
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMAuthError(
            f"API key 无效或无权限（HTTP {exc.status_code}）："
            "请检查环境变量，或 ~/.mncc/config.toml 中的 api_key_env 指向的变量"
        )
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError("触发限流或账户额度不足（429）：请稍后重试，或降低调用频率")
    if isinstance(exc, openai.NotFoundError):
        return LLMResponseError(
            f"模型或接口不存在（404）：请检查配置中的 model 是否为该服务端支持的名称；详情：{exc}"
        )
    if isinstance(exc, openai.APITimeoutError):
        return LLMConnectionError("请求超时：网络不通或服务端无响应，请稍后重试")
    if isinstance(exc, openai.APIConnectionError):
        return LLMConnectionError(f"无法连接服务端：{exc}；请检查 base_url 是否正确、网络是否可达")
    if isinstance(exc, openai.APIStatusError):
        return LLMResponseError(f"服务端错误（HTTP {exc.status_code}）：{exc}")
    return LLMResponseError(f"调用 LLM 失败：{exc}")


class OpenAICompatClient(LLMClient):
    """基于 openai SDK 的兼容协议客户端。

    重试策略：连接失败 / 429 / 5xx 由 SDK 内建指数退避（max_retries）完成，
    满足 §4.2 的"有重试"；本类只负责把异常翻译成友好文案，保证 REPL 不崩。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        self._sdk = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self.base_url = base_url
        self.model = model

    def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[Event]:
        try:
            # 少数兼容端点不认 stream_options 参数；若因此被拒则去掉它重试一次
            yield from self._stream_once(messages, tools, include_usage=True)
        except openai.BadRequestError as exc:
            if "stream_options" in str(exc):
                yield from self._stream_once(messages, tools, include_usage=False)
            else:
                raise _to_friendly_error(exc) from exc

    def complete(
        self,
        messages: list[Message],
        max_tokens: int | None = None,
    ) -> CompletionResult:
        request: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        try:
            response = self._sdk.chat.completions.create(**request)
        except LLMError:
            raise
        except Exception as exc:
            raise _to_friendly_error(exc) from exc

        if not response.choices or response.choices[0].message is None:
            # 空 content 由 ContextManager 判空降级（D5）
            return CompletionResult("")
        content = response.choices[0].message.content or ""
        usage: Usage | None = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage = Usage(
                prompt_tokens=raw_usage.prompt_tokens or 0,
                completion_tokens=raw_usage.completion_tokens or 0,
                total_tokens=raw_usage.total_tokens
                or (raw_usage.prompt_tokens or 0) + (raw_usage.completion_tokens or 0),
            )
        return CompletionResult(content=content, usage=usage)

    def _stream_once(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        *,
        include_usage: bool,
    ) -> Iterator[Event]:
        request: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            request["tools"] = tools
        if include_usage:
            # OpenAI 协议：流式默认不返回 usage，必须显式要求
            request["stream_options"] = {"include_usage": True}

        parts: list[str] = []
        usage: Usage | None = None
        finish_reason = "stop"
        # 流式协议里 tool_calls 按 index 分片交错到达：id/name 在首片给全，
        # arguments 逐段追加。聚合必须在本客户端内完成（M2_DESIGN 决策 2）——
        # loop/UI 不该感知协议碎片。
        fragments: dict[int, dict[str, Any]] = {}
        try:
            raw_stream = self._sdk.chat.completions.create(**request)
            try:
                for chunk in raw_stream:
                    # usage 挂在最后一个 choices 为空的 chunk 上（include_usage）
                    if getattr(chunk, "usage", None) is not None:
                        u = chunk.usage
                        usage = Usage(
                            prompt_tokens=u.prompt_tokens or 0,
                            completion_tokens=u.completion_tokens or 0,
                            total_tokens=u.total_tokens
                            or (u.prompt_tokens or 0) + (u.completion_tokens or 0),
                        )
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    content = delta.content
                    if content:
                        parts.append(content)
                        yield TextDelta(content)
                    for frag in delta.tool_calls or ():
                        # 少数端点不发 index；缺省按 0 处理（单工具调用场景不受影响）
                        index = frag.index if frag.index is not None else 0
                        slot = fragments.setdefault(index, {"id": "", "name": "", "arguments": []})
                        if frag.id:
                            slot["id"] = frag.id
                        if frag.function is not None:
                            if frag.function.name:
                                slot["name"] = frag.function.name
                            if frag.function.arguments:
                                slot["arguments"].append(frag.function.arguments)
            finally:
                # 用户中途打断（KeyboardInterrupt）时也要释放底层连接
                close = getattr(raw_stream, "close", None)
                if close is not None:
                    close()
        except openai.BadRequestError:
            raise  # 交给 stream() 决定是否降级重试
        except LLMError:
            raise
        except Exception as exc:
            raise _to_friendly_error(exc) from exc

        tool_calls = [
            ToolCall(id=slot["id"], name=slot["name"], arguments="".join(slot["arguments"]))
            for _, slot in sorted(fragments.items())
            if slot["id"] or slot["name"]
        ]
        yield ResponseCompleted(
            content="".join(parts), finish_reason=finish_reason, usage=usage, tool_calls=tool_calls
        )
