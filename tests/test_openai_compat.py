"""OpenAI 兼容客户端：流式事件聚合、usage 解析、异常翻译、stream_options 降级。

openai SDK 不发起真实请求：OpenAI 构造函数与 completions.create 全部替换
为脚本化的假对象（§10：LLM 一律 mock）。
"""

from __future__ import annotations

from typing import Any

try:  # openai>=3 改用 httpx2，旧版仍是 httpx；测试两种环境都要能跑
    import httpx2 as httpx
except ModuleNotFoundError:
    import httpx

import openai
import pytest
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage

import mncc.llm.openai_compat as compat
from mncc.llm.client import (
    LLMAuthError,
    LLMConnectionError,
    ResponseCompleted,
    TextDelta,
    Usage,
)


def _chunk(content: str | None = None, finish: str | None = None, usage=None):
    return ChatCompletionChunk(
        id="cmpl-1",
        created=1700000000,
        model="test",
        object="chat.completion.chunk",
        choices=(
            []
            if usage is not None
            else [Choice(index=0, delta=ChoiceDelta(content=content), finish_reason=finish)]
        ),
        usage=usage,
    )


class ScriptedStream:
    def __init__(self, chunks: list) -> None:
        self._iter = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)

    def close(self) -> None:
        self.closed = True


class ScriptedCompletions:
    def __init__(
        self,
        chunks: list,
        error: Exception | None = None,
        fail_when: str | None = None,
        response: Any = None,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.fail_when = fail_when
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.error is not None and (self.fail_when is None or self.fail_when in kwargs):
            raise self.error
        if self.response is not None:
            return self.response  # 非流式响应（complete()）
        return ScriptedStream(self.chunks)


@pytest.fixture()
def install_fake_openai(monkeypatch: pytest.MonkeyPatch):
    def _install(
        chunks: list,
        error: Exception | None = None,
        fail_when: str | None = None,
        response: Any = None,
    ):
        holder: dict[str, Any] = {}

        def factory(**kwargs: Any):
            completions = ScriptedCompletions(chunks, error, fail_when, response)
            holder["completions"] = completions
            holder["client_kwargs"] = kwargs
            chat = type("Chat", (), {"completions": completions})()
            return type("SDK", (), {"chat": chat})()

        monkeypatch.setattr(openai, "OpenAI", factory)
        return holder

    return _install


def _make_client() -> compat.OpenAICompatClient:
    return compat.OpenAICompatClient(base_url="https://fake.local/v1", api_key="sk", model="m")


def _http_error(cls, status: int, message: str = "boom"):
    request = httpx.Request("POST", "https://fake.local/v1/chat/completions")
    return cls(message, response=httpx.Response(status, request=request), body=None)


MESSAGES = [{"role": "user", "content": "hi"}]


def test_stream_aggregates_deltas_and_usage(install_fake_openai) -> None:
    chunks = [
        _chunk(content="你"),
        _chunk(content="好"),
        _chunk(finish="stop"),
        _chunk(usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]
    holder = install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    assert events[0] == TextDelta("你")
    assert events[1] == TextDelta("好")
    last = events[-1]
    assert isinstance(last, ResponseCompleted)
    assert last.content == "你好"
    assert last.finish_reason == "stop"
    assert last.usage == Usage(10, 5, 15)
    # 请求确实带上了 include_usage（拿到真实 token 计量的关键）
    assert holder["completions"].calls[0]["stream_options"] == {"include_usage": True}


def test_usage_chunk_with_empty_choices_is_skipped(install_fake_openai) -> None:
    chunks = [
        _chunk(content="ok"),
        _chunk(finish="stop"),
        _chunk(usage=CompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))
    assert events[-1].usage == Usage(1, 1, 2)


def test_stream_options_fallback_for_picky_endpoints(install_fake_openai) -> None:
    # 真实世界里被拒的原因是报错文案点名该参数（如 "Unsupported parameter: stream_options"）
    error = _http_error(openai.BadRequestError, 400, "Unsupported parameter: 'stream_options'")
    chunks = [_chunk(content="ok", finish="stop")]
    holder = install_fake_openai(chunks, error=error, fail_when="stream_options")

    events = list(_make_client().stream(MESSAGES))

    assert events[-1].content == "ok"
    calls = holder["completions"].calls
    assert len(calls) == 2
    assert "stream_options" not in calls[1]


def test_other_bad_request_is_mapped(install_fake_openai) -> None:
    install_fake_openai([], error=_http_error(openai.BadRequestError, 400))
    from mncc.llm.client import LLMResponseError

    with pytest.raises(LLMResponseError):
        list(_make_client().stream(MESSAGES))


def test_auth_error_mapped(install_fake_openai) -> None:
    install_fake_openai([], error=_http_error(openai.AuthenticationError, 401))
    with pytest.raises(LLMAuthError, match="API key"):
        list(_make_client().stream(MESSAGES))


def test_connection_error_mapped(install_fake_openai) -> None:
    request = httpx.Request("POST", "https://fake.local/v1/chat/completions")
    install_fake_openai([], error=openai.APIConnectionError(request=request))
    with pytest.raises(LLMConnectionError, match="无法连接"):
        list(_make_client().stream(MESSAGES))


def test_empty_stream_yields_empty_completed(install_fake_openai) -> None:
    install_fake_openai([])
    events = list(_make_client().stream(MESSAGES))
    assert len(events) == 1
    assert events[0].content == ""
    assert events[0].usage is None


# ---- tool_calls 聚合测试（M2 核心）----


def _chunk_with_tool_calls(
    index: int,
    *,
    tc_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    finish: str | None = None,
):
    """构造携带 delta.tool_calls 的 chunk。"""
    func = (
        ChoiceDeltaToolCallFunction(name=name, arguments=arguments)
        if (name or arguments)
        else None
    )
    tc = ChoiceDeltaToolCall(index=index, id=tc_id, function=func)
    return ChatCompletionChunk(
        id="cmpl-2",
        created=1700000000,
        model="test",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tc]), finish_reason=finish)],
    )


def test_tool_calls_single_call_aggregated(install_fake_openai) -> None:
    """单个工具调用：id/name 首片给全，arguments 分两段拼接。"""
    chunks = [
        _chunk_with_tool_calls(0, tc_id="call_abc", name="read_file", arguments='{"path":'),
        _chunk_with_tool_calls(0, arguments='"test.py"}'),
        _chunk(finish="tool_calls"),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    completed = events[-1]
    assert isinstance(completed, ResponseCompleted)
    assert completed.finish_reason == "tool_calls"
    assert len(completed.tool_calls) == 1
    tc = completed.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "read_file"
    assert tc.arguments == '{"path":"test.py"}'


def test_tool_calls_multiple_calls_different_indices(install_fake_openai) -> None:
    """两个工具调用，index=0 和 index=1 交错到达。"""
    chunks = [
        _chunk_with_tool_calls(0, tc_id="call_1", name="read_file", arguments='{"path":'),
        _chunk_with_tool_calls(1, tc_id="call_2", name="run_command", arguments='{"cmd":'),
        _chunk_with_tool_calls(0, arguments='"a.py"}'),
        _chunk_with_tool_calls(1, arguments='"pytest"}'),
        _chunk(finish="tool_calls"),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    completed = events[-1]
    assert isinstance(completed, ResponseCompleted)
    assert len(completed.tool_calls) == 2
    assert completed.tool_calls[0].name == "read_file"
    assert completed.tool_calls[0].arguments == '{"path":"a.py"}'
    assert completed.tool_calls[1].name == "run_command"
    assert completed.tool_calls[1].arguments == '{"cmd":"pytest"}'


def test_tool_calls_no_text_content(install_fake_openai) -> None:
    """纯工具调用（content 为空）。"""
    chunks = [
        _chunk_with_tool_calls(0, tc_id="call_x", name="echo", arguments='{"text":"hi"}'),
        _chunk(finish="tool_calls"),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    text_events = [e for e in events if isinstance(e, TextDelta)]
    assert text_events == []  # 无文本增量
    assert events[-1].content == ""  # content 为空
    assert events[-1].tool_calls[0].name == "echo"


def test_tool_calls_text_before_and_after(install_fake_openai) -> None:
    """模型先输出文本，再发起工具调用。"""
    chunks = [
        _chunk(content="让我看看"),
        _chunk_with_tool_calls(0, tc_id="call_z", name="read_file", arguments='{"path":"x.py"}'),
        _chunk(finish="tool_calls"),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    text_events = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(e.text for e in text_events) == "让我看看"
    assert events[-1].content == "让我看看"
    assert events[-1].tool_calls[0].name == "read_file"


def test_tool_calls_with_usage(install_fake_openai) -> None:
    """工具调用 + usage 同时返回。"""
    chunks = [
        _chunk_with_tool_calls(0, tc_id="call_u", name="echo", arguments='{}'),
        _chunk(finish="tool_calls"),
        _chunk(usage=CompletionUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28)),
    ]
    install_fake_openai(chunks)
    events = list(_make_client().stream(MESSAGES))

    completed = events[-1]
    assert completed.tool_calls[0].name == "echo"
    assert completed.usage == Usage(20, 8, 28)


# ---- M4：非流式 complete()（供 L2 摘要使用）----


def _fake_response(content: str | None, usage: CompletionUsage | None) -> Any:
    message = type("M", (), {"content": content})()
    choice = type("C", (), {"message": message})()
    return type("R", (), {"choices": [choice], "usage": usage})()


def test_complete_non_streaming_request_and_parse(install_fake_openai) -> None:
    response = _fake_response(
        "摘要文本", CompletionUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    )
    holder = install_fake_openai([], response=response)
    result = _make_client().complete(MESSAGES, max_tokens=500)

    call = holder["completions"].calls[0]
    assert call["stream"] is False
    assert call["max_tokens"] == 500
    assert call["messages"] == MESSAGES
    assert "tools" not in call
    assert result.content == "摘要文本"
    assert result.usage == Usage(100, 20, 120)


def test_complete_omits_max_tokens_when_none(install_fake_openai) -> None:
    holder = install_fake_openai([], response=_fake_response("ok", None))
    _make_client().complete(MESSAGES)
    assert "max_tokens" not in holder["completions"].calls[0]


def test_complete_empty_content_returns_empty_string(install_fake_openai) -> None:
    install_fake_openai([], response=_fake_response(None, None))
    result = _make_client().complete(MESSAGES)
    assert result.content == ""  # 由 ContextManager 判空降级
    assert result.usage is None


def test_complete_maps_errors(install_fake_openai) -> None:
    install_fake_openai([], error=_http_error(openai.AuthenticationError, 401))
    with pytest.raises(LLMAuthError, match="API key"):
        _make_client().complete(MESSAGES)
